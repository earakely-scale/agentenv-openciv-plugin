"""Later eras in the env (patches 0018 and 0019): upgrades on unit lines and in the brief, and the viewer's art for the
units the standalone ruleset now keeps."""

import re
from pathlib import Path

from test_render import brief_of, state

from agentenv_openciv3 import render, server, webart

ROOT = Path(__file__).resolve().parents[2]


def with_upgrades(s: dict) -> dict:
    """Three units of the state can upgrade in a city now (two Warriors, one Archer), one cannot afford it."""
    warriors = [u for u in s["units"] if u["type"] == "Warrior"]
    for u in warriors[:2]:
        u["upgrade"] = {"to": "Swordsman", "gold": 60, "ok": True}
        u["orders"] = [*u["orders"], "upgrade"]
    warriors[2]["upgrade"] = {"to": "Swordsman", "gold": 60, "ok": False,
                              "reason": "the upgrade to Swordsman costs 60 gold and you have 34"}
    s["units"].append({**warriors[0], "id": "u99", "type": "Archer", "upgrade": {"to": "Longbowman", "gold": 60,
                                                                                   "ok": True}})
    return s


def test_a_unit_line_shows_its_upgrade_and_why_not():
    s = with_upgrades(state(3, idle=2, standing=4, n_events=0))
    ok = next(u for u in s["units"] if (u.get("upgrade") or {}).get("ok"))
    no = next(u for u in s["units"] if u.get("upgrade") and not u["upgrade"]["ok"])
    assert render.unit_line(ok).endswith(" · upgrade → Swordsman 60g")
    assert render.unit_line(no).endswith(" · upgrade → Swordsman 60g (not now)")
    assert render.unit_line(no, detail=True).endswith(
        " · upgrade → Swordsman 60g (not now: the upgrade to Swordsman costs 60 gold and you have 34)")
    assert "upgrade" not in render.unit_line(next(u for u in s["units"] if "upgrade" not in u))


def test_the_brief_counts_the_units_that_can_upgrade_with_the_call():
    s = with_upgrades(state(3, idle=2, standing=4, n_events=4))
    s["gold"] = 1000
    text = brief_of(s, plan_chars=300)
    assert len(text) / 4 < 600, len(text) / 4
    # the third Warrior cannot upgrade, so "all:Warrior" would fail for it: the call names the two that can
    ids = [u["id"] for u in s["units"] if u["type"] == "Warrior"][:2]
    assert ('3 units can upgrade for 180 gold in all (2 Warrior→Swordsman, 1 Archer→Longbowman) → '
            f'unit_orders(orders=[{{"unit": "{ids[0]}", "order": "upgrade"}}, {{"unit": "{ids[1]}", "order": '
            '"upgrade"}])') in text
    assert "can upgrade" not in brief_of(state(3, idle=2, standing=4, n_events=4), plan_chars=300)
    # every Warrior can: one group order
    next(u for u in s["units"] if u["type"] == "Warrior" and not u["upgrade"]["ok"])["upgrade"]["ok"] = True
    assert ('4 units can upgrade for 240 gold in all (3 Warrior→Swordsman, 1 Archer→Longbowman) → '
            'unit_orders(orders=[{"unit": "all:Warrior", "order": "upgrade"}])') in brief_of(s, plan_chars=300)


def test_the_brief_says_how_many_upgrades_the_treasury_pays_for():
    """Each upgrade is affordable alone, but not all together: the line says how many the gold pays for, in unit
    order as unit_orders runs them, and its call names only those."""
    s = with_upgrades(state(3, idle=2, standing=4, n_events=4))
    s["gold"] = 130
    first = next(u["id"] for u in s["units"] if u["type"] == "Warrior")
    s["units"][-1]["upgrade"]["gold"] = 90  # the Archer, last: 60 + 60 + 90 > 130
    line = render.upgrades_line(s)
    ids = [u["id"] for u in s["units"] if (u.get("upgrade") or {}).get("ok")][:2]
    assert ids[0] == first
    assert line == ('3 units can upgrade for 210 gold in all (2 Warrior→Swordsman, 1 Archer→Longbowman); your 130 '
                    f'gold pays for 2 now → unit_orders(orders=[{{"unit": "{ids[0]}", "order": "upgrade"}}, '
                    f'{{"unit": "{ids[1]}", "order": "upgrade"}}])')
    s["gold"] = 60
    assert render.upgrades_line(s).endswith(f'your 60 gold pays for 1 now → unit_orders(orders=[{{"unit": "{first}", '
                                            '"order": "upgrade"}])')


def test_the_viewer_has_art_for_every_unit_the_ruleset_keeps():
    """webart.UNIT_ART mirrors the standalone ruleset's art map as patches/0018 leaves it: 76 units, each with a folder
    of the community art (whose 29 folders the map may use)."""
    patch = (ROOT / "patches" / "0018-the-standalone-ruleset-keeps-every-unit-it-can-play.patch").read_text()
    lua = patch[patch.index("+++ b/C7/Lua/standalone/ruleset.lua"):patch.index("--- a/C7Engine")]
    kept = dict(re.findall(r'^[ +]\s*\["([^"]+)"\] = "([^"]+)",$', lua, re.M))
    assert len(kept) == 76 and webart.UNIT_ART == kept
    assert not {"Fighter", "Bomber", "Cruise Missile", "ICBM", "Carrier", "Leader", "Army"} & set(kept)
    assert len(set(kept.values())) <= 29


def test_unit_order_documents_the_upgrade():
    doc = str(server.UnitOrderSpec.model_fields["order"].description)
    assert "upgrade" in doc
    docs = (ROOT / "docs" / "tools.md").read_text()
    assert "upgrade" in docs and "unit_upgraded" in (ROOT / "docs" / "protocol.md").read_text()
