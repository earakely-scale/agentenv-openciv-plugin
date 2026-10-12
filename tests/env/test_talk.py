"""What the agents say, to each other and to the people watching (docs/tools.md): the message tool, end_turn's note,
the plan on the broadcast, the broadcast pace (min_turn_seconds) and the broadcast's title and casters, with the fake
bridge (whose seats share one scripted game)."""

import asyncio
import base64
import json
import time
import urllib.error
from types import SimpleNamespace

import pytest
from agentenv_game import GameError
from mcp.server.fastmcp.exceptions import ToolError
from seated import lobby_game, playing

from agentenv_openciv3 import actionlog, broadcast, server

pytestmark = pytest.mark.anyio

MATCH = {"seats": ["Greece", "Egypt"], "labels": {"Rome": "opus", "Greece": "sonnet"}, "opponents": 3}
CIVS = ("Rome", "Greece", "Egypt")
INDEX = {"Rome": 0, "Greece": 1, "Egypt": 2}   # the fake bridge's player indices; Babylon (3) is an AI civ


class Leader:
    """The agent playing `civ`: its tool calls are at its player slot's address."""

    def __init__(self, env, civ: str):
        self.env, self.civ = env, civ

    async def _call(self, name: str, args: dict):
        with playing(self.env, self.civ):
            return (await self.env.mcp.call_tool(name, args))[0].text

    async def __call__(self, name: str, **args) -> str:
        return await self._call(name, args)

    async def error(self, name: str, **args) -> str:
        with pytest.raises(ToolError) as info:
            await self._call(name, args)
        return str(info.value).removeprefix(f"Error executing tool {name}: ")


@pytest.fixture
async def match(env):
    await lobby_game(env, **MATCH)
    return env, {civ: Leader(env, civ) for civ in CIVS}


async def end_turn(leaders: dict, **notes: str) -> list[str]:
    """Every seat ends the turn (skip_idle), each with its note from `notes` (by civ)."""
    return await asyncio.gather(*(leaders[c]("end_turn", skip_idle=True, **(
        {"note": notes[c]} if c in notes else {})) for c in CIVS))


async def live_data(env, since: int = -1) -> dict:
    return json.loads((await env._live_data(SimpleNamespace(query_params={"since": str(since)}))).body)


async def test_a_message_reaches_its_leader_once_in_its_next_reply_and_in_its_brief(match, action_log):
    env, leaders = match
    sent = await leaders["Rome"]("message", to="greece", text="Join me against Egypt.")
    assert sent == 'Sent to Greece (sonnet) · message 1 of 3 this turn: "Join me against Egypt."'
    units = await leaders["Greece"]("list_units")
    assert units.startswith('✉ Rome (opus) to you: "Join me against Egypt."\n')
    assert "✉" not in await leaders["Greece"]("list_units")
    assert "✉" not in await leaders["Egypt"]("list_units")
    # A brief as the next reply lists the message under MESSAGES instead.
    await leaders["Greece"]("message", to="opus", text="Deal. Egypt falls by T10.")
    brief = await leaders["Rome"]("get_turn_brief")
    assert "✉" not in brief
    assert ('MESSAGES\n  T1 you to Greece: "Join me against Egypt."\n'
            '  T1 Greece (sonnet) to you: "Deal. Egypt falls by T10."\nPLAN') in brief
    assert "MESSAGES" not in await leaders["Egypt"]("get_turn_brief")
    assert [{k: m[k] for k in ("turn", "from", "to", "text")} for m in env.messages] == [
        {"turn": 1, "from": "Rome", "to": ["Greece"], "text": "Join me against Egypt."},
        {"turn": 1, "from": "Greece", "to": ["Rome"], "text": "Deal. Egypt falls by T10."}]
    assert all(0 <= m["seconds"] < 30 for m in env.messages)
    # A call of the seat, but no game action: logged, no footer, not in the actions the viewer shows.
    rows = [r for r in action_log() if r["tool"] == "message"]
    assert [(r["seat"], r["args"]["to"], r["ok"]) for r in rows] == [("Rome", "greece", True), ("Greece", "opus", True)]
    assert "[T1/" not in sent and not env.seats[0].actions.timeline
    assert actionlog.describe("message", {"to": "all", "text": "hi"}) is None


