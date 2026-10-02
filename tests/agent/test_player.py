"""The Claude player agent: its pure parts (credentials, MCP config, the claude command, the game's turn, nudges,
sessions) and whole games against a fake `claude`."""

import asyncio
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path

import pytest
from agentenv_protocol.a2a_agent import TaskRequest, TextPart

AGENT = Path(__file__).resolve().parents[2] / "agents" / "claude-player" / "agent.py"
FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
spec = importlib.util.spec_from_file_location("claude_player", AGENT)
player = importlib.util.module_from_spec(spec)
sys.modules["claude_player"] = player
spec.loader.exec_module(player)


def test_each_kind_of_key_becomes_the_claude_code_setting_for_it():
    oauth = {"LITELLM_API_KEY": "sk-ant-oat01-x"}
    assert player.model_env(oauth, "sonnet") == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    direct = {"LITELLM_API_KEY": "sk-ant-api03-x", "LITELLM_BASE_URL": "https://api.anthropic.com"}
    assert player.model_env(direct, "sonnet") == {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}
    proxy = {"LITELLM_API_KEY": "sk-proxy", "LITELLM_BASE_URL": "https://llm.example.com/v1/"}
    assert player.model_env(proxy, "anthropic/claude-sonnet-5-5") == {
        "ANTHROPIC_AUTH_TOKEN": "sk-proxy", "ANTHROPIC_BASE_URL": "https://llm.example.com",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-sonnet-5-5",
        "ANTHROPIC_SMALL_FAST_MODEL": "anthropic/claude-sonnet-5-5"}
    assert player.model_env({}, "sonnet") == {}


def test_a_proxy_model_name_works_on_anthropics_own_api():
    proxy = {"LITELLM_BASE_URL": "https://llm.example.com"}
    assert player.model_id(proxy, "anthropic/claude-opus-5-5") == "anthropic/claude-opus-5-5"
    direct = {"LITELLM_BASE_URL": "https://api.anthropic.com"}
    assert player.model_id(direct, "anthropic/claude-opus-5-5") == "claude-opus-5-5"
    assert player.model_id({}, "anthropic/claude-haiku-4-5") == "claude-haiku-4-5"
    assert player.model_id({}, "sonnet") == "sonnet"


