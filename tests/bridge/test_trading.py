"""Trading techs and gold over the protocol (docs/protocol.md, Trades): AI offers, quotes and proposals judged by the
AI's own values, trades between seats, and research after a trade (patch 0017). Needs the built bridge, like
test_protocol."""

from __future__ import annotations

import json

import pytest
from test_protocol import (
    SEATS,
    SEED,
    Bridge,
    end_round,
    found_capital,
    kinds,
    launch,  # noqa: F401  (the fixture)
    seat_game,
)


def start(b: Bridge) -> None:
    b.call("new_game", seed=SEED, size="Small", opponents=5, turn_limit=200)


def offered(b: Bridge) -> tuple[int, dict, dict]:
    """Rome founds its capital and ends turns until an AI offers a trade (Zululand, on turn 21 when written); returns
    the turn, the event and the offering civ as diplomacy shows it."""
    start(b)
    found_capital(b)
    for _ in range(60):
        res = b.call("end_turn", skip_idle=True)
        if event := next((e for e in res["events"] if e["kind"] == "trade_offered"), None):
            civ = next(c for c in b.call("diplomacy")["civs"] if c["trade_offered"])
            return res["turn"], event, civ
    pytest.fail("no AI offered a trade")


def civ_named(b: Bridge, name: str, seat: str | None = None) -> dict:
    return next(c for c in b.call("diplomacy", **({"seat": seat} if seat else {}))["civs"] if c["civ"] == name)


def test_a_traded_tech_keeps_research_progress(launch):  # noqa: F811
    """patches/0017: a tech got in a trade cleared the current research and its beakers, whatever it was."""
    b = launch()
    _, _, them = offered(b)
    research = b.call("state")["research"]
    assert research["beakers"] > 0
    tech = next(t["name"] for t in them["techs_for_you"] if t["name"] != research["current"])
    quote = b.call("quote_trade", civ=them["civ"], get_techs=[tech])
    res = b.call("propose_trade", civ=them["civ"], get_techs=[tech], give_gold=quote["gold_to_balance"])
    assert tech in b.call("state")["known_techs"]
    assert {k: res["research"][k] for k in ("current", "beakers", "queue")} == {
        k: research[k] for k in ("current", "beakers", "queue")}


def test_ai_trade_offers_stand_for_a_turn(launch, tmp_path):  # noqa: F811
    b = launch("--autosave", str(tmp_path / "a"))
    turn, event, them = offered(b)
    civ, offer = them["civ"], them["trade_offered"]
    assert offer["until_turn"] == turn and set(offer) == {"you_get", "you_give", "you_value_get", "you_value_give",
                                                          "until_turn"}
    assert offer["you_get"]["techs"] and f"(worth {offer['you_value_get']} to you)" in event["text"]
    assert event["text"].endswith(f'Accept with diplomacy(action="accept_trade", civ="{civ}").')
    assert next(r for r in b.call("state")["rivals"] if r["civ"] == civ)["trade_offered"] == offer
    assert {t["name"] for t in them["techs_for_you"]} >= set(offer["you_get"]["techs"])

    # The autosave keeps the offer: a restored game shows it and accepts it the same way.
    restored = launch()
    restored.call("load", path=str(tmp_path / "a" / "autosave.json"))
    assert civ_named(restored, civ) == them

    state = b.call("state")
    gold, research = state["gold"], state["research"]
    res = b.call("accept_trade", civ=civ)
    assert restored.call("accept_trade", civ=civ) == res
    # The offer held the tech being researched: the research moved on, and the engine's pick waits for the agent.
    assert research["current"] in offer["you_get"]["techs"] and res["research"]["current"] != research["current"]
    assert res["research"]["beakers"] == 0 and res["research"]["source"] == "engine"
    assert "choose_research" in [x["kind"] for x in b.call("state")["blockers"]]
    paid = offer["you_get"]["gold"] - offer["you_give"]["gold"]
    assert res["gold"] == gold + paid and res["civ"]["gold"] == them["gold"] - paid
    assert set(offer["you_get"]["techs"]) <= set(b.call("state")["known_techs"])
    assert res["civ"]["trade_offered"] is None and not set(offer["you_give"]["techs"]) & {
        t["name"] for t in res["civ"]["techs_for_them"]}
    assert b.error("accept_trade", civ=civ)["code"] == "no_offer"

    # Not taken up, it lapses when the turn ends.
    lapsed = launch()
    lapsed.call("load", path=str(tmp_path / "a" / "autosave.json"))
    lapsed.call("end_turn", skip_idle=True)
    err = lapsed.error("accept_trade", civ=civ)
    assert err["code"] == "no_offer" and f"lapsed at the end of turn {turn}" in err["message"]
    assert civ_named(lapsed, civ)["trade_offered"] is None


