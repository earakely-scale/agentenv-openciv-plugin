"""The Claude player agent's pure parts: credentials, MCP config, the claude command, the game's turn, nudges."""

import importlib.util
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agents" / "claude-player" / "agent.py"
spec = importlib.util.spec_from_file_location("claude_player", AGENT)
player = importlib.util.module_from_spec(spec)
sys.modules["claude_player"] = player
spec.loader.exec_module(player)


def test_each_kind_of_key_becomes_the_claude_code_setting_for_it():
    assert player.model_env({"LITELLM_API_KEY": "sk-ant-oat01-x"}) == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    assert player.model_env({"LITELLM_API_KEY": "sk-ant-api03-x", "LITELLM_BASE_URL": "https://api.anthropic.com"}) == {
        "ANTHROPIC_API_KEY": "sk-ant-api03-x"}
    assert player.model_env({"LITELLM_API_KEY": "sk-proxy", "LITELLM_BASE_URL": "https://llm.example.com/v1/"}) == {
        "ANTHROPIC_AUTH_TOKEN": "sk-proxy", "ANTHROPIC_BASE_URL": "https://llm.example.com"}
    assert player.model_env({}) == {}


def test_mcp_servers_become_a_claude_code_mcp_config():
    servers = {"openciv3": {"url": "http://env:18765/mcp", "headers": None},
               "other": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}
    assert player.mcp_config(servers) == {"mcpServers": {
        "openciv3": {"type": "http", "url": "http://env:18765/mcp"},
        "other": {"type": "http", "url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}}


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