async def test_a_message_to_all_reaches_every_other_leader_still_in_the_game(match):
    env, leaders = match
    sent = await leaders["Egypt"]("message", to="ALL", text="Peace to all who keep off the Nile.")
    assert sent.startswith("Sent to all: Rome (opus), Greece (sonnet) · message 1 of 3 this turn")
    for civ in ("Rome", "Greece"):
        assert (await leaders[civ]("list_units")).startswith(
            '✉ Egypt to all: "Peace to all who keep off the Nile."\n')
    assert "✉" not in await leaders["Egypt"]("list_units")
    env.seats[1].over = True
    sent = await leaders["Egypt"]("message", to="all", text="Greece is gone.")
    assert sent.startswith("Sent to all: Rome (opus) ·")
    assert "Greece is gone." not in await leaders["Greece"]("list_units")
    assert env.messages[-1]["to"] == "all"
    # The brief lists both, and the seat's own message to all.
    brief = await leaders["Rome"]("get_turn_brief")
    assert 'T1 Egypt to all: "Greece is gone."' in brief
    assert 'T1 you to all: "Greece is gone."' in await leaders["Egypt"]("get_turn_brief")


async def test_a_leader_is_named_by_civ_or_label_and_only_leaders_read_messages(match, action_log):
    env, leaders = match
    assert (await leaders["Rome"]("message", to=" Sonnet ", text="a")).startswith("Sent to Greece (sonnet)")
    assert (await leaders["Rome"]("message", to="egypts", text="b")).startswith("Sent to Egypt ·")
    for to, why in [("Babylon", "Babylon is an AI civilization: it doesn't read messages, diplomacy() deals with it"),
                    ("rome", "Rome is you"), ("Atlantis", "'Atlantis' is no leader in this match")]:
        err = await leaders["Rome"].error("message", to=to, text="hello")
        assert err == f'{why}. You can message Greece (sonnet), Egypt or "all".'
    env.seats[2].over = True
    assert (await leaders["Rome"].error("message", to="Egypt", text="hello")).startswith(
        'Egypt is out of the game. You can message Greece (sonnet) or "all".')
    assert [r["error_code"] for r in action_log() if not r["ok"]] == ["not_a_leader"] * 4


async def test_a_message_has_a_length_a_content_and_a_limit_a_turn(match, action_log):
    env, leaders = match
    rome = leaders["Rome"]
    err = await rome.error("message", to="Greece", text="x" * 281)
    assert err == "the message is 281 characters; the limit is 280. Shorten it and send it again."
    assert "the message is empty" in await rome.error("message", to="Greece", text=" \u200b\n\u202e ")
    sent = await rome("message", to="Greece", text="y" * 280)
    assert sent.endswith(f'"{"y" * 280}"')
    await rome("message", to="Greece", text="See https://example.com/x now")
    assert env.messages[-1]["text"] == "See [link] now"
    await rome("message", to="all", text="three")
    err = await rome.error("message", to="Egypt", text="four")
    assert err.startswith("you have sent 3 messages this turn, the limit; you can send more next turn.")
    assert [r["error_code"] for r in action_log() if not r["ok"]] == ["message_too_long", "empty_message",
                                                                      "message_limit"]
    await end_turn(leaders)
    assert (await rome("message", to="Egypt", text="four")).startswith("Sent to Egypt · message 1 of 3 this turn")


async def test_a_game_with_one_seat_has_no_one_to_message(env, tools, action_log):
    err = await tools.error("message", to="all", text="anyone?")
    assert "you are the only leader in this game, so no one reads messages" in err
    assert action_log()[-1]["error_code"] == "no_one_to_message"


