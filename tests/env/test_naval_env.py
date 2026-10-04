"""Naval play in the env: passengers and cargo in unit lines, and the board and unload orders in the tools."""

import pytest

from agentenv_openciv3 import render


def ship(cargo, capacity=2, **fields):
    return {"id": "u3", "type": "Galley", "x": 69, "y": 37, "moves_left": 3.0, "moves_max": 3, "hp": 3, "hp_max": 3,
            "status": "idle", "target": None, "capacity": capacity, "cargo": cargo, "orders": ["goto", "hold"],
            "needs_orders": True, **fields}


def passenger(status="aboard", **fields):
    return {"id": "u5", "type": "Settler", "x": 69, "y": 37, "moves_left": 1.0, "moves_max": 1, "hp": 1, "hp_max": 1,
            "status": status, "target": None, "aboard": "u3", "orders": ["settle", "goto", "wake"],
            "needs_orders": False, **fields}


def test_a_ship_shows_its_cargo_and_a_passenger_its_ship():
    assert render.unit_line(ship(["u5", "u6"])) == "u3 Galley (69,37) 3/3mv · idle · cargo 2/2: u5 u6"
    assert render.unit_line(ship([], capacity=3)) == "u3 Galley (69,37) 3/3mv · idle · cargo 0/3"
    assert render.unit_line(passenger()) == "u5 Settler (69,37) 1/1mv · aboard u3"
    # A passenger with a standing order shows it.
    target = {"x": 69, "y": 39, "dist": 1, "dir": "S"}
    line = render.unit_line(passenger("settle", target=target))
    assert line == "u5 Settler (69,37) 1/1mv · aboard u3, settle→(69,39) 1 S"
    # Units that are not ships or passengers are unchanged.
    plain = {k: v for k, v in passenger("fortified").items() if k != "aboard"}
    assert render.unit_line(plain) == "u5 Settler (69,37) 1/1mv · fortified"


@pytest.mark.anyio
async def test_unit_order_describes_board_and_unload(env):
    listed = {t.name: t for t in await env.mcp.list_tools()}
    order = listed["unit_order"].inputSchema["properties"]["order"]["description"]
    assert "board (a land unit boards your ship on its tile, or on the adjacent water tile x,y" in order
    assert "unload (in a city, a ship's passengers go ashore)" in order
    assert "At sea a passenger lands with goto or settle to a land tile next to its ship." in order
    spec = listed["unit_orders"].inputSchema["$defs"]["UnitOrderSpec"]["properties"]
    assert "board" in spec["order"]["description"] and "board" in spec["x"]["description"]


@pytest.mark.anyio
async def test_list_units_names_passengers_and_cargo(env):
    listed = {t.name: t for t in await env.mcp.list_tools()}
    description = " ".join(listed["list_units"].description.split())
    assert '("aboard u3" on a ship), a ship\'s cargo ("cargo 1/2: u5")' in description


def test_a_laden_ship_is_not_told_to_fortify_and_standing_shows_its_cargo():
    """Fortified at sea, a ship parks its passengers out of sight: the batch hint leaves laden ships out, and the
    STANDING line names who is aboard."""
    from test_render import state, unit

    s = state(2, idle=0, standing=0, n_events=0)
    galley = {**unit(3, "Galley", 30, 10, needs=True), "capacity": 2, "cargo": ["u4", "u5"]}
    warriors = [unit(n, "Warrior", 20, 10, needs=True) for n in (6, 7)]
    hint = render.batch_hint([galley, *warriors])
    assert hint == 'unit_orders(orders=[{"unit": "idle:Warrior", "order": "fortify"}])'
    s["units"] = [{**galley, "needs_orders": False, "status": "fortified"},
                  {**unit(4, "Warrior", 30, 10, status="aboard"), "aboard": "u3"}]
    assert "STANDING u3 Galley fortified, cargo u4 u5" in render.brief(s, start_techs=2)
