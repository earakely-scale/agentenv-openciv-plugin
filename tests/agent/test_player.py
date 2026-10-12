"""The game loop the player agents share (the game's turn, nudges, sessions, the task's result) and the Claude player:
its pure parts (credentials, MCP config, the claude command) and whole games against a fake `claude`."""

import json
from pathlib import Path

import openciv3_player as common
from conftest import load_agent, run_task

claude = load_agent("claude")


def test_each_kind_of_key_becomes_the_claude_code_setting_for_it():
    oauth = {"LITELLM_API_KEY": "sk-ant-oat01-x"}
    assert claude.model_env(oauth, "sonnet") == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    direct = {"LITELLM_API_KEY": "sk-ant-api03-x", "LITELLM_BASE_URL": "https://api.anthropic.com"}
    assert claude.model_env(direct, "sonnet") == {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}
    proxy = {"LITELLM_API_KEY": "sk-proxy", "LITELLM_BASE_URL": "https://llm.example.com/v1/"}
    assert claude.model_env(proxy, "anthropic/claude-sonnet-5-5") == {
        "ANTHROPIC_AUTH_TOKEN": "sk-proxy", "ANTHROPIC_BASE_URL": "https://llm.example.com",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-sonnet-5-5",
        "ANTHROPIC_SMALL_FAST_MODEL": "anthropic/claude-sonnet-5-5"}
    assert claude.model_env({}, "sonnet") == {}