async def test_end_turn_keeps_the_last_note_per_seat_and_turn_even_when_blocked(match, action_log):
    env, leaders = match
    blocked = await leaders["Rome"].error("end_turn", note="Founding the capital.")
    assert blocked.startswith("END TURN BLOCKED")
    rome, greece, egypt = env.seats
    assert rome.notes == {1: "Founding the capital."}
    texts = await end_turn(leaders, Rome="Capital founded, settlers out:\x00 www.example.com", Greece="n" * 200)
    assert all(t.startswith("TURN T1 → T2") for t in texts)
    assert rome.notes == {1: "Capital founded, settlers out: [link]"}
    assert greece.notes == {1: "n" * 139 + "…"} and egypt.notes == {}
    await end_turn(leaders, Rome="Second turn.", Egypt="  ")
    assert rome.notes == {1: "Capital founded, settlers out: [link]", 2: "Second turn."} and egypt.notes == {}
    assert [r["args"].get("note") for r in action_log() if r["seat"] == "Egypt"] == [None, "  "]


async def test_the_live_data_carries_notes_plans_and_messages(match):
    env, leaders = match
    await leaders["Rome"]("plan", text="Expand to 6 cities by T40, then build an army.")
    await leaders["Rome"]("plan", text="Expand to 6 cities, then " + "attack Egypt " * 40)
    await leaders["Greece"]("message", to="Rome", text="Hold the river.")
    await leaders["Egypt"]("message", to="all", text="We want peace.")
    live = (await live_data(env))["live"]
    assert live["min_turn_seconds"] == 0
    assert [{k: m[k] for k in ("from", "to", "text")} for m in live["messages"]] == [
        {"from": "Greece", "to": ["Rome"], "text": "Hold the river."},
        {"from": "Egypt", "to": "all", "text": "We want peace."}]
    rome, greece, _ = live["seats"]
    assert len(rome["plan"]) == 300 and rome["plan"].startswith("Expand to 6 cities, then attack Egypt")
    assert rome["plan"].endswith("…") and rome["plan_turn"] == 1 and rome["note"] is None
    assert (greece["plan"], greece["plan_turn"], greece["note"]) == (None, None, None)

    texts = await end_turn(leaders, Rome="Settled the river.", Egypt="Walls up.")
    assert all(t.startswith("TURN T1 → T2") for t in texts)
    doc = await live_data(env)
    first, second = doc["turns"]
    assert not {"notes", "plans", "messages"} & set(first)
    assert second["notes"] == {"0": "Settled the river.", "2": "Walls up."}
    assert second["plans"] == {"0": rome["plan"]}
    assert second["messages"] == [{"from": 1, "to": [0], "text": "Hold the river."},
                                  {"from": 2, "to": "all", "text": "We want peace."}]
    assert doc["live"]["messages"] == [] and [s["note"] for s in doc["live"]["seats"]] == [None, None, None]
    assert doc["live"]["seats"][0]["plan_turn"] == 1
    later = await live_data(env, since=1)
    assert [t["turn"] for t in later["turns"]] == [2] and later["turns"][0]["notes"] == second["notes"]

    # The html recording embeds the same document.
    res = await env.recording(formats=["html"])
    html = base64.b64decode(res["files"][0]["base64"]).decode()
    embedded = json.loads(html.split("window.OPENCIV_DATA = ", 1)[1].split("; window.OPENCIV_VIDEOS = ", 1)[0])
    turn2 = next(t for t in embedded["turns"] if t["turn"] == 2)
    assert (turn2["notes"], turn2["plans"], turn2["messages"]) == (second["notes"], second["plans"],
                                                                   second["messages"])

    await lobby_game(env, **MATCH)
    assert env.messages == [] and (await live_data(env))["live"]["seats"][0]["plan"] is None


