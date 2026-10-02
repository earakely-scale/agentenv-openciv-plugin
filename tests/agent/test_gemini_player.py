"""The Gemini player: its settings, endpoint and command, and whole games against a fake `gemini`."""

import json

from conftest import load_agent, run_task

gemini = load_agent("gemini")
SERVERS = {"openciv3": {"url": "http://env/mcp", "headers": {"Authorization": "Bearer t"}}}


def test_the_settings_keep_only_the_game_tools_and_wait_long_for_them():
    settings = gemini.gemini_settings(SERVERS, "gemini")
    assert settings["mcpServers"] == {"openciv3": {
        "httpUrl": "http://env/mcp", "timeout": 1800000, "trust": True,
        "headers": {"Authorization": "Bearer t", "X-OpenCiv3-Seat": "gemini"}}}
    assert settings["tools"]["core"] == [] and "generalist" in settings["tools"]["exclude"]
    assert settings["model"] == {"disableLoopDetection": True}
    bare = gemini.gemini_settings({"openciv3": {"url": "http://env/mcp"}}, None)
    assert "headers" not in bare["mcpServers"]["openciv3"]


def test_models_go_through_the_endpoints_gemini_route():
    proxy = {"LITELLM_BASE_URL": "https://llm.example.com/v1", "LITELLM_API_KEY": "sk-proxy"}
    assert gemini.gemini_env(proxy) == {"GEMINI_API_KEY": "sk-proxy",
                                        "GOOGLE_GEMINI_BASE_URL": "https://llm.example.com/gemini"}
    assert gemini.gemini_env({"LITELLM_API_KEY": "g-key"}) == {"GEMINI_API_KEY": "g-key"}
    cmd = gemini.gemini_cmd("gemini/gemini-3.1-pro-preview", "Play.", "s-1")
    assert cmd[:3] == ["gemini", "-p", "Play."] and cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-preview"
    assert cmd[-2:] == ["--resume", "s-1"] and cmd[cmd.index("--approval-mode") + 1] == "yolo"


def test_a_prompt_without_a_stop_turn_plays_the_game_in_sessions_to_game_over(fake_cli, monkeypatch):
    game_file = fake_cli("gemini")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://llm.example.com")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-proxy")
    result = run_task(gemini.GeminiPlayer, gemini.GeminiConfig(name="gemini", system_prompt="Win."), "Lead England.",
                      SERVERS)
    game = json.loads(game_file.read_text())
    assert game["settings"]["mcpServers"]["openciv3"]["headers"]["X-OpenCiv3-Seat"] == "gemini"
    assert game["gemini_md"] == "Win." and game["endpoint"] == ["https://llm.example.com/gemini", "sk-proxy"]
    assert [len(messages) for messages in game["sessions"].values()] == [3, 3, 2]
    first, second, _ = game["sessions"].values()
    assert first[0].startswith("Lead England.\n\nA game longer than 10 turns is played in sessions of 10 turns.")
    assert second[0].startswith("This game is under way: an earlier session played it up to turn 12.")
    assert game["args"][-2:] == ["--resume", "session-3"]
    assert result.parts[0].text == ("Ended turn 30.\n\nPlayed T0 to T30 (GAME OVER) in 3 sessions after 16 tool calls "
                                    "and 5 nudges.")
    usage = result.usage
    assert (usage.tool_call_count, usage.input_tokens, usage.output_tokens, usage.cost_usd) == (16, 800, 80, None)
    assert result.native_trajectory.format == "gemini-cli-stream-json/v1"


def test_an_error_result_fails_the_task_with_gemini_clis_error(fake_cli, monkeypatch):
    fake_cli("gemini")
    monkeypatch.setenv("FAKE_GEMINI_FAIL", "Requested entity was not found.")
    result = run_task(gemini.GeminiPlayer, gemini.GeminiConfig(model="gemini-9"), "Lead England until turn 8.")
    assert (result.error.code, result.error.message) == ("gemini_error", "Requested entity was not found.")
