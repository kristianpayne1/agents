"""The `claude` adapter: the command it builds and how it reads the CLI's JSON result."""

import json
from pathlib import Path

from agents.harness import get_harness
from agents.harness.base import LEAF, SPLITTER, NodeRequest

SERVER = ["python", "-m", "agents.server"]


def request(role: str, effort: str | None = None) -> NodeRequest:
    return NodeRequest(
        role=role,
        model="some-model",
        effort=effort,
        system_prompt="role prompt",
        prompt="the task",
        worktree=Path("/tmp/worktree"),
        budget_usd=1.5,
        env={"AGENTS_RUN_ID": "run-1", "AGENTS_NODE_ID": "root.1"},
        delegate_server=SERVER if role == SPLITTER else None,
        allowed_commands=["pytest tests/test_x.py"],
    )


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_leaf_command_is_headless_and_has_no_delegate_server() -> None:
    command = get_harness("claude").build_command(request(LEAF))
    argv = command.argv

    assert argv[:2] == ["claude", "--print"]
    assert command.stdin == "the task"
    assert flag_value(argv, "--output-format") == "json"
    assert flag_value(argv, "--model") == "some-model"
    assert flag_value(argv, "--append-system-prompt") == "role prompt"
    assert flag_value(argv, "--max-budget-usd") == "1.5000"
    assert "--effort" not in argv
    # No MCP servers at all, even ones registered for the user.
    assert "--strict-mcp-config" in argv
    assert json.loads(flag_value(argv, "--mcp-config")) == {"mcpServers": {}}
    assert not any(argument.startswith("mcp__") for argument in argv)


def test_leaf_may_only_run_its_test_command_and_a_few_git_commands() -> None:
    argv = get_harness("claude").build_command(request(LEAF)).argv
    allowed = argv[argv.index("--allowedTools") + 1 :]

    assert flag_value(argv, "--permission-mode") == "acceptEdits"
    assert flag_value(argv, "--permission-prompts") == "none"
    assert "Bash(pytest tests/test_x.py)" in allowed
    assert "Bash(git commit *)" in allowed
    assert not any("push" in rule or "merge" in rule or "reset" in rule for rule in allowed)
    assert flag_value(argv, "--disallowedTools") == "Agent"


def test_splitter_command_attaches_the_delegate_server_with_the_run_context() -> None:
    argv = get_harness("claude").build_command(request(SPLITTER, effort="high")).argv

    assert flag_value(argv, "--effort") == "high"
    servers = json.loads(flag_value(argv, "--mcp-config"))["mcpServers"]
    assert servers["delegate"]["command"] == "python"
    assert servers["delegate"]["args"] == ["-m", "agents.server"]
    assert servers["delegate"]["env"]["AGENTS_NODE_ID"] == "root.1"
    assert "mcp__delegate" in argv


def test_planner_command_is_interactive() -> None:
    argv = get_harness("claude").build_planner_command(request(SPLITTER)).argv

    assert "--print" not in argv
    assert "delegate" in json.loads(flag_value(argv, "--mcp-config"))["mcpServers"]
    assert argv[-2:] == ["--", "the task"]


def test_result_and_cost_come_from_the_json_output() -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Added slugify().",
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300},
            "permission_denials": [{"tool_name": "Bash"}],
        }
    )

    result = get_harness("claude").parse_result(0, stdout, "")

    assert result.ok
    assert result.summary == "Added slugify()."
    assert result.cost_usd == 0.0123
    assert result.tokens["output_tokens"] == 20
    assert result.tokens["cache_creation_input_tokens"] == 0
    assert result.permission_denials == 1


def test_error_result_is_not_ok_but_still_has_its_cost() -> None:
    stdout = json.dumps(
        {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 2}
    )

    result = get_harness("claude").parse_result(1, stdout, "")

    assert not result.ok
    assert result.summary == "claude stopped: error_max_budget_usd"
    assert result.cost_usd == 2.0


def test_missing_cost_is_reported_as_unknown() -> None:
    result = get_harness("claude").parse_result(0, json.dumps({"result": "done"}), "")

    assert result.ok
    assert result.cost_usd is None


def test_output_that_is_not_json_is_a_failure() -> None:
    result = get_harness("claude").parse_result(127, "", "claude: command not found")

    assert not result.ok
    assert "command not found" in result.summary
    assert result.cost_usd is None