async def test_the_last_seat_ends_the_turn_no_sooner_than_min_turn_seconds(env, monkeypatch):
    monkeypatch.setattr(server, "SEAT_STALL_SECONDS", 0.3)
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 0.05)
    await lobby_game(env, **MATCH, min_turn_seconds=1)
    leaders = {civ: Leader(env, civ) for civ in CIVS}
    started = env.turn_started
    waiting = [asyncio.create_task(leaders[c]("end_turn", skip_idle=True)) for c in ("Rome", "Greece")]
    await asyncio.sleep(0.1)
    last = asyncio.create_task(leaders["Egypt"]("end_turn", skip_idle=True))
    await asyncio.sleep(0.5)
    assert not last.done() and env.turn == 1 and env.seats[2].pacing
    [part] = await asyncio.wait_for(env.data_get(), 1)      # the env is free meanwhile
    live = (await live_data(env))["live"]
    assert part.data["turn"] == 1 and live["min_turn_seconds"] == 1
    assert [s["ended"] for s in live["seats"]] == [True, True, True]    # Egypt has ended it too: it waits
    assert 0.1 <= live["seats"][2]["seconds"] < 0.5
    texts = await asyncio.gather(*waiting, last)
    assert all(t.startswith("TURN T1 → T2") for t in texts) and env.turn_started - started >= 1
    # Egypt made no call for longer than a seat may stall, but it was waiting for the pace, not silent.
    assert "the env ended your turn" not in texts[2] and env.seats[2].auto_ended_turns == 0
    assert not env.seats[2].pacing


async def test_the_pace_waits_for_no_blocked_end_turn_stalled_seat_or_single_seat(env, tools, monkeypatch):
    monkeypatch.setattr(server, "SEAT_STALL_SECONDS", 0.3)
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 0.05)
    await lobby_game(env, **MATCH, min_turn_seconds=1)
    leaders = {civ: Leader(env, civ) for civ in CIVS}
    waiting = [asyncio.create_task(leaders[c]("end_turn", skip_idle=True)) for c in ("Rome", "Greece")]
    await asyncio.sleep(0.05)
    clock = time.monotonic()
    assert (await leaders["Egypt"].error("end_turn")).startswith("END TURN BLOCKED")
    assert time.monotonic() - clock < 0.5
    # Egypt falls silent: the env ends its turn after the stall time, without waiting for the pace.
    started = env.turn_started
    texts = await asyncio.gather(*waiting)
    assert all(t.startswith("TURN T1 → T2") for t in texts) and env.turn_started - started < 1
    assert env.seats[2].auto_ended_turns == 1

    await lobby_game(env, min_turn_seconds=1, seats=[])
    clock = time.monotonic()
    assert (await Leader(env, "Rome")("end_turn", skip_idle=True)).startswith("TURN T1 → T2")
    assert time.monotonic() - clock < 0.9
    with pytest.raises(GameError, match="min_turn_seconds: Input should be less than or equal to 600"):
        await lobby_game(env, min_turn_seconds=601)


async def test_a_new_game_while_the_last_seat_waits_for_the_pace_fails_its_end_turn(env, monkeypatch):
    monkeypatch.setattr(server, "SEAT_WAIT_SECONDS", 3)
    await lobby_game(env, **MATCH, min_turn_seconds=1)
    leaders = {civ: Leader(env, civ) for civ in CIVS}
    waiting = [asyncio.create_task(leaders[c].error("end_turn", skip_idle=True)) for c in CIVS]
    await asyncio.sleep(0.3)
    await lobby_game(env, **MATCH)
    assert all("a new game started" in err for err in await asyncio.gather(*waiting))
    assert env.turn == 1


