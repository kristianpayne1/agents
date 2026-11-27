"""The adapter for the `claude` CLI.

The flags were checked against `claude --help` (2.1.x). If a flag stops working, that's the
place to look first.
"""

import json
from typing import Any

from agents.harness.base import LEAF, Command, Harness, HarnessResult, NodeRequest

# The name the delegate server is attached under. Its tools then appear to the model as
# mcp__delegate__delegate, mcp__delegate__integrate and mcp__delegate__status.
MCP_SERVER_NAME = "delegate"

# Git commands a headless node may run. It can look, stage and commit, but it can't switch
# branches, merge, reset or push: merging is `integrate`'s job.
GIT_COMMANDS = ["git status", "git diff", "git log", "git show", "git add", "git commit"]

# The CLI's own sub-agent tool. Nodes delegate through the delegate server or not at all.
BUILT_IN_SUBAGENT_TOOL = "Agent"

TOKEN_FIELDS = [
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
]


class ClaudeHarness(Harness):
    name = "claude"

    def build_command(self, request: NodeRequest) -> Command:
        argv = ["claude", "--print", "--output-format", "json", "--model", request.model]
        if request.resume_session_id:
            # Carry on the node's earlier session, in the same worktree, with what it knew.
            argv += ["--resume", request.resume_session_id]
        if request.effort:
            argv += ["--effort", request.effort]
        argv += [
            "--append-system-prompt",
            request.system_prompt,
            # Nobody is there to answer a permission prompt. File edits in the worktree are
            # accepted, the allowed tools below run, and anything else is denied.
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
            # The CLI stops by itself if this node's own spending reaches its budget.
            "--max-budget-usd",
            f"{request.budget_usd:.4f}",
            # Only the servers named here are loaded, so a leaf can't pick up a delegate
            # server that `agents install` registered for the user.
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(_mcp_config(request)),
            "--disallowedTools",
            BUILT_IN_SUBAGENT_TOOL,
            "--allowedTools",
            *_allowed_tools(request),
        ]
        return Command(argv=argv, stdin=request.prompt)

    def build_planner_command(self, request: NodeRequest) -> Command:
        argv = ["claude", "--model", request.model]
        if request.effort:
            argv += ["--effort", request.effort]
        argv += [
            "--append-system-prompt",
            request.system_prompt,
            "--mcp-config",
            json.dumps(_mcp_config(request)),
            "--allowedTools",
            f"mcp__{MCP_SERVER_NAME}",
        ]
        if request.prompt:
            # `--` ends the tool list, so the prompt isn't read as another tool name.
            argv += ["--", request.prompt]
        return Command(argv=argv)

    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> HarnessResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            output = (stderr or stdout).strip()[-2000:]
            return HarnessResult(
                ok=False,
                summary=f"claude exited with code {exit_code} and no JSON result: {output}",
                cost_usd=None,
            )

        usage = payload.get("usage") or {}
        tokens = {name: int(usage.get(name) or 0) for name in TOKEN_FIELDS}
        # On an error (out of budget, out of turns) there's a subtype but no result text.
        summary = payload.get("result") or f"claude stopped: {payload.get('subtype', 'unknown')}"
        ok = exit_code == 0 and not payload.get("is_error", False)
        return HarnessResult(
            ok=ok,
            summary=summary,
            cost_usd=self.extract_cost(payload),
            tokens=tokens,
            permission_denials=len(payload.get("permission_denials") or []),
            session_id=str(payload.get("session_id") or ""),
        )

    def extract_cost(self, payload: dict[str, Any]) -> float | None:
        cost = payload.get("total_cost_usd")
        return float(cost) if isinstance(cost, int | float) else None


def _mcp_config(request: NodeRequest) -> dict[str, Any]:
    if request.delegate_server is None:
        return {"mcpServers": {}}
    command, *args = request.delegate_server
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {"command": command, "args": args, "env": request.env},
        }
    }


def _allowed_tools(request: NodeRequest) -> list[str]:
    rules: list[str] = []
    for command in GIT_COMMANDS + request.allowed_commands:
        # The first rule matches the bare command, the second the command with arguments.
        rules += [f"Bash({command})", f"Bash({command} *)"]
    if request.role != LEAF and request.delegate_server is not None:
        rules.append(f"mcp__{MCP_SERVER_NAME}")
    return rules
