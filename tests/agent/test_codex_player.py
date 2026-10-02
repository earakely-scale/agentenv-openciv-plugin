"""The Codex player: its config.toml and command, and whole games against a fake `codex`."""

import json
import tomllib

from conftest import load_agent, run_task

codex = load_agent("codex")
SERVERS = {"openciv3": {"url": "http://env/mcp", "headers": {"Authorization": "Bearer t"}}}


def test_the_config_routes_models_through_the_endpoint_and_waits_long_for_tools():
    config = tomllib.loads(codex.codex_config({"LITELLM_BASE_URL": "https://llm.example.com/v1/"}, SERVERS, "sol"))
    assert config["model_providers"]["agentenv"] == {
        "name": "agent-env", "base_url": "https://llm.example.com/openai/v1", "env_key": "LITELLM_API_KEY",
        "wire_api": "responses"}
    assert config["model_provider"] == "agentenv" and config["web_search"] == "disabled"
    assert config["mcp_servers"]["openciv3"] == {
        "url": "http://env/mcp", "required": True, "startup_timeout_sec": 60, "tool_timeout_sec": 1800,
        "http_headers": {"Authorization": "Bearer t", "X-OpenCiv3-Seat": "sol"}}
    direct = tomllib.loads(codex.codex_config({}, {"openciv3": {"url": "http://env/mcp"}}, None))
    assert direct["model_providers"]["agentenv"]["base_url"] == "https://api.openai.com/v1"
    assert "http_headers" not in direct["mcp_servers"]["openciv3"]


def test_the_command_resumes_the_thread_with_only_the_game_tools():
    config = codex.CodexConfig(model="openai/gpt-5.6-sol", effort="high", system_prompt='Play "well".')
    cmd = codex.codex_cmd(config, None)
    assert cmd[:2] == ["codex", "exec"] and cmd[-1] == "-" and "--json" in cmd
    assert 'model="gpt-5.6-sol"' in cmd and 'model_reasoning_effort="high"' in cmd
    assert 'developer_instructions="Play \\"well\\"."' in cmd
    assert {"shell_tool", "unified_exec", "view_image"} <= {cmd[k + 1] for k, x in enumerate(cmd) if x == "--disable"}
    assert codex.codex_cmd(config, "t-1")[-3:] == ["resume", "t-1", "-"]


def test_a_prompt_without_a_stop_turn_plays_the_game_in_threads_to_game_over(fake_cli, monkeypatch):
    game_file = fake_cli("codex")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://llm.example.com")
    result = run_task(codex.CodexPlayer, codex.CodexConfig(name="sol", model="openai/gpt-5.6-sol"), "Lead America.",
                      SERVERS)
    game = json.loads(game_file.read_text())
    assert 'http_headers = { "Authorization" = "Bearer t", "X-OpenCiv3-Seat" = "sol" }' in game["config"]
    assert [len(messages) for messages in game["sessions"].values()] == [3, 3, 2]
    first, second, _ = game["sessions"].values()
    assert first[0].startswith("Lead America.\n\nA game longer than 10 turns is played in sessions of 10 turns.")
    assert first[1] == "The game is at turn 4; this session ends at turn 10. Continue playing."
    assert second[0].startswith("This game is under way: an earlier session played it up to turn 12.")
    assert game["args"][-3:] == ["resume", "thread-3", "-"]
    assert result.parts[0].text == ("Ended turn 30.\n\nPlayed T0 to T30 (GAME OVER) in 3 sessions after 16 tool calls "
                                    "and 5 nudges.")
    usage = result.usage
    assert (usage.tool_call_count, usage.input_tokens, usage.output_tokens, usage.cost_usd) == (16, 800, 80, None)
    assert result.native_trajectory.format == "codex-exec-json/v1"


def test_a_failed_turn_fails_the_task_with_codexs_error(fake_cli, monkeypatch):
    fake_cli("codex")
    monkeypatch.setenv("FAKE_CODEX_FAIL", "model gpt-9 not found")
    result = run_task(codex.CodexPlayer, codex.CodexConfig(model="gpt-9"), "Lead America until turn 8.")
    assert (result.error.code, result.error.message) == ("codex_error", "model gpt-9 not found")