async def test_an_engine_restart_while_the_last_seat_waits_for_the_pace_has_every_seat_end_the_turn_again(
        env, monkeypatch):
    monkeypatch.setattr(server, "SEAT_WAIT_SECONDS", 3)
    await lobby_game(env, **MATCH, min_turn_seconds=1)
    leaders = {civ: Leader(env, civ) for civ in CIVS}
    waiting = [asyncio.create_task(leaders[c].error("end_turn", skip_idle=True)) for c in CIVS]
    await asyncio.sleep(0.3)
    assert env.seats[2].pacing
    env.bridge._proc.kill()
    await env.bridge._proc.wait()
    assert "!! the engine restarted from the start of turn 1" in await leaders["Rome"]("get_turn_brief")
    again = ("the engine restarted from the start of turn 1; orders given since then are lost; every civilization ends "
             "this turn again; end your turn again.")
    assert all(again in err for err in await asyncio.gather(*waiting))
    assert env.turn == 1 and not any(s.ready or s.pacing for s in env.seats)
    assert all(t.startswith("TURN T1 → T2") for t in await end_turn(leaders))


async def test_a_new_game_sets_the_broadcast_the_live_data_carries(env):
    assert (await live_data(env))["live"]["broadcast"] is None   # no new game yet
    casters = {"model": "openai/gpt-5.6-luna", "analyst": {"name": " Iris ", "voice": "coral"}}
    await lobby_game(env, **MATCH, broadcast={"title": " Battle of the Labs ", "casters": casters})
    assert (await live_data(env, since=99))["live"]["broadcast"] == {
        "title": "Battle of the Labs", "casters": {"model": "openai/gpt-5.6-luna",
                                                   "analyst": {"name": "Iris", "voice": "coral"}}}
    await lobby_game(env, **MATCH, broadcast={"casters": True})
    assert (await live_data(env))["live"]["broadcast"] == {"title": None, "casters": {}}
    await lobby_game(env, **MATCH)   # off unless the new game asks again
    assert (await live_data(env))["live"]["broadcast"] == {"title": None, "casters": None}
    # the broadcast's names for the seats go on the viewer's players
    await lobby_game(env, **MATCH, broadcast={"names": {"opus": "Opus 5.5"}})
    players = {p["civ"]: p for p in (await live_data(env))["players"]}
    assert players["Rome"]["name"] == "Opus 5.5" and "name" not in players["Greece"]
    with pytest.raises(GameError, match="broadcast casters need two different names"):
        await lobby_game(env, broadcast={"casters": {"play_by_play": {"name": "Max"}, "analyst": {"name": "max"}}})


