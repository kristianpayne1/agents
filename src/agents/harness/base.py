"""The harness adapter interface.

A harness is a coding-agent CLI that can run headless (print a result and exit). An adapter
knows three things about its CLI: how to build the command for a node, how to read the
result it prints, and where the cost is in that result. Everything else (worktrees, budgets,
retries, tests) is the same for every harness and lives in engine.py.
"""

import asyncio
import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPLITTER = "splitter"
LEAF = "leaf"


@dataclass(frozen=True)
class NodeRequest:
    """Everything a harness needs to run one node."""

    role: str  # SPLITTER or LEAF
    model: str
    effort: str | None
    system_prompt: str
    prompt: str
    worktree: Path
    budget_usd: float
    # The run context (AGENTS_RUN_ID and friends) for the node and its delegate server.
    env: dict[str, str]
    # The command that starts the delegate MCP server. None for leaves: they can't delegate.
    delegate_server: list[str] | None
    # Shell commands the node may run besides a few git ones. In practice, its test command.
    allowed_commands: list[str]


@dataclass(frozen=True)
class Command:
    argv: list[str]
    stdin: str = ""


@dataclass(frozen=True)
class HarnessResult:
    ok: bool
    summary: str
    # None means the CLI didn't report a cost; the tokens are still logged.
    cost_usd: float | None
    tokens: dict[str, int] = field(default_factory=dict)
    # How many tool calls the CLI refused because the node had no permission for them.
    permission_denials: int = 0


class Harness(ABC):
    name: str

    @abstractmethod
    def build_command(self, request: NodeRequest) -> Command:
        """The headless command that runs this node. The prompt goes in on stdin."""

    @abstractmethod
    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> HarnessResult:
        """Turn what the CLI printed into a result."""

    @abstractmethod
    def extract_cost(self, payload: dict[str, Any]) -> float | None:
        """The cost in dollars from the CLI's parsed output, or None if it isn't there."""

    @abstractmethod
    def build_planner_command(self, request: NodeRequest) -> Command:
        """The interactive command for the root planner (used by `agents run`)."""


async def run(harness: Harness, request: NodeRequest, timeout_s: int) -> HarnessResult:
    """Run one node to the end in its worktree and return its parsed result."""
    command = harness.build_command(request)
    process = await asyncio.create_subprocess_exec(
        *command.argv,
        cwd=request.worktree,
        env={**os.environ, **request.env},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Its own process group, so a timeout can stop the agent and whatever it started.
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(command.stdin.encode()), timeout=timeout_s
        )
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return HarnessResult(ok=False, summary=f"timed out after {timeout_s}s", cost_usd=None)

    exit_code = process.returncode if process.returncode is not None else 1
    return harness.parse_result(exit_code, stdout.decode(), stderr.decode())