def test_quote_then_propose_trade_with_an_ai(launch):  # noqa: F811
    b = launch()
    _, _, them = offered(b)
    civ, tech = them["civ"], them["techs_for_you"][0]["name"]
    gold = b.call("state")["gold"]
    quote = b.call("quote_trade", civ=civ, get_techs=[tech])
    need = quote["gold_to_balance"]
    assert not quote["accepts"] and need == quote["they_value_get"] > 0 and quote["they_value_give"] == 0
    assert quote["suggest"] == f'diplomacy(action="propose_trade", civ="{civ}", give_gold={need}, get_techs=["{tech}"])'
    assert b.call("quote_trade", civ=civ, get_techs=[tech], give_gold=need)["accepts"]

    refused = b.error("propose_trade", civ=civ, get_techs=[tech], give_gold=need - 1)
    assert refused["code"] == "refused" and refused["suggest"] == quote["suggest"]
    assert b.call("state")["gold"] == gold and tech not in b.call("state")["known_techs"]

    errors = {
        "unknown_tech": [dict(get_techs=["Writing"]), dict(get_techs=["Bogus"]), dict(give_techs=[tech])],
        "not_enough_gold": [dict(get_techs=[tech], give_gold=gold + 1),
                            dict(get_techs=[tech], get_gold=them["gold"] + 1)],
        "bad_args": [dict(give_gold=1), dict(), dict(get_techs=tech)],
    }
    for code, cases in errors.items():
        for args in cases:
            assert b.error("propose_trade", civ=civ, **args)["code"] == code, args
    unknown = b.error("quote_trade", civ=civ, get_techs=["Bogus"])
    assert unknown["alternatives"] == [t["name"] for t in them["techs_for_you"]]
    assert b.error("propose_trade", civ="Atlantis", get_techs=[tech])["code"] == "unknown_civ"

    res = b.call("propose_trade", civ=civ, get_techs=[tech], give_gold=need)
    assert res["message"] == f"Traded with {civ}: you gave {need} gold and got {tech}."
    assert res["gold"] == gold - need and res["civ"]["gold"] == them["gold"] + need
    assert tech in b.call("state")["known_techs"] and tech not in [t["name"] for t in res["civ"]["techs_for_you"]]

    b.call("declare_war", civ=civ)
    assert civ_named(b, civ)["techs_for_you"] is None
    assert b.error("quote_trade", civ=civ, give_gold=1)["code"] == "not_at_peace"
    assert b.error("propose_trade", civ=civ, get_techs=["Bogus"])["code"] == "not_at_peace"
    assert b.error("accept_trade", civ=civ)["code"] == "no_offer"


def test_a_game_with_trades_replays_the_same(launch):  # noqa: F811
    """Trades draw nothing from the game's random numbers: the same commands give the same game."""
    worlds = []
    for _ in range(2):
        b = launch()
        _, _, them = offered(b)
        b.call("accept_trade", civ=them["civ"])
        for _ in range(3):
            b.call("end_turn", skip_idle=True)
        worlds.append(b.call("world"))
    assert worlds[0] == worlds[1]


@pytest.mark.parametrize("policy", ["found_capital", "engine_ai"])
def test_autoplay_declines_every_ai_offer(launch, policy):  # noqa: F811
    """Baselines stay as they were before trading: autoplay answers each AI offer with a decline."""
    b = launch()
    start(b)
    res = b.call("autoplay", turns=60, policy=policy)
    assert res["trades_declined"] > 0
    assert all(c["trade_offered"] is None for c in b.call("diplomacy")["civs"])
    assert all(r.get("trade_offered") is None for r in b.call("state")["rivals"])


def meet(path, a: str, b: str, gold: int) -> str:
    """Edits an autosave so seats `a` and `b` have met, at peace, `b` holds `gold`, and `a` knows a tech `b` lacks."""
    save = json.loads(path.read_text())
    players = {p["civilization"]: p for p in save["game"]["players"]}
    pa, pb = players[a], players[b]
    peace = {"warDeclarationCount": 0, "warDeclarationWithRoPActiveCount": 0, "wasSneakAttacked": False,
             "refuseContactUntilTurn": -1,
             "multiTurnDeals": [{"dealType": "diplomaticAgreement", "dealSubType": "peace", "dealDetails": "exchange",
                                 "goldPerTurn": 0, "dealDuration": 0, "turnStartDeal": 0, "turnEndDeal": 0}],
             "declaredWarWithActiveRightOfPassage": False,
             "warStartedOnTurn": 0, "unitsLostToThem": 0, "citiesLostToThem": 0, "treatiesBrokenByThem": 0}
    pa["playerRelationships"] = {**(pa.get("playerRelationships") or {}), pb["id"]: dict(peace)}
    pb["playerRelationships"] = {**(pb.get("playerRelationships") or {}), pa["id"]: dict(peace)}
    pb["gold"] = gold
    extra = min((t for t in save["game"]["techs"] if t["id"] not in pa["knownTechs"] + pb["knownTechs"]),
                key=lambda t: t["cost"])
    pa["knownTechs"].append(extra["id"])
    path.write_text(json.dumps(save))
    return extra["name"]