@pytest.mark.parametrize(("value", "error"), [
    ("on", "broadcast must be an object with title and casters"),
    ({"casters": True, "voice": "ash"}, r"broadcast has unknown keys \['voice'\]"),
    ({"title": ""}, "broadcast title must be text of 1-80 characters"),
    ({"title": "x" * 81}, "broadcast title must be text of 1-80 characters"),
    ({"casters": "yes"}, "broadcast casters must be true, false or an object"),
    ({"casters": {"voice": "ash"}}, r"broadcast casters have unknown keys \['voice'\]"),
    ({"casters": {"model": " "}}, "broadcast casters model must be a model name"),
    ({"casters": {"analyst": {"name": "Iris", "pitch": "low"}}}, "broadcast casters analyst must be an object of"),
    ({"casters": {"play_by_play": {"voice": 3}}}, "broadcast casters play_by_play must be an object of"),
    ({"casters": {"analyst": {"model": " "}}}, "broadcast casters analyst must be an object of"),
    ({"casters": {"analyst": {"name": "<b>Iris</b>"}}}, "broadcast casters analyst name must be 1-20 letters"),
    ({"names": ["opus"]}, "broadcast names must map seat labels to names of 1-30 characters"),
    ({"names": {"opus": ""}}, "broadcast names must map seat labels to names of 1-30 characters"),
    ({"names": {"opus": "x" * 31}}, "broadcast names must map seat labels to names of 1-30 characters"),
    ({"banners": []}, "broadcast banners must be a list of 1-8 banners"),
    ({"banners": [{"text": "ad"}] * 9}, "broadcast banners must be a list of 1-8 banners"),
    ({"banners": [{"text": "ad", "link": "x"}]}, "a broadcast banner is an object of text, logos and theme"),
    ({"banners": [{"text": " "}]}, "a broadcast banner's text must be 1-100 characters"),
    ({"banners": [{"text": "x" * 101}]}, "a broadcast banner's text must be 1-100 characters"),
    ({"banners": [{"text": "Powered by {modal}"}]}, r"places logos it has no source for: \['modal'\]"),
    ({"banners": [{"text": "Powered by Modal", "logos": {"modal": "https://example.com/m.png"}}]},
     r"has logos it never places \(\{name\} in its text\): \['modal'\]"),
    ({"banners": [{"text": "{Modal}", "logos": {"Modal": "https://example.com/m.png"}}]},
     "logos map names .* to image sources"),
    ({"banners": [{"text": "ad", "theme": "neon"}]}, "a broadcast banner's theme is one of dark, light"),
    ({"banners": [{"text": "{m}", "logos": {"m": "http://example.com/m.png"}}]},
     "banner logo 'm' is an https:// URL or a data:image/...;base64, URI, which the env fetches"),
    ({"banners": [{"text": "{m}", "logos": {"m": "/m.png"}}]}, "banner logo 'm' is an https:// URL"),
    ({"banners": [{"text": "{m}", "logos": {"m": "m.png"}}]}, "banner logo 'm' is an https:// URL"),
    ({"banners": [{"text": "{m}", "logos": {"m": "data:text/html;base64,PGI+"}}]},
     "banner logo 'm' is not a base64 data: URI of a PNG, JPEG, WebP, GIF or SVG image"),
])
def test_broadcast_settings_reject_what_the_stream_cannot_use(value, error):
    with pytest.raises(ValueError, match=error):
        broadcast.settings(value)


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5E"
                       "rkJggg==")
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2"><rect width="2"/></svg>'


def data_uri(body: bytes, kind: str = "image/png") -> str:
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"


class Reply:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def read(self, n): return self.body[:n]


def serving(monkeypatch, bodies: dict[str, bytes]) -> list:
    """The web as broadcast.inline fetches it: `bodies` by URL, every other one missing. The fetches, in order."""
    fetched = []

    def urlopen(request, timeout):
        fetched.append((request.full_url, timeout))
        if request.full_url not in bodies:
            raise urllib.error.URLError("no such host")
        return Reply(bodies[request.full_url])

    monkeypatch.setattr(broadcast.urllib.request, "urlopen", urlopen)
    return fetched


def test_broadcast_banners_inline_each_logo_from_a_url_or_a_data_uri(monkeypatch):
    fetched = serving(monkeypatch, {"https://example.com/modal.png": PNG, "https://example.com/icon.svg": SVG,
                                    "https://example.com/page.png": b"<html>no</html>",
                                    "https://example.com/big.png": PNG + b"0" * broadcast.LOGO_BYTES})
    show = {"title": "Five-way war", "banners": [
        {"text": "Powered by {modal} Modal with {agentenv} AgentEnv Framework", "theme": "light",
         "logos": {"modal": "https://example.com/modal.png", "agentenv": "https://example.com/icon.svg"}},
        {"text": "Built by {scale}", "logos": {"scale": data_uri(PNG)}},
        {"text": "Every match is open source"}]}
    assert broadcast.settings(show)["banners"][2] == {"text": "Every match is open source", "logos": {},
                                                      "theme": "dark"}
    inlined = broadcast.inline(show)
    assert fetched == [("https://example.com/modal.png", broadcast.LOGO_SECONDS),
                       ("https://example.com/icon.svg", broadcast.LOGO_SECONDS)]
    assert inlined["banners"][0]["logos"] == {"modal": data_uri(PNG), "agentenv": data_uri(SVG, "image/svg+xml")}
    assert inlined["banners"][1] == show["banners"][1] and inlined["title"] == "Five-way war"
    assert broadcast.settings(inlined, inlined=True)["banners"][0]["theme"] == "light"
    with pytest.raises(ValueError, match="reaches the game as a data: URI"):
        broadcast.settings(show, inlined=True)
    for url, error in (("https://example.com/page.png", "is not a PNG, JPEG, WebP, GIF or SVG image"),
                       ("https://example.com/big.png", "is over 524,288 bytes"),
                       ("https://example.com/gone.png", "could not be read from")):
        with pytest.raises(ValueError, match=error):
            broadcast.inline({"banners": [{"text": "{bad}", "logos": {"bad": url}}]})


