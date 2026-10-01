import asyncio

import pytest

from agentenv_openciv3.bridge import Bridge, BridgeError

pytestmark = pytest.mark.anyio


@pytest.fixture
async def bridge(fake_cmd):
    b = Bridge(fake_cmd)
    yield b
    await b.close()


async def test_ready_line_and_new_game(bridge):
    game = await bridge.new_game(seed=5, turn_limit=30)
    assert bridge.version == "fake-1"
    assert game == {"turn": 1, "turn_limit": 30, "seed": 5, "civ": "Rome", "opponents": ["Greece", "Egypt", "Babylon"],
                    "map": {"width": 60, "height": 60, "wrap_x": True}}
    state = await bridge.call("state")
    assert state["units"][0]["id"] == "u1"


async def test_error_reply_is_typed(bridge):
    await bridge.new_game(seed=1)
    await bridge.call("unit_order", unit="u1", order="found_city")
    with pytest.raises(BridgeError) as info:
        await bridge.call("unit_order", unit="u3", order="found_city")
    e = info.value
    assert e.code == "cannot_found"
    assert "adjacent to Rome" in e.message
    assert e.alternatives and {"x", "y", "score", "dist", "dir"} <= set(e.alternatives[0])
    assert e.suggest.startswith('unit_order(unit="u3", order="settle"')


async def test_second_new_game_needs_a_new_process(bridge):
    await bridge.new_game(seed=1)
    with pytest.raises(BridgeError) as info:
        await bridge.call("new_game", seed=2)
    assert info.value.code == "already_started"
    pid = bridge._proc.pid
    game = await bridge.new_game(seed=2)
    assert game["seed"] == 2 and bridge._proc.pid != pid


async def test_timeout_stops_the_process(bridge):
    await bridge.start()
    with pytest.raises(BridgeError) as info:
        await bridge.call("_sleep", timeout=0.3, seconds=5)
    assert info.value.code == "timeout"
    assert not bridge.running
    with pytest.raises(BridgeError) as info:
        await bridge.call("state")
    assert info.value.code == "bridge_down"


async def test_crash_reports_stderr_tail(bridge):
    await bridge.start()
    with pytest.raises(BridgeError) as info:
        await bridge.call("_crash")
    assert info.value.code == "bridge_failed"
    assert "NullReferenceException" in info.value.message
    assert not bridge.running


async def test_lines_far_beyond_the_asyncio_default_limit(bridge):
    await bridge.start()
    assert len((await bridge.call("_big", size=5_000_000))["blob"]) == 5_000_000


async def test_heavy_stderr_logging_does_not_block(bridge, monkeypatch):
    monkeypatch.setenv("FAKE_BRIDGE_STDERR_KB", "1024")
    await bridge.start()
    await bridge.call("new_game", seed=1)


async def test_stray_stdout_lines_are_skipped(bridge, monkeypatch):
    monkeypatch.setenv("FAKE_BRIDGE_STDOUT_NOISE", "1")
    await bridge.start()
    assert bridge.version == "fake-1"


async def test_cancelled_call_does_not_shift_replies(bridge):
    await bridge.new_game(seed=1)
    slow = asyncio.create_task(bridge.call("_sleep", seconds=0.3))
    await asyncio.sleep(0.05)
    slow.cancel()
    state = await bridge.call("state")
    assert state["turn"] == 1 and "units" in state


async def test_missing_binary():
    with pytest.raises(BridgeError) as info:
        await Bridge(["/nonexistent/CivBridge"]).start()
    assert info.value.code == "bridge_failed"
