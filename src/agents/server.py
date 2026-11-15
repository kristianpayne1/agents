"""The delegate MCP server: `delegate`, `integrate` and `status` over stdio.

Every splitter, the root planner included, gets its own copy of this server. A copy learns
which node it speaks for from the run context in its environment. If there is none, it is
the root of a new run, which it starts the first time a tool is called.

The work itself is in engine.py. This file only describes the tools to the model.
"""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from agents import config, engine, prompts
from agents.config import Config
from agents.harness import base as harness_base


class Session:
    """Remembers which node this server speaks for."""

    def __init__(self, settings: Config) -> None:
        self.settings = settings
        self._caller = engine.caller_from_env()
        self._lock = asyncio.Lock()

    async def caller(self) -> engine.Caller:
        # The lock stops two tool calls arriving together from each starting a run.
        async with self._lock:
            if self._caller is None:
                self._caller = engine.start_run(Path.cwd(), self.settings)
            return self._caller


def build_server() -> MCPServer:
    settings = config.load()
    session = Session(settings)
    server = MCPServer("delegate", instructions=prompts.load(harness_base.SPLITTER))

    @server.tool()
    async def delegate(
        task: Annotated[str, Field(description="What the child should do, in plain words.")],
        budget_usd: Annotated[
            float,
            Field(description="The slice of your remaining budget this child's subtree may use."),
        ],
        contract: Annotated[
            str,
            Field(
                description="Interfaces, signatures and test names the child must keep to. "
                "It is passed down unchanged to any grandchildren."
            ),
        ] = "",
        files: Annotated[
            list[str] | None,
            Field(description="Files the child may edit, relative to the repo. Needed for leaves."),
        ] = None,
        test_cmd: Annotated[
            str | None,
            Field(description="The command that proves the task is done. Needed for leaves."),
        ] = None,
        kind: Annotated[
            str, Field(description=f"A tag for the logs. One of: {', '.join(engine.KINDS)}.")
        ] = "other",
    ) -> str:
        """Start a child node in its own worktree and wait for its result.

        The child's branch starts from the tip of your branch, so commit first. The server
        picks the child's tier from its depth. Returns node_id, status (passed, failed,
        refused or over_budget), branch, summary, diff_stat, test_output, cost_usd and
        attempts. The child's work stays on its branch until you call `integrate`.
        """
        caller = await session.caller()
        result = await engine.delegate(
            caller,
            settings,
            task=task,
            budget_usd=budget_usd,
            contract=contract,
            files=files,
            test_cmd=test_cmd,
            kind=kind,
        )
        return _as_text(result)

    @server.tool()
    async def integrate(
        node_ids: Annotated[
            list[str],
            Field(description="The children to merge, in order. May be empty to only run tests."),
        ],
        test_cmd: Annotated[
            str, Field(description="The command to run in your worktree after merging.")
        ],
    ) -> str:
        """Merge finished children into your branch, one at a time, then run a test command.

        If a merge conflicts, it stops there, leaves that child unmerged and returns the
        conflict to you. Review a child's result before you merge it.
        """
        caller = await session.caller()
        # The merge and the test run block, so they go in a thread.
        result = await asyncio.to_thread(
            engine.integrate, caller, settings, node_ids=node_ids, test_cmd=test_cmd
        )
        return _as_text(result)

    @server.tool()
    async def status() -> str:
        """Show the run's tree so far: each node's status and cost, your branch and
        worktree, and the budget you have left."""
        caller = await session.caller()
        result = engine.status(caller.run_id, caller.node_id)
        # A splitter has to know this before it writes tasks: leaves need files and tests.
        leaves = engine.children_are_leaves(settings, caller.depth)
        result["you"]["your_children_are"] = "leaves" if leaves else "splitters"
        return _as_text(result)

    return server


def _as_text(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2)


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
