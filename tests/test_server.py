"""The MCP server itself, started as a real subprocess and called over stdio."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from helpers import checkout_state, fake, leaf_call
from mcp import Client, StdioServerParameters

from agents import engine, paths, tree
from agents.config import Config


def server_parameters(cwd: Path, extra_env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agents.server"],
        env={**os.environ, **extra_env},
        cwd=str(cwd),
    )


async def call(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)  # pyright: ignore[reportAttributeAccessIssue]


def test_tools_work_over_stdio_and_run_in_parallel(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    context = engine.context_env(
        leaf_settings,
        run_id=root.run_id,
        node_id=root.node_id,
        depth=root.depth,
        budget_usd=10.0,
        worktree=root.worktree,
    )

    async def scenario() -> tuple[list[str], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        async with Client(server_parameters(repo, context)) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            children = await asyncio.gather(
                call(client, "delegate", leaf_call("a.txt", task=slow("a.txt"))),
                call(client, "delegate", leaf_call("b.txt", task=slow("b.txt"))),
            )
            merged = await call(
                client,
                "integrate",
                {
                    "node_ids": [child["node_id"] for child in children],
                    "test_cmd": "test -f a.txt && test -f b.txt",
                },
            )
            status = await call(client, "status", {})
            return tools, list(children), merged, status

    tools, children, merged, status = asyncio.run(scenario())

    assert sorted(tools) == ["delegate", "delegate_many", "integrate", "status"]
    assert [child["status"] for child in children] == ["passed", "passed"]
    assert merged["status"] == "passed"
    assert status["you"]["node_id"] == "root"
    assert status["you"]["your_children_are"] == "leaves"
    assert len(status["nodes"]) == 3
    # The two delegate calls were handled at the same time, not one after the other.
    db = paths.run_db(root.run_id)
    first, second = tree.get_node(db, "root.1"), tree.get_node(db, "root.2")
    assert first is not None and second is not None
    assert first.finished_at is not None and second.finished_at is not None
    assert first.started_at < second.finished_at
    assert second.started_at < first.finished_at


def test_server_without_a_run_context_starts_a_run(repo: Path, leaf_settings: Config) -> None:
    """This is the `agents install` path: a plain session whose server begins the run."""
    before = checkout_state(repo)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(server_parameters(repo, {})) as client:
            child = await call(client, "delegate", leaf_call("a.txt"))
            return child, await call(client, "status", {})

    child, status = asyncio.run(scenario())

    assert child["status"] == "passed"
    assert child["node_id"] == "root.1"
    assert status["repo"] == str(repo.resolve())
    assert Path(status["you"]["worktree"]).is_relative_to(paths.state_dir())
    assert checkout_state(repo) == before


def slow(path: str) -> str:
    return fake(write={path: "x\n"}, sleep=0.3)


def test_delegate_many_over_stdio(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    context = engine.context_env(
        leaf_settings,
        run_id=root.run_id,
        node_id=root.node_id,
        depth=root.depth,
        budget_usd=10.0,
        worktree=root.worktree,
    )

    async def scenario() -> list[dict[str, Any]]:
        async with Client(server_parameters(repo, context)) as client:
            children = [
                leaf_call("a.txt", task=slow("a.txt")),
                leaf_call("b.txt", task=slow("b.txt")),
            ]
            result = await client.call_tool("delegate_many", {"children": children})
            assert not result.is_error, result.content
            return json.loads(result.content[0].text)  # pyright: ignore[reportAttributeAccessIssue]

    results = asyncio.run(scenario())

    assert [r["status"] for r in results] == ["passed", "passed"]
    db = paths.run_db(root.run_id)
    first, second = tree.get_node(db, "root.1"), tree.get_node(db, "root.2")
    assert first is not None and second is not None
    assert first.finished_at is not None and second.finished_at is not None
    assert first.started_at < second.finished_at
    assert second.started_at < first.finished_at
