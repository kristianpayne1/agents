"""The script the fake harness runs in place of a model.

It reads the node's prompt on stdin and looks for one line that starts with `FAKE:` followed
by JSON instructions. A task for the fake harness is therefore just that line, for example:

    FAKE: {"write": {"hello.txt": "hi\\n"}, "cost": 0.02}

Instructions (all optional):

    write       {path: content} files to write in the worktree
    commit      commit the changes (default true); false leaves them for the server to commit
    cost        the cost to report, in dollars (default 0.01)
    sleep       seconds to wait first, to make parallel runs overlap
    exit        the exit code (default 0)
    result      the final message to print (default "fake agent done"), for example one
                that asks for a decision
    delegate    a list of `delegate` arguments: act as a splitter and run these children,
                all at the same time, with one `delegate_many` call
    answers     a list of {"child": index, "answer": text, "budget_usd": dollars}: after the
                children are done, answer each listed child that came back needs_decision.
                The child is resumed with the answer, so a FAKE: line in the answer is what
                the resumed child does.
    integrate   a test command: merge the children that passed, then run it
    attempts    a list of instruction sets, one per attempt; the last is reused if the node
                is retried more times than the list is long

Like a real headless agent, it prints one JSON object when it's done, with a session id. Run
with `--resume <session id>`, it carries on that session: it reports the same id, and only
the new prompt (the answer) gives it instructions.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from agents import config, engine, worktrees

MARKER = "FAKE:"


def main() -> int:
    resumed = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
    instructions = _read_instructions(sys.stdin.read())
    if "attempts" in instructions:
        attempt = int(os.environ.get(engine.ENV_ATTEMPT, "1"))
        per_attempt = instructions["attempts"]
        instructions = per_attempt[min(attempt, len(per_attempt)) - 1]

    time.sleep(instructions.get("sleep", 0))

    worktree = Path.cwd()
    for path, content in instructions.get("write", {}).items():
        target = worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if instructions.get("commit", True):
        worktrees.commit_all(worktree, "fake agent: edits")

    children: list[dict[str, Any]] = []
    integrated: dict[str, Any] | None = None
    if "delegate" in instructions:
        children, integrated = asyncio.run(_act_as_splitter(instructions))

    result = {
        "result": instructions.get("result", "fake agent done"),
        "session_id": resumed or str(uuid.uuid4()),
        "resumed": resumed is not None,
        "total_cost_usd": instructions.get("cost", 0.01),
        "children": children,
        "integrated": integrated,
    }
    print(json.dumps(result))
    return instructions.get("exit", 0)


def _read_instructions(prompt: str) -> dict[str, Any]:
    for line in prompt.splitlines():
        if line.startswith(MARKER):
            return json.loads(line.removeprefix(MARKER))
    return {}


async def _act_as_splitter(
    instructions: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    caller = engine.caller_from_env()
    if caller is None:
        raise RuntimeError("a fake splitter needs the run context in its environment")
    settings = config.load()

    children = await engine.delegate_many(caller, settings, instructions["delegate"])

    for reply in instructions.get("answers", []):
        child = children[reply["child"]]
        if child["status"] == "needs_decision":
            children[reply["child"]] = await engine.answer(
                caller,
                settings,
                node_id=child["node_id"],
                answer=reply["answer"],
                budget_usd=reply.get("budget_usd", 1.0),
            )

    integrated = None
    if "integrate" in instructions:
        passed = [child["node_id"] for child in children if child["status"] == "passed"]
        integrated = engine.integrate(
            caller, settings, node_ids=passed, test_cmd=instructions["integrate"]
        )
    return children, integrated


if __name__ == "__main__":
    sys.exit(main())