def test_mcp_servers_become_a_claude_code_mcp_config():
    servers = {"openciv3": {"url": "http://env:18765/mcp", "headers": None},
               "other": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}
    assert player.mcp_config(servers) == {"mcpServers": {
        "openciv3": {"type": "http", "url": "http://env:18765/mcp"},
        "other": {"type": "http", "url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}}
    seated = player.mcp_config(servers, "Greece")["mcpServers"]
    assert seated["openciv3"]["headers"] == {"X-OpenCiv3-Seat": "Greece"}
    assert seated["other"]["headers"] == {"Authorization": "Bearer t", "X-OpenCiv3-Seat": "Greece"}


def test_the_seat_is_openciv3_seat_or_else_the_agents_name():
    named = player.PlayerConfig(name="opus")
    assert player.seat_name({"OPENCIV3_SEAT": "Rome"}, named) == "Rome"
    assert player.seat_name({}, named) == "opus"
    assert player.seat_name({}, player.PlayerConfig()) is None


def test_the_claude_command_allows_only_the_env_tools():
    cmd = player.claude_cmd(player.PlayerConfig(model="opus", effort="high"), Path("/t/mcp.json"), ["openciv3"])
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__openciv3__*" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus" and cmd[-2:] == ["--effort", "high"]


def _result(text):
    result = {"type": "tool_result", "content": [{"type": "text", "text": text}]}
    return {"type": "user", "message": {"content": [result]}}


def test_the_game_turn_comes_from_the_last_footer():
    assert player.game_turn(_result("Ended.\n[T23/60 · needs orders: u7]")) == (23, 60, False)
    over = _result("GAME OVER at T60. Final score 412.\n[T60/60 · needs orders: none]")
    assert player.game_turn(over) == (60, 60, True)
    assert player.game_turn(_result("GAME OVER — turn 60/60 reached.\n[GAME OVER T60/60]")) == (60, 60, True)
    assert player.game_turn(_result("Rome · Despotism\n[T0/60 · needs orders: research]")) == (0, 60, False)
    assert player.game_turn({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}) is None


def test_nudges_until_the_session_turn_or_game_over():
    nudge = "The game is at turn 23; this session ends at turn {}. Continue playing."
    assert player.next_message(23, 540, False, 90, 0, 30) == nudge.format(90)
    assert player.next_message(90, 540, False, 90, 0, 30) is None
    assert player.next_message(23, 60, False, None, 0, 30) == nudge.format(60)
    assert player.next_message(23, 540, True, 90, 0, 30) is None
    assert player.next_message(23, 540, False, 90, 30, 30) is None
    assert player.next_message(None, None, False, 90, 0, 30) is None
    assert player.STOP_AT.search("Play on until turn 180, then reply.").group(1) == "180"


def test_a_prompt_names_its_stop_turn_or_the_game_is_played_in_sessions():
    assert player.session_plan("Play on until turn 180, then reply.", {}) == player.Session(stop=180)
    assert player.session_plan("Play until GAME OVER.", {}) == player.Session(turns=75)
    assert player.session_plan("Play until GAME OVER.", {"OPENCIV3_SESSION_TURNS": "40"}) == player.Session(turns=40)
    assert player.session_plan("Play until GAME OVER.", {"OPENCIV3_SESSION_TURNS": "0"}) == player.Session()


def test_a_session_ends_its_length_after_the_first_turn_it_sees():
    session = player.Session(turns=75)
    assert session.end is None
    session.see(_result("[T3/300 · needs orders: u1]"))
    session.see(_result("[T4/300 · needs orders: u1]"))
    assert (session.start, session.turn, session.end) == (3, 4, 78)
    assert session.following() == player.Session(turns=75, start=4, turn=4, limit=300)
    assert player.Session(stop=90).end == 90 and player.Session().end is None


def test_a_later_session_is_told_the_game_is_under_way():
    first = player.session_message("Lead Rome.", player.Session(turns=75))
    assert first.startswith("Lead Rome.\n\nYou play this game in sessions of 75 turns.")
    later = player.session_message("Lead Rome.", player.Session(turns=75, start=76))
    assert later.startswith("This game is under way: an earlier session played it up to turn 76. Start with the "
                            "get_turn_brief tool") and later.endswith(first)
    assert player.session_message("Lead Rome until turn 90.", player.Session(stop=90)) == "Lead Rome until turn 90."


def test_sessions_follow_one_another_while_the_game_goes_on_and_moves():
    def session(start, turn, calls=5, **kw):
        return player.Session(turns=10, start=start, turn=turn, calls=calls, **kw)

    assert player.rotate([session(0, 12)])
    assert not player.rotate([session(0, 30, over=True)])
    assert not player.rotate([session(0, 12, calls=0)])
    assert not player.rotate([player.Session(stop=90, start=0, turn=90, calls=5)])
    assert player.rotate([session(0, 12), session(12, 12)])
    assert not player.rotate([session(0, 12), session(12, 12), session(12, 12)])


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    """fake_claude.py as `claude` on PATH, sessions of 10 turns; returns the file it keeps the game in."""
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} "$@"\n')
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{claude.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_GAME", str(tmp_path / "game.json"))
    monkeypatch.setenv("OPENCIV3_SESSION_TURNS", "10")
    monkeypatch.delenv("OPENCIV3_SEAT", raising=False)
    return tmp_path / "game.json"


def _play(prompt, **config):
    request = TaskRequest(task_id="t", context_id="c", parts=(TextPart(text=prompt),),
                          config=player.PlayerConfig(**config), mcp_servers={"openciv3": {"url": "http://env/mcp"}})
    return asyncio.run(player.ClaudePlayer().run(request))


def test_a_prompt_without_a_stop_turn_plays_the_game_in_sessions_to_game_over(fake_claude):
    result = _play("Lead Rome.", name="opus")
    game = json.loads(fake_claude.read_text())
    assert game["mcp"]["mcpServers"]["openciv3"]["headers"] == {"X-OpenCiv3-Seat": "opus"}
    assert [len(messages) for messages in game["sessions"]] == [3, 3, 2]
    first, second, third = game["sessions"]
    assert first[0].startswith("Lead Rome.\n\nYou play this game in sessions of 10 turns.")
    assert first[1:] == [f"The game is at turn {t}; this session ends at turn 10. Continue playing." for t in (4, 8)]
    assert second[0].startswith("This game is under way: an earlier session played it up to turn 12.")
    assert third[1] == "The game is at turn 28; this session ends at turn 30. Continue playing."
    assert result.parts[0].text == ("Ended turn 30.\n\nPlayed T0 to T30 (GAME OVER) in 3 sessions after 16 tool calls "
                                    "and 5 nudges ($2.00).")
    usage = result.usage
    assert (usage.tool_call_count, usage.input_tokens, usage.output_tokens, usage.cost_usd) == (16, 800, 80, 2.0)
    payload = result.native_trajectory.payload
    marks = [e for e in payload if e["type"] == "session"]
    assert marks == [{"type": "session", "index": k, "start_turn": t} for k, t in ((1, 0), (2, 12), (3, 24))]
    assert payload[0] == marks[0] and len(payload) == 3 + 8 * 6


def test_a_prompt_with_a_stop_turn_is_one_session(fake_claude):
    result = _play("Lead Rome until turn 8.")
    game = json.loads(fake_claude.read_text())
    assert game["mcp"]["mcpServers"]["openciv3"] == {"type": "http", "url": "http://env/mcp"}
    assert game["sessions"] == [["Lead Rome until turn 8.",
                                 "The game is at turn 4; this session ends at turn 8. Continue playing."]]
    assert result.parts[0].text == ("Ended turn 8.\n\nSession ended at turn 8/30 after 4 tool calls and 1 nudges "
                                    "($0.50).")
    assert result.usage.cost_usd == 0.5 and len(result.native_trajectory.payload) == 2 * 6
