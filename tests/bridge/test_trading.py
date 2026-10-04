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

    # An offer a trade has made impossible is not shown, and accepting it says why.
    sold = launch()
    sold.call("load", path=str(tmp_path / "a" / "autosave.json"))
    wanted = offer["you_give"]["techs"]
    assert wanted
    sold.call("propose_trade", civ=civ, give_techs=wanted)
    assert civ_named(sold, civ)["trade_offered"] is None
    assert next(r for r in sold.call("state")["rivals"] if r["civ"] == civ)["trade_offered"] is None
    err = sold.error("accept_trade", civ=civ)
    assert err == {"code": "offer_changed", "message": f"{civ} already knows {wanted[0]}."}
    assert sold.call("decline_trade", civ=civ)["message"] == (
        f"Declined {civ}'s offer, which could no longer be made: {civ} already knows {wanted[0]}.")


def test_an_offer_made_while_end_turn_plays_on_stands_until_the_agent_can_answer(launch):  # noqa: F811
    """An end_turn(until_attention) of several turns: an AI offer made on one of its earlier turns stands until the turn
    it stops on, as its event says."""
    b = launch()
    start(b)
    found_capital(b)
    b.call("set_production", city="c1", item="Warrior", then=["Warrior"] * 9)
    for _ in range(30):
        for u in b.call("state")["units"]:
            if u.get("needs_orders") and not b.send("unit_order", unit=u["id"], order="fortify")["ok"]:
                b.call("unit_order", unit=u["id"], order="sentry")
        res = b.call("end_turn", skip_idle=True, until_attention=True, max_turns=20)
        early = [e for e in res["events"] if e["kind"] == "trade_offered" and e["turn"] < res["turn"] - 1]
        if early:
            break
    else:
        pytest.fail("no AI offered a trade on an earlier turn of an end_turn")
    civ = early[0]["text"].split(" offers ", 1)[0]
    assert f"until the end of turn {res['turn']}. Accept" in early[0]["text"]
    assert civ_named(b, civ)["trade_offered"]["until_turn"] == res["turn"]
    assert b.call("accept_trade", civ=civ)["message"].startswith(f"Traded with {civ}:")


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
    # Gold asked as well: the balanced call asks for less of it before it gives more.
    asked = b.call("quote_trade", civ=civ, get_techs=[tech], get_gold=min(10, them["gold"]))
    assert asked["gold_to_balance"] == need + min(10, them["gold"]) and asked["suggest"] == quote["suggest"]

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
    # The world snapshot has it for the viewer, once: who gave what, in the snapshot's player indices.
    world = b.call("world")
    index = {p["civ"]: p["index"] for p in world["players"]}
    [made] = world["trades"]
    assert {k: made[k] for k in ("turn", "a", "b", "a_gave", "b_gave")} == {
        "turn": world["turn"], "a": index["Rome"], "b": index[civ], "a_gave": f"{need} gold", "b_gave": tech}

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
    [told] = [t for k, t in kinds(events["Greece"]) if k == "trade_offered"]
    assert told.startswith(f"Rome offers {tech} (worth ") and told.endswith(
        f" to you) for 10 gold (worth 10 to you), until the end of turn {turn + 1}. "
        'Accept with diplomacy(action="accept_trade", civ="Rome").')
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
    # An offer answered in the turn it was made is not told when the turn ends; one replaced is told as it is now.
    r.call("propose_trade", seat="Greece", civ="Rome", give_gold=2)
    r.call("accept_trade", seat="Rome", civ="Greece")
    r.call("propose_trade", seat="Rome", civ="Greece", give_gold=2)
    r.call("propose_trade", seat="Rome", civ="Greece", give_gold=1)
    events = end_round(r)
    assert not any(k == "trade_offered" for k, _ in kinds(events["Rome"]))
    assert [t for k, t in kinds(events["Greece"]) if k == "trade_offered"] == [
        f"Rome offers 1 gold (worth 1 to you) for nothing (worth 0 to you), until the end of turn {turn + 3}. "
        'Accept with diplomacy(action="accept_trade", civ="Rome").']

    # Not accepted by the end of the next turn, an offer lapses; a declined one goes at once.
    end_round(r)
    assert "lapsed" in r.error("accept_trade", seat="Greece", civ="Rome")["message"]
    r.call("propose_trade", seat="Rome", civ="Greece", give_gold=1)
    assert r.call("decline_trade", seat="Greece", civ="Rome")["message"] == "Declined Rome's offer."
    assert ("trade_declined", "Greece declined your offer of 1 gold for nothing.") in kinds(end_round(r)["Rome"])