def test_another_providers_model_gets_no_anthropic_betas():
    proxy = {"LITELLM_API_KEY": "sk-proxy", "LITELLM_BASE_URL": "https://llm.example.com"}
    assert claude.model_env(proxy, "xai/grok-4.7")["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in claude.model_env(proxy, "anthropic/claude-opus-5-5")


def test_a_proxy_model_name_works_on_anthropics_own_api():
    proxy = {"LITELLM_BASE_URL": "https://llm.example.com"}
    assert claude.model_id(proxy, "anthropic/claude-opus-5-5") == "anthropic/claude-opus-5-5"
    direct = {"LITELLM_BASE_URL": "https://api.anthropic.com"}
    assert claude.model_id(direct, "anthropic/claude-opus-5-5") == "claude-opus-5-5"
    assert claude.model_id({}, "anthropic/claude-haiku-4-5") == "claude-haiku-4-5"
    assert claude.model_id({}, "sonnet") == "sonnet"


def test_mcp_servers_become_a_claude_code_mcp_config():
    servers = {"openciv3": {"url": "http://env:18765/mcp", "headers": None},
               "other": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}
    assert claude.mcp_config(servers) == {"mcpServers": {
        "openciv3": {"type": "http", "url": "http://env:18765/mcp"},
        "other": {"type": "http", "url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}}
    seated = claude.mcp_config(servers, "Greece")["mcpServers"]
    assert seated["openciv3"]["headers"] == {"X-OpenCiv3-Seat": "Greece"}
    assert seated["other"]["headers"] == {"Authorization": "Bearer t", "X-OpenCiv3-Seat": "Greece"}


def test_the_seat_is_openciv3_seat_or_else_the_agents_name():
    named = claude.ClaudeConfig(name="opus")
    assert common.seat_name({"OPENCIV3_SEAT": "Rome"}, named) == "Rome"
    assert common.seat_name({}, named) == "opus"
    assert common.seat_name({}, claude.ClaudeConfig()) is None


def test_the_claude_command_allows_only_the_env_tools():
    cmd = claude.claude_cmd(claude.ClaudeConfig(model="opus", effort="high"), Path("/t/mcp.json"), ["openciv3"])
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__openciv3__*" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus" and cmd[-2:] == ["--effort", "high"]


def test_the_game_turn_comes_from_the_last_footer():
    assert common.game_turn("Ended.\n[T23/60 · needs orders: u7]") == (23, 60, False)
    assert common.game_turn("GAME OVER at T60. Final score 412.\n[T60/60 · needs orders: none]") == (60, 60, True)
    assert common.game_turn("GAME OVER — turn 60/60 reached.\n[GAME OVER T60/60]") == (60, 60, True)
    assert common.game_turn("[T3/60 · needs orders: u1] then [T4/60 · needs orders: u1]") == (4, 60, False)
    assert common.game_turn("GAME OVER") == (None, None, True)
    plan = "plan (T12): expand, then attack Rome. This session ends after 40 turns or GAME OVER.\n"
    assert common.game_turn(plan + "[T12/270 · nothing needs orders]") == (12, 270, False)
    assert common.game_turn("From China: GAME OVER for Rome soon.\n[T40/270 · needs orders: u7]") == (
        40, 270, False)
    assert common.game_turn("plan (T5):\nGAME OVER is far off.\n[T5/270 · nothing needs orders]") == (5, 270, False)
    assert common.game_turn("hi") is None


def test_nudges_until_the_session_turn_or_game_over():
    nudge = "The game is at turn 23; this session ends at turn {}. Continue playing."
    assert common.next_message(23, 540, False, 90, 0, 30) == nudge.format(90)
    assert common.next_message(90, 540, False, 90, 0, 30) is None
    assert common.next_message(23, 60, False, None, 0, 30) == nudge.format(60)
    assert common.next_message(23, 540, True, 90, 0, 30) is None
    assert common.next_message(23, 540, False, 90, 30, 30) is None
    assert common.next_message(None, None, False, 90, 0, 30) is None
    assert common.STOP_AT.search("Play on until turn 180, then reply.").group(1) == "180"


def test_a_prompt_names_its_stop_turn_or_the_game_is_played_in_sessions():
    assert common.session_plan("Play on until turn 180, then reply.", {}) == common.Session(stop=180)
    assert common.session_plan("Play until GAME OVER.", {}) == common.Session(turns=75)
    assert common.session_plan("Play until GAME OVER.", {"OPENCIV3_SESSION_TURNS": "40"}) == common.Session(turns=40)
    assert common.session_plan("Play until GAME OVER.", {"OPENCIV3_SESSION_TURNS": "0"}) == common.Session()


def test_a_session_ends_its_length_after_the_first_turn_it_sees():
    session = common.Session(turns=75)
    assert session.end is None
    session.saw("[T3/300 · needs orders: u1]")
    session.saw("Error executing tool end_turn: [T4/300 · needs orders: u1]")
    assert (session.start, session.turn, session.end) == (3, 4, 78)
    assert session.following() == common.Session(turns=75, start=4, turn=4, limit=300)
    assert common.Session(stop=90).end == 90 and common.Session().end is None


def test_a_later_session_is_told_the_game_is_under_way():
    first = common.session_message("Lead Rome.", common.Session(turns=75))
    assert first.startswith("Lead Rome.\n\nA game longer than 75 turns is played in sessions of 75 turns.")
    later = common.session_message("Lead Rome.", common.Session(turns=75, start=76))
    assert later.startswith("This game is under way: an earlier session played it up to turn 76. Start with the "
                            "get_turn_brief tool") and later.endswith(first)
    assert common.session_message("Lead Rome until turn 90.", common.Session(stop=90)) == "Lead Rome until turn 90."


def test_sessions_follow_one_another_while_the_game_goes_on_and_moves():
    def session(start, turn, calls=5, **kw):
        return common.Session(turns=10, start=start, turn=turn, calls=calls, **kw)

    assert common.rotate([session(0, 12)])
    assert not common.rotate([session(0, 30, over=True)])
    assert not common.rotate([session(0, 12, calls=0)])
    assert not common.rotate([common.Session(stop=90, start=0, turn=90, calls=5)])
    assert common.rotate([session(0, 12), session(12, 12)])
    assert not common.rotate([session(0, 12), session(12, 12), session(12, 12)])


def test_a_session_that_exited_without_a_result_fails_the_task_only_when_none_played_on():
    crashed = common.Session(turns=10, start=0, turn=12, limit=30, calls=6, returncode=-9)
    failed = common.played([crashed], claude.ClaudeCode).error
    assert (failed.code, failed.message) == ("claude_failed", "Claude Code exited (-9) without a result.")
    over = common.Session(turns=10, start=12, turn=30, limit=30, over=True, calls=10, reply="Won.", finished=True,
                          input_tokens=100, output_tokens=10, cost_usd=1.5)
    result = common.played([crashed, over], claude.ClaudeCode)
    assert result.parts[0].text == ("Won.\n\nPlayed T0 to T30 (GAME OVER) in 2 sessions after 16 tool calls and 0 "
                                    "nudges ($1.50).")
    assert (result.usage.tool_call_count, result.usage.input_tokens, result.usage.cost_usd) == (16, 100, 1.5)


def test_a_session_that_fails_before_playing_fails_the_task_and_keeps_what_was_played():
    first = common.Session(turns=10, start=0, turn=12, limit=30, calls=6, reply="Expanding.", finished=True,
                           input_tokens=1000, output_tokens=100, cost_usd=1.0)
    broke = common.Session(turns=10, start=12, turn=12, limit=30, error="API Error: 400 budget exceeded")
    result = common.played([first, broke], claude.ClaudeCode)
    assert (result.error.code, result.error.message) == (
        "claude_error", "Session 2 failed at turn 12 before playing: API Error: 400 budget exceeded")
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cost_usd) == (1000, 100, 1.0)
    timed_out = common.played([first], claude.ClaudeCode, ("timeout", "The game ran past its 60 s limit."))
    assert timed_out.error.code == "timeout" and timed_out.usage.tool_call_count == 6
    assert timed_out.native_trajectory.payload[0] == {"type": "session", "index": 1, "start_turn": 0}
    assert common.played([], claude.ClaudeCode, ("timeout", "The game ran past its 60 s limit")).error.code == "timeout"
    refused = common.played([common.Session(error="Model not found.")], claude.ClaudeCode).error
    assert (refused.code, refused.message) == ("claude_error", "Model not found.")


def test_a_cli_that_reports_no_cost_reports_tokens_only():
    session = common.Session(stop=8, start=0, turn=8, limit=30, calls=4, finished=True, input_tokens=5)
    result = common.played([session], claude.ClaudeCode)
    assert result.parts[0].text == "Session ended at turn 8/30 after 4 tool calls and 0 nudges."
    assert (result.usage.input_tokens, result.usage.cost_usd) == (5, None)


def test_a_prompt_without_a_stop_turn_plays_the_game_in_sessions_to_game_over(fake_cli):
    game_file = fake_cli("claude")
    result = run_task(claude.ClaudePlayer, claude.ClaudeConfig(name="opus"), "Lead Rome.")
    game = json.loads(game_file.read_text())
    assert game["mcp"]["mcpServers"]["openciv3"]["headers"] == {"X-OpenCiv3-Seat": "opus"}
    assert [len(messages) for messages in game["sessions"].values()] == [3, 3, 2]
    first, second, third = game["sessions"].values()
    assert first[0].startswith("Lead Rome.\n\nA game longer than 10 turns is played in sessions of 10 turns.")
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
    assert result.native_trajectory.format == "claude-code-stream-json/v1"


def test_a_prompt_with_a_stop_turn_is_one_session(fake_cli):
    game_file = fake_cli("claude")
    result = run_task(claude.ClaudePlayer, claude.ClaudeConfig(), "Lead Rome until turn 8.")
    game = json.loads(game_file.read_text())
    assert game["mcp"]["mcpServers"]["openciv3"] == {"type": "http", "url": "http://env/mcp"}
    nudge = "The game is at turn 4; this session ends at turn 8. Continue playing."
    assert list(game["sessions"].values()) == [["Lead Rome until turn 8.", nudge]]
    assert result.parts[0].text == ("Ended turn 8.\n\nSession ended at turn 8/30 after 4 tool calls and 1 nudges "
                                    "($0.50).")
    assert result.usage.cost_usd == 0.5 and len(result.native_trajectory.payload) == 2 * 6


def test_a_model_that_goes_silent_ends_its_session_and_a_fresh_one_plays_on(fake_cli, monkeypatch):
    game_file = fake_cli("claude")
    monkeypatch.setenv("FAKE_HANG", "model")
    monkeypatch.setattr(claude, "MODEL_SILENCE_SECONDS", 1)
    result = run_task(claude.ClaudePlayer, claude.ClaudeConfig(name="opus"), "Lead Rome.")
    sessions = list(json.loads(game_file.read_text())["sessions"].values())
    assert [len(messages) for messages in sessions] == [1, 2, 3, 2]
    assert sessions[1][0].startswith("This game is under way: an earlier session played it up to turn 0.")
    assert result.parts[0].text.endswith("Played T0 to T30 (GAME OVER) in 4 sessions after 15 tool calls and 4 nudges "
                                         "($1.75).")


def test_a_long_tool_call_is_not_a_silent_model(fake_cli, monkeypatch):
    game_file = fake_cli("claude")
    monkeypatch.setenv("FAKE_HANG", "tool")
    monkeypatch.setenv("FAKE_HANG_SECONDS", "2")
    monkeypatch.setattr(claude, "MODEL_SILENCE_SECONDS", 1)
    result = run_task(claude.ClaudePlayer, claude.ClaudeConfig(name="opus"), "Lead Rome.")
    sessions = json.loads(game_file.read_text())["sessions"].values()
    assert [len(messages) for messages in sessions] == [3, 3, 2]
    assert result.parts[0].text.endswith("in 3 sessions after 16 tool calls and 5 nudges ($2.00).")