async def test_the_live_data_links_each_banner_logo_and_the_env_serves_it(env, monkeypatch):
    banners = [{"text": "Powered by {modal} Modal", "theme": "light", "logos": {"modal": data_uri(PNG)}},
               {"text": "{mark} AgentEnv", "logos": {"mark": data_uri(SVG, "image/svg+xml")}}]
    await lobby_game(env, **MATCH, broadcast={"title": "Ads", "banners": banners})
    assert (await live_data(env))["live"]["broadcast"]["banners"] == [
        {"text": "Powered by {modal} Modal", "theme": "light", "logos": {"modal": "live/logo/0/modal"}},
        {"text": "{mark} AgentEnv", "theme": "dark", "logos": {"mark": "live/logo/1/mark"}}]

    async def get(banner: int, name: str):
        return await env._live_logo(SimpleNamespace(path_params={"banner": banner, "name": name}))

    png, svg = await get(0, "modal"), await get(1, "mark")
    assert (png.body, png.media_type, svg.body, svg.media_type) == (PNG, "image/png", SVG, "image/svg+xml")
    assert svg.headers["content-security-policy"].startswith("default-src 'none'")
    assert [(await get(b, n)).status_code for b, n in ((0, "mark"), (2, "modal"), (-1, "modal"))] == [404] * 3
    serving(monkeypatch, {"https://example.com/m.png": PNG})    # the env fetches a logo as the lobby closes
    await lobby_game(env, **MATCH, broadcast={"banners": [{"text": "{m}", "logos": {"m": "https://example.com/m.png"}}]})
    assert (await live_data(env))["live"]["broadcast"]["banners"][0]["logos"] == {"m": "live/logo/0/m"}
    assert (await get(0, "m")).body == PNG
    with pytest.raises(ValueError, match="banner logo 'm' could not be read from https://example.com/gone.png"):
        await lobby_game(env, **MATCH, broadcast={"banners": [{"text": "{m}",
                                                               "logos": {"m": "https://example.com/gone.png"}}]})


def test_broadcast_settings_turn_the_casters_on_or_off():
    assert broadcast.settings(None) is None
    assert broadcast.settings({}) == {"title": None, "casters": None}
    assert broadcast.settings({"casters": False}) == broadcast.settings({"casters": None}) == {
        "title": None, "casters": None}
    assert broadcast.settings({"title": "Showmatch", "casters": {"tts_model": "openai/gpt-4o-mini-tts",
                                                                 "play_by_play": {"name": "Rex O'Neil"}}}) == {
        "title": "Showmatch", "casters": {"tts_model": "openai/gpt-4o-mini-tts",
                                          "play_by_play": {"name": "Rex O'Neil"}}}
    # each caster may have its own model: a fast one for the play-by-play, a stronger one for the analysis
    assert broadcast.settings({"casters": {"model": "anthropic/claude-haiku-4-5",
                                           "analyst": {"model": " anthropic/claude-sonnet-5-5 "}}})["casters"] == {
        "model": "anthropic/claude-haiku-4-5", "analyst": {"model": "anthropic/claude-sonnet-5-5"}}
    # the seats' names on screen and in the casters' mouths: by label
    assert broadcast.settings({"names": {" opus ": " Opus 5.5 ", "sol": "GPT-6 Sol"}}) == {
        "title": None, "casters": None, "names": {"opus": "Opus 5.5", "sol": "GPT-6 Sol"}}
