"""Trading techs and gold through the diplomacy tool (against the fake bridge), and how trades render."""

import pytest

from agentenv_openciv3 import render

pytestmark = pytest.mark.anyio

OFFER = {"you_get": {"techs": ["Masonry"], "gold": 5}, "you_give": {"techs": ["Bronze Working"], "gold": 0},
         "you_value_get": 29, "you_value_give": 16, "until_turn": 4}


async def meet_greece(env, tools, **game):
    await tools("diplomacy")    # starts the game
    await env.bridge.call("_game", met=True, **game)
    env.seat.cache = None


async def test_quote_then_propose_a_trade(env, tools):
    await meet_greece(env, tools)
    status = await tools("diplomacy")
    assert ("gold 60 · has Masonry 24/24, Ceremonial Burial 16/16; lacks Bronze Working 16/16 (worth to you/them)"
            in status)
    assert 'Trade: diplomacy(action="quote_trade", civ="...", get_techs=["..."], give_gold=0)' in status
    text = await tools("diplomacy", action="quote_trade", civ="Greece", get_techs=["Masonry"])
    assert text.startswith("Greece: you give nothing (worth 0 to you, 0 to them) · you get Masonry (worth 24 to you, "
                           "24 to them) · Greece refuses: it wants 24 gold more")
    err = await tools.error("diplomacy", action="propose_trade", civ="Greece", get_techs=["Masonry"])
    assert "Greece wants 24 gold more" in err
    text = await tools("diplomacy", action="quote_trade", civ="Greece", get_techs=["Masonry"],
                       give_techs=["Bronze Working"], give_gold=8)
    assert 'Greece accepts → diplomacy(action="propose_trade", civ="Greece", give_techs=["Bronze Working"], ' in text
    text = await tools("diplomacy", action="propose_trade", civ="Greece", get_techs=["Masonry"],
                       give_techs=["Bronze Working"], give_gold=8)
    assert text.startswith("Traded with Greece.") and "gold 68" in text
    assert "Masonry" in (await env.bridge.call("state"))["known_techs"]


async def test_a_misspelt_tech_is_read_as_the_tradeable_one(env, tools):
    await meet_greece(env, tools, gold=100)
    text = await tools("diplomacy", action="propose_trade", civ="greece", get_techs=["masonry"], give_gold=24)
    assert text.startswith("(read 'masonry' as 'Masonry') (read 'greece' as 'Greece') Traded with Greece.")
    err = await tools.error("diplomacy", action="propose_trade", civ="Greece", get_techs=["Writing"], give_gold=24)
    assert "Writing cannot be traded that way" in err and "Ceremonial Burial" in err


async def test_an_offer_shows_in_the_brief_and_is_accepted(env, tools):
    await meet_greece(env, tools, trade_offer=OFFER)
    brief = await tools("get_turn_brief")
    assert ('TRADE Greece offers Masonry, 5 gold (worth 29 to you) for Bronze Working (worth 16 to you), until T4: '
            'accept with diplomacy(action="accept_trade", civ="Greece")') in brief
    assert "OFFERS Masonry, 5 gold (worth 29 to you)" in await tools("diplomacy")
    text = await tools("diplomacy", action="accept_trade", civ="Greece")
    assert text.startswith("Traded with Greece.") and "TRADE" not in await tools("get_turn_brief")
    assert "no trade offer standing" in await tools.error("diplomacy", action="accept_trade", civ="Greece")
    await env.bridge.call("_game", trade_offer=OFFER)
    env.seat.cache = None
    assert (await tools("diplomacy", action="decline_trade", civ="Greece")).startswith("Declined Greece's offer.")


async def test_the_play_api_forwards_trade_args(env, tools):
    await meet_greece(env, tools)
    body, mutating = env._play_action("diplomacy", {"action": "quote_trade", "civ": "Greece", "gold": 0,
                                                    "give_techs": None, "give_gold": 0, "get_techs": ["Masonry"],
                                                    "get_gold": 0})
    assert not mutating
    res = await body()
    assert res["result"]["accepts"] is False and res["result"]["gold_to_balance"] == 24


def test_trade_lines():
    q = {"civ": "Zululand", "you_give": {"techs": [], "gold": 48}, "you_get": {"techs": ["Bronze Working"], "gold": 0},
         "you_value_give": 48, "you_value_get": 54, "they_value_give": 48, "they_value_get": 48, "accepts": True,
         "gold_to_balance": 0, "gold_they_would_add": 0, "suggest": None}
    assert render.trade_quote(q) == (
        "Zululand: you give 48 gold (worth 48 to you, 48 to them) · you get Bronze Working (worth 54 to you, 48 to "
        'them) · Zululand accepts → diplomacy(action="propose_trade", civ="Zululand", give_gold=48, '
        'get_techs=["Bronze Working"])')
    assert render.trade_quote({**q, "accepts": None}).endswith("another agent decides: propose_trade offers it")
    refused = {**q, "accepts": False, "gold_to_balance": 30, "suggest": None}
    assert render.trade_quote(refused).endswith("Zululand refuses: it wants 30 gold more, more than you have")
    civ = {"civ": "Greece", "agent": True, "at_war": False, "score": {"total": 1, "cities": 1, "techs": 2},
           "gold": 7, "techs_for_you": [], "techs_for_them": [], "trade_offered": None,
           "you_offered_trade": {**OFFER, "you_get": OFFER["you_give"], "you_give": OFFER["you_get"]}}
    line = render.civ_line(civ)
    assert line.endswith("gold 7 · you offered Masonry, 5 gold for Bronze Working, until T4")
    assert "AT WAR" not in line and "has " not in line
    assert "gold" not in render.civ_line({**civ, "at_war": True, "you_offered_trade": None}).split("·")[-1]


def test_every_auto_entry_is_listed():
    autos = [{"kind": "research_picked", "text": f"pick #{i}"} for i in range(5)]
    assert render.auto_lines(autos) == [f"auto: pick #{i}" for i in range(5)]


def test_the_brief_lists_an_offer_event_without_its_call():
    e = {"turn": 4, "kind": "trade_offered", "text": 'Greece offers Masonry (worth 24 to you) for 30 gold (worth 30 to '
                                                    'you), until the end of turn 4. Accept with diplomacy(...).'}
    assert render.short_event(e)["text"].endswith("until the end of turn 4.")


def test_trade_events_come_first():
    assert {"trade_offered", "trade_signed"} <= render.FIRST and "trade_offered" not in render.URGENT