def test_seats_trade_when_both_agree(launch, tmp_path):  # noqa: F811
    b = seat_game(launch, "--autosave", str(tmp_path / "a"))
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
    end_round(b)
    path = tmp_path / "a" / "autosave.json"
    tech = meet(path, "Rome", "Greece", 50)
    r = launch("--autosave", str(tmp_path / "b"))
    r.call("load", path=str(path))
    turn = r.call("state")["turn"]
    greece = civ_named(r, "Greece", "Rome")
    assert greece["agent"] and tech in [t["name"] for t in greece["techs_for_them"]] and greece["gold"] == 50
    assert r.call("quote_trade", seat="Rome", civ="Greece", give_techs=[tech], get_gold=10)["accepts"] is None
    assert r.error("propose_trade", seat="Rome", civ="Greece", get_gold=51)["code"] == "not_enough_gold"
    assert r.error("propose_trade", seat="Rome", civ="Egypt", give_gold=1)["code"] == "unknown_civ"

    rome_gold = r.call("state", seat="Rome")["gold"]
    res = r.call("propose_trade", seat="Rome", civ="Greece", give_techs=[tech], get_gold=10)
    assert res["message"] == (f"Offered Greece {tech} for 10 gold; the trade is made if Greece accepts it before turn "
                              f"{turn + 2}.")
    assert tech not in r.call("state", seat="Greece")["known_techs"]
    assert r.call("state", seat="Rome")["gold"] == rome_gold
    rome = civ_named(r, "Rome", "Greece")
    assert {k: rome["trade_offered"][k] for k in ("you_get", "you_give", "until_turn")} == {
        "you_get": {"techs": [tech], "gold": 0}, "you_give": {"techs": [], "gold": 10}, "until_turn": turn + 1}
    assert civ_named(r, "Greece", "Rome")["you_offered_trade"]["you_give"] == {"techs": [tech], "gold": 0}
    assert r.error("accept_trade", seat="Rome", civ="Greece")["code"] == "no_offer"

    events = end_round(r)
    assert [k for k, _ in kinds(events["Greece"])].count("trade_offered") == 1
    assert not any(k.startswith("trade") for k, _ in kinds(events["Rome"]) + kinds(events["Egypt"]))
    # The autosave keeps the seat's offer.
    restored = launch()
    restored.call("load", path=str(tmp_path / "b" / "autosave.json"))
    assert civ_named(restored, "Rome", "Greece") == civ_named(r, "Rome", "Greece")

    gold = {civ: r.call("state", seat=civ)["gold"] for civ in ("Rome", "Greece")}
    res = r.call("accept_trade", seat="Greece", civ="Rome")
    assert res["message"] == f"Traded with Rome: you gave 10 gold and got {tech}."
    assert tech in r.call("state", seat="Greece")["known_techs"] and res["gold"] == gold["Greece"] - 10
    assert r.call("state", seat="Rome")["gold"] == gold["Rome"] + 10
    assert r.error("accept_trade", seat="Greece", civ="Rome")["code"] == "no_offer"
    events = end_round(r)
    assert ("trade_signed", f"Greece accepted your trade: you got 10 gold for {tech}.") in kinds(events["Rome"])
    assert not any(k.startswith("trade") for k, _ in kinds(events["Egypt"]) + kinds(events["Greece"]))

    # The same trade proposed back signs it, once.
    gold = {civ: r.call("state", seat=civ)["gold"] for civ in ("Rome", "Greece")}
    r.call("propose_trade", seat="Rome", civ="Greece", give_gold=5)
    assert r.call("propose_trade", seat="Greece", civ="Rome", get_gold=5)["message"] == (
        "Traded with Rome: you gave nothing and got 5 gold.")
    assert {civ: r.call("state", seat=civ)["gold"] for civ in ("Rome", "Greece")} == {
        "Rome": gold["Rome"] - 5, "Greece": gold["Greece"] + 5}
    for seat, civ in (("Rome", "Greece"), ("Greece", "Rome")):
        assert r.error("accept_trade", seat=seat, civ=civ)["code"] == "no_offer"

    # Not accepted by the end of the next turn, an offer lapses; a declined one goes at once.
    r.call("propose_trade", seat="Rome", civ="Greece", give_gold=1)
    end_round(r)
    end_round(r)
    assert "lapsed" in r.error("accept_trade", seat="Greece", civ="Rome")["message"]
    r.call("propose_trade", seat="Rome", civ="Greece", give_gold=1)
    assert r.call("decline_trade", seat="Greece", civ="Rome")["message"] == "Declined Rome's offer."
    assert ("trade_declined", "Greece declined your offer of 1 gold for nothing.") in kinds(end_round(r)["Rome"])
