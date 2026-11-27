"""Small helpers shared by the tests."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from agents import config, engine
from agents.config import Config

# A config where every tier uses the fake harness, so no test calls a model.
ROLES = """
[[tier]]
name = "planner"
can_split = true
harness = "fake"
model = "fake-planner"

[[tier]]
name = "heavy"
can_split = true
harness = "fake"
model = "fake-heavy"

[[tier]]
name = "leaf"
can_split = false
pool = [{{ name = "fake-leaf", harness = "fake", model = "fake-leaf" }}]

[limits]
max_depth = {max_depth}
run_budget_usd = {run_budget_usd}
leaf_max_files = {leaf_max_files}
leaf_max_task_words = {leaf_max_task_words}
leaf_max_attempts = {leaf_max_attempts}
max_questions = {max_questions}
node_timeout_s = 60
test_timeout_s = 60
"""

DEFAULT_LIMITS = {
    "max_depth": 2,
    "run_budget_usd": 10.0,
    "leaf_max_files": 3,
    "leaf_max_task_words": 300,
    "leaf_max_attempts": 2,
    "max_questions": 3,
}


def write_roles(path: Path, **limits: float) -> Config:
    path.write_text(ROLES.format(**{**DEFAULT_LIMITS, **limits}))
    return config.load(path)


def fake(**instructions: Any) -> str:
    """A task for the fake harness. See agents/harness/fake_agent.py for the instructions."""
    return "FAKE: " + json.dumps(instructions)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def checkout_state(repo: Path) -> tuple[str, str, str]:
    """What the user would notice about their checkout: commit, branch and changes."""
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "branch", "--show-current"),
        git(repo, "status", "--porcelain"),
    )


def delegate(caller: engine.Caller, settings: Config, **arguments: Any) -> dict[str, Any]:
    return asyncio.run(engine.delegate(caller, settings, **arguments))


def delegate_all(
    caller: engine.Caller, settings: Config, calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run several delegate calls at the same time, the way parallel tool calls arrive."""

    async def run_together() -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(*(engine.delegate(caller, settings, **call) for call in calls))
        )

    return asyncio.run(run_together())


def leaf_call(path: str, content: str = "x\n", **extra: Any) -> dict[str, Any]:
    """Arguments for a leaf task that writes one file and is tested by the file existing."""
    return {
        "task": fake(write={path: content}),
        "files": [path],
        "test_cmd": f"test -f {path}",
        "kind": "feature",
        "budget_usd": 1.0,
        **extra,
    }