def test_offers_end_with_the_game(launch, tmp_path):  # noqa: F811
    """A game that ends (here at its turn limit) ends the offers standing in it: none is told, listed or accepted."""
    b = seat_game(launch, "--autosave", str(tmp_path / "a"))
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
    end_round(b)
    path = tmp_path / "a" / "autosave.json"
    tech = meet(path, "Rome", "Greece", 50)
    save = json.loads(path.read_text())
    save["bridge"]["turn_limit"] = save["game"]["turnNumber"] + 1
    path.write_text(json.dumps(save))
    r = launch("--autosave", str(tmp_path / "b"))
    r.call("load", path=str(path))
    r.call("propose_trade", seat="Rome", civ="Greece", give_techs=[tech], get_gold=10)
    assert civ_named(r, "Rome", "Greece")["trade_offered"]
    events = end_round(r)
    assert r.call("state", seat="Greece")["game_over"]
    assert not any(k == "trade_offered" for k, _ in kinds(events["Greece"]))
    assert civ_named(r, "Rome", "Greece")["trade_offered"] is None
    assert r.error("accept_trade", seat="Greece", civ="Rome")["code"] == "game_over"


def test_a_tech_got_in_a_trade_replaces_an_obsolete_unit_in_production(launch, tmp_path):  # noqa: F811
    """Feudalism got mid-turn makes the Spearman a city is building obsolete (a Pikeman can be built there, with
    Iron): the city builds the Pikeman instead, keeping its shields, rather than finishing an obsolete unit."""
    b = seat_game(launch, "--autosave", str(tmp_path / "a"))
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
    end_round(b)
    path = tmp_path / "a" / "autosave.json"
    meet(path, "Rome", "Greece", 50)
    save = json.loads(path.read_text())
    g = save["game"]
    players = {p["civilization"]: p for p in g["players"]}
    players["Rome"]["knownTechs"] = sorted(set(players["Rome"]["knownTechs"]) | {"tech-1", "tech-8"})
    players["Greece"]["knownTechs"] = sorted(set(players["Greece"]["knownTechs"]) | {"tech-23"})
    city = next(c for c in g["cities"] if c["owner"] == players["Rome"]["id"])
    city["producible"], city["producibleType"], city["shieldsStored"] = "Spearman", "unit", 9
    at = city["location"]
    next(t for t in g["map"]["tiles"] if (t["x"], t["y"]) == (at["x"], at["y"]))["resource"] = "Iron"
    path.write_text(json.dumps(save))
    r = launch()
    r.call("load", path=str(path))
    assert r.call("city", seat="Rome", city="c1")["producing"] == "Spearman"
    r.call("propose_trade", seat="Greece", civ="Rome", give_techs=["Feudalism"])
    res = r.call("accept_trade", seat="Rome", civ="Greece")
    name = city["name"]
    assert res["message"].endswith(f"{name} now builds a Pikeman: the Spearman it was building is obsolete.")
    c1 = r.call("city", seat="Rome", city="c1")
    assert (c1["producing"], c1["production_stored"]) == ("Pikeman", 9)


def test_offers_told_in_the_turn_a_victory_ends_the_game_are_not_told(launch, tmp_path):  # noqa: F811
    """The turn that ends in a victory (here Rome's capital passes 20,000 culture) delivers no offer: it would end
    with the game at once."""
    b = seat_game(launch, "--autosave", str(tmp_path / "a"))
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
    end_round(b)
    path = tmp_path / "a" / "autosave.json"
    tech = meet(path, "Rome", "Greece", 50)
    save = json.loads(path.read_text())
    rome = next(p for p in save["game"]["players"] if p["civilization"] == "Rome")["id"]
    next(c for c in save["game"]["cities"] if c["owner"] == rome)["perPlayerCulture"][rome] = 19_999
    path.write_text(json.dumps(save))
    r = launch()
    r.call("load", path=str(path))
    r.call("propose_trade", seat="Rome", civ="Greece", give_techs=[tech], get_gold=10)
    events = end_round(r)
    assert r.call("state", seat="Greece")["victory"]["kind"] == "culture"
    assert [k for k, _ in kinds(events["Greece"])][-1] == "victory"
    assert not any(k == "trade_offered" for k, _ in kinds(events["Greece"]))
