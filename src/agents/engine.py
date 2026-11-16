"""What `delegate`, `integrate` and `status` actually do.

server.py only exposes these functions as MCP tools. Keeping the work here means the tests
(and the fake agent) can call it directly, with no MCP client in between.
"""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import checks, config, log, paths, prompts, router, tree, worktrees
from agents.config import Config, PoolMember
from agents.harness import base as harness_base
from agents.harness import get_harness

# The run context every node gets in its environment.
ENV_RUN_ID = "AGENTS_RUN_ID"
ENV_NODE_ID = "AGENTS_NODE_ID"
ENV_DEPTH = "AGENTS_DEPTH"
ENV_BUDGET = "AGENTS_BUDGET_USD"
ENV_WORKTREE = "AGENTS_WORKTREE"
ENV_ATTEMPT = "AGENTS_ATTEMPT"

KINDS = ("tests", "rename", "boilerplate", "component", "migration", "feature", "other")

# How a splitter's agent CLI starts its own copy of the delegate server.
SERVER_COMMAND = [sys.executable, "-m", "agents.server"]

# Long output (test runs, conflict diffs) is cut to its last part before it goes to a model.
OUTPUT_LIMIT = 4000
TEST_TIMEOUT_EXIT_CODE = 124


@dataclass(frozen=True)
class Caller:
    """The node that is calling a tool."""

    run_id: str
    node_id: str
    depth: int
    worktree: Path


@dataclass
class Outcome:
    """How one node ended."""

    status: str
    summary: str
    cost_usd: float = 0.0
    cost_known: bool = True
    attempts: int = 0
    test_output: str = ""
    test_exit_code: int | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    permission_denials: int = 0


def caller_from_env() -> Caller | None:
    """The calling node, from the run context in the environment. None if there isn't one."""
    run_id = os.environ.get(ENV_RUN_ID)
    if not run_id:
        return None
    return Caller(
        run_id=run_id,
        node_id=os.environ[ENV_NODE_ID],
        depth=int(os.environ[ENV_DEPTH]),
        worktree=Path(os.environ[ENV_WORKTREE]),
    )


def context_env(
    settings: Config,
    *,
    run_id: str,
    node_id: str,
    depth: int,
    budget_usd: float,
    worktree: Path,
    attempt: int = 1,
) -> dict[str, str]:
    """The environment variables that tell a node (and its delegate server) who it is."""
    return {
        ENV_RUN_ID: run_id,
        ENV_NODE_ID: node_id,
        ENV_DEPTH: str(depth),
        ENV_BUDGET: f"{budget_usd:.4f}",
        ENV_WORKTREE: str(worktree),
        ENV_ATTEMPT: str(attempt),
        # The whole run uses the config and state directory it started with.
        config.ENV_CONFIG: str(settings.source),
        paths.ENV_STATE_DIR: str(paths.state_dir()),
    }


# --- starting a run ----------------------------------------------------------------------


def start_run(directory: Path, settings: Config) -> Caller:
    """Start a run for the repo that `directory` is in, and return its root node.

    The root gets its own worktree, branched from the current HEAD, so the user's checkout
    is never touched.
    """
    repo = worktrees.main_repo(directory)
    base_commit = worktrees.head_commit(directory)
    planner = settings.tiers[0].splitter_agent()

    run_id = time.strftime("%Y%m%d-%H%M")
    if paths.run_db(run_id).exists():
        run_id = time.strftime("%Y%m%d-%H%M%S")
    db = paths.run_db(run_id)
    branch = worktrees.branch_name(run_id, tree.ROOT_ID)
    worktree = paths.run_worktrees(repo.name, run_id) / tree.ROOT_ID

    tree.create_run(
        db,
        run_id=run_id,
        repo_path=str(repo),
        repo_name=repo.name,
        base_commit=base_commit,
        budget_usd=settings.limits.run_budget_usd,
        root_tier=planner.name,
        root_harness=planner.harness,
        root_model=planner.model,
        root_branch=branch,
        root_worktree=str(worktree),
    )
    try:
        worktrees.add_worktree(repo, worktree, branch, base_commit)
    except worktrees.GitError:
        db.unlink()
        raise
    return Caller(run_id=run_id, node_id=tree.ROOT_ID, depth=0, worktree=worktree)


def latest_run_id() -> str | None:
    """The most recently started run on this machine."""
    # Run ids are timestamps, so the last one by name is the newest.
    run_ids = sorted(path.stem for path in paths.runs_dir().glob("*.sqlite"))
    return run_ids[-1] if run_ids else None


async def run_root_headless(
    root: Caller, settings: Config, *, task: str, test_cmd: str | None
) -> harness_base.HarnessResult:
    """Run the root planner with nobody at the keyboard, and return its result.

    `agents run` opens the planner as an interactive session instead. This is for the
    benchmark and the live test, where the planner's cost has to be measured too.
    """
    planner = settings.tiers[0].splitter_agent()
    budget = settings.limits.run_budget_usd
    request = harness_base.NodeRequest(
        role=harness_base.SPLITTER,
        model=planner.model,
        effort=planner.effort,
        system_prompt=prompts.load(harness_base.SPLITTER),
        prompt=node_prompt(
            is_leaf=False,
            task=task,
            contract="",
            files=[],
            test_cmd=test_cmd,
            budget_usd=budget,
            children_are_leaves=children_are_leaves(settings, root.depth),
            failed_test_output="",
        ),
        worktree=root.worktree,
        budget_usd=budget,
        env=context_env(
            settings,
            run_id=root.run_id,
            node_id=root.node_id,
            depth=root.depth,
            budget_usd=budget,
            worktree=root.worktree,
        ),
        delegate_server=SERVER_COMMAND,
        allowed_commands=[test_cmd] if test_cmd else [],
    )
    return await harness_base.run(
        get_harness(planner.harness), request, settings.limits.node_timeout_s
    )


# --- delegate ----------------------------------------------------------------------------


async def delegate(
    caller: Caller,
    settings: Config,
    *,
    task: str,
    budget_usd: float,
    contract: str = "",
    files: list[str] | None = None,
    test_cmd: str | None = None,
    kind: str = "other",
) -> dict[str, Any]:
    """Start a child node, wait for it, and return its result."""
    started = time.time()
    db = paths.run_db(caller.run_id)
    files = list(files or [])
    test_cmd = test_cmd.strip() if test_cmd and test_cmd.strip() else None

    # The server picks the tier from the depth. The caller never names a model.
    depth = caller.depth + 1
    tier = settings.tier_for_depth(depth)
    is_leaf = not tier.can_split
    if is_leaf:
        leaf_task = router.LeafTask(kind, task, contract, files, test_cmd or "")
        agent = router.route(leaf_task, tier.pool)
    else:
        agent = tier.splitter_agent()

    refusal = _refusal(
        caller,
        settings,
        is_leaf=is_leaf,
        task=task,
        contract=contract,
        files=files,
        test_cmd=test_cmd,
        kind=kind,
        budget_usd=budget_usd,
    )
    # One step records the child and reserves its budget, so parallel siblings can't
    # both be given the same money.
    node = tree.add_child(
        db,
        parent_id=caller.node_id,
        depth=depth,
        tier=tier.name,
        harness=agent.harness,
        model=agent.model,
        kind=kind,
        task=task,
        budget_usd=budget_usd,
        refusal=refusal,
    )

    outcome = Outcome(status=node.status, summary=node.summary)
    diff_stat = ""
    if node.status == tree.RUNNING:
        try:
            node = _create_workspace(caller, node)
            outcome = await _run_node(
                settings,
                caller.run_id,
                node,
                agent,
                is_leaf=is_leaf,
                task=task,
                contract=contract,
                files=files,
                test_cmd=test_cmd,
            )
            diff_stat = worktrees.diff_stat(Path(node.worktree), node.base_commit)
        except Exception as error:
            # Whatever goes wrong with one child, the parent gets an answer, not a crash.
            outcome = Outcome(status=tree.FAILED, summary=f"Internal error: {error}")
        node = tree.finish_node(
            db,
            node.node_id,
            status=outcome.status,
            cost_usd=outcome.cost_usd,
            attempts=outcome.attempts,
            summary=outcome.summary,
            test_exit_code=outcome.test_exit_code,
        )

    log.write(
        {
            "event": log.NODE,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": caller.run_id,
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "depth": node.depth,
            "tier": node.tier,
            "model": node.model,
            "kind": node.kind,
            "file_count": len(files),
            "task_words": checks.word_count(task, contract),
            "attempts": outcome.attempts,
            "status": outcome.status,
            "cost_usd": outcome.cost_usd,
            "cost_known": outcome.cost_known,
            "tokens": outcome.tokens,
            "permission_denials": outcome.permission_denials,
            "duration_s": round(time.time() - started, 1),
            "test_exit_code": outcome.test_exit_code,
        }
    )
    return {
        "node_id": node.node_id,
        "status": outcome.status,
        "branch": node.branch,
        "summary": outcome.summary,
        "diff_stat": diff_stat,
        "test_output": outcome.test_output,
        "cost_usd": outcome.cost_usd,
        "attempts": outcome.attempts,
    }


def _refusal(
    caller: Caller,
    settings: Config,
    *,
    is_leaf: bool,
    task: str,
    contract: str,
    files: list[str],
    test_cmd: str | None,
    kind: str,
    budget_usd: float,
) -> str | None:
    """Why this child may not start, or None if it may."""
    if not settings.tier_for_depth(caller.depth).can_split:
        return "Refused: a leaf can't delegate."
    if not task.strip():
        return "Refused: `task` is empty."
    if kind not in KINDS:
        return f"Refused: unknown kind '{kind}'. Use one of: {', '.join(KINDS)}."
    if budget_usd <= 0:
        return "Refused: `budget_usd` must be more than 0."
    if is_leaf:
        return checks.check_leaf_task(
            task=task, contract=contract, files=files, test_cmd=test_cmd, limits=settings.limits
        )
    return None


def _create_workspace(caller: Caller, node: tree.Node) -> tree.Node:
    """Give the child a branch and worktree, starting from the tip of the caller's branch."""
    db = paths.run_db(caller.run_id)
    run = tree.get_run(db)
    parent = tree.get_node(db, caller.node_id)
    if parent is None:
        raise KeyError(f"no node '{caller.node_id}' in run {caller.run_id}")

    repo = Path(run.repo_path)
    branch = worktrees.branch_name(run.run_id, node.node_id)
    worktree = paths.run_worktrees(run.repo_name, run.run_id) / node.node_id
    # Several delegate servers may add worktrees to the same repo at once.
    with worktrees.file_lock(paths.lock_file(f"{run.run_id}.git")):
        base_commit = worktrees.head_commit(repo, parent.branch)
        worktrees.add_worktree(repo, worktree, branch, base_commit)

    tree.set_workspace(
        db, node.node_id, branch=branch, worktree=str(worktree), base_commit=base_commit
    )
    updated = tree.get_node(db, node.node_id)
    assert updated is not None
    return updated


async def _run_node(
    settings: Config,
    run_id: str,
    node: tree.Node,
    agent: PoolMember,
    *,
    is_leaf: bool,
    task: str,
    contract: str,
    files: list[str],
    test_cmd: str | None,
) -> Outcome:
    """Run the child's agent, then its test command. A leaf is retried if the test fails."""
    limits = settings.limits
    harness = get_harness(agent.harness)
    worktree = Path(node.worktree)
    role = harness_base.LEAF if is_leaf else harness_base.SPLITTER
    max_attempts = limits.leaf_max_attempts if is_leaf else 1

    outcome = Outcome(status=tree.FAILED, summary="")
    for attempt in range(1, max_attempts + 1):
        budget_left = node.budget_usd - outcome.cost_usd
        if budget_left <= 0:
            outcome.summary += "\nNot retried: this task's budget is used up."
            break
        outcome.attempts = attempt

        request = harness_base.NodeRequest(
            role=role,
            model=agent.model,
            effort=agent.effort,
            system_prompt=prompts.load(role),
            prompt=node_prompt(
                is_leaf=is_leaf,
                task=task,
                contract=contract,
                files=files,
                test_cmd=test_cmd,
                budget_usd=budget_left,
                children_are_leaves=children_are_leaves(settings, node.depth),
                failed_test_output=outcome.test_output if attempt > 1 else "",
            ),
            worktree=worktree,
            budget_usd=budget_left,
            env=context_env(
                settings,
                run_id=run_id,
                node_id=node.node_id,
                depth=node.depth,
                budget_usd=budget_left,
                worktree=worktree,
                attempt=attempt,
            ),
            delegate_server=None if is_leaf else SERVER_COMMAND,
            allowed_commands=[test_cmd] if test_cmd else [],
        )
        result = await harness_base.run(harness, request, limits.node_timeout_s)

        outcome.summary = result.summary
        if result.cost_usd is None:
            outcome.cost_known = False
        else:
            outcome.cost_usd += result.cost_usd
        for name, count in result.tokens.items():
            outcome.tokens[name] = outcome.tokens.get(name, 0) + count
        outcome.permission_denials += result.permission_denials

        # Nodes are asked to commit their own work. If one didn't, do it for it.
        worktrees.commit_all(worktree, f"agents: uncommitted work from {node.node_id}")

        if test_cmd:
            exit_code, output = await asyncio.to_thread(
                run_test, test_cmd, worktree, limits.test_timeout_s
            )
            outcome.test_exit_code = exit_code
            outcome.test_output = output
            passed = exit_code == 0
        else:
            passed = result.ok
        if passed:
            outcome.status = tree.PASSED
            break

    if is_leaf:
        outside = [
            path
            for path in worktrees.changed_files(worktree, node.base_commit)
            if path not in files
        ]
        if outside:
            outcome.summary += f"\nWarning: edited files outside its list: {', '.join(outside)}"
    return outcome


def node_prompt(
    *,
    is_leaf: bool,
    task: str,
    contract: str,
    files: list[str],
    test_cmd: str | None,
    budget_usd: float,
    children_are_leaves: bool,
    failed_test_output: str,
) -> str:
    sections = [f"## Task\n\n{task.strip()}"]
    if contract.strip():
        rule = (
            "Keep to this exactly."
            if is_leaf
            else "Keep to this exactly, and pass it on unchanged with every task you delegate."
        )
        sections.append(f"## Contract\n\n{rule}\n\n{contract.strip()}")
    if files:
        heading = "## Files you may edit" if is_leaf else "## Files this task covers"
        sections.append(heading + "\n\n" + "\n".join(f"- {path}" for path in files))
    if test_cmd:
        sections.append(
            f"## Test command\n\n`{test_cmd}`\n\n"
            "Run it exactly as written. It is run again after you finish, and that run "
            "decides whether the task passed. If you are denied permission to run it, don't "
            "try other ways: finish the work and commit it."
        )
    if not is_leaf:
        sections.append(
            "## Budget\n\n"
            f"You have ${budget_usd:.2f} for this task, including everything you delegate."
        )
        sections.append("## Your children\n\n" + _children_note(children_are_leaves))
        sections.append(
            "## This session\n\n"
            "Nobody is watching this session and nobody can answer questions, so make "
            "reasonable choices and finish. You can run `git status`, `diff`, `log`, `show`, "
            "`add` and `commit`, plus your test command if you were given one. Other shell "
            "commands are denied. To run any other test command, call `integrate`: with an "
            "empty list of nodes it only runs the command."
        )
    if failed_test_output:
        sections.append(
            "## Your previous attempt\n\n"
            "Your earlier changes are still in the worktree, but the test command failed. "
            "Fix the problem. This was its output:\n\n"
            f"```\n{failed_test_output}\n```"
        )
    return "\n\n".join(sections) + "\n"


def children_are_leaves(settings: Config, depth: int) -> bool:
    """Whether a node at this depth delegates to leaves (rather than to more splitters)."""
    return not settings.tier_for_depth(depth + 1).can_split


def _children_note(leaves: bool) -> str:
    if leaves:
        return (
            "The nodes you delegate to are leaves. Every task you hand out needs `files` "
            "and a `test_cmd`, and has to fit the leaf size limits, or it is refused."
        )
    return (
        "The nodes you delegate to are splitters, which split the work further. "
        "`files` and `test_cmd` are optional for them."
    )


def run_test(test_cmd: str, worktree: Path, timeout_s: int) -> tuple[int, str]:
    """Run a test command in a worktree. Returns its exit code and the end of its output."""
    # The command gets its own process group, so that on a timeout everything it started
    # can be stopped, not just the shell.
    process = subprocess.Popen(
        test_cmd,
        shell=True,
        cwd=worktree,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return TEST_TIMEOUT_EXIT_CODE, f"`{test_cmd}` timed out after {timeout_s}s"
    return process.returncode, _tail(output)


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= OUTPUT_LIMIT:
        return text
    return "[earlier output cut]\n" + text[-OUTPUT_LIMIT:]


# --- integrate ---------------------------------------------------------------------------


def integrate(
    caller: Caller, settings: Config, *, node_ids: list[str], test_cmd: str
) -> dict[str, Any]:
    """Merge finished children into the caller's branch, then run a test command.

    This blocks while the tests run, so the server calls it in a thread.
    """
    db = paths.run_db(caller.run_id)
    if not test_cmd.strip():
        return _integrate_error("`test_cmd` is missing.")

    children: list[tree.Node] = []
    for node_id in node_ids:
        node = tree.get_node(db, node_id)
        if node is None or node.parent_id != caller.node_id:
            return _integrate_error(f"'{node_id}' is not one of your children.")
        if node.status not in (tree.PASSED, tree.FAILED):
            return _integrate_error(f"'{node_id}' is {node.status}, so it has nothing to merge.")
        children.append(node)

    merged: list[str] = []
    with worktrees.file_lock(paths.lock_file(f"{caller.run_id}.{caller.node_id}")):
        if worktrees.is_dirty(caller.worktree, include_untracked=False):
            return _integrate_error("You have uncommitted changes. Commit them, then try again.")

        for position, node in enumerate(children):
            message = f"Merge {node.node_id} ({node.kind})"
            result = worktrees.merge_no_ff(caller.worktree, node.branch, message)
            if not result.merged:
                # Stop here and hand the conflict back. Resolving it is the caller's call.
                tree.set_merge_state(db, node.node_id, tree.CONFLICT)
                _log_integration(caller, "conflict", merged, node.node_id, None)
                return {
                    "status": "conflict",
                    "merged": merged,
                    "conflict": {
                        "node_id": node.node_id,
                        "branch": node.branch,
                        "files": list(result.conflict_files),
                        "details": _tail(result.details),
                    },
                    "not_merged": [child.node_id for child in children[position:]],
                    "message": (
                        f"{node.node_id} conflicts with your branch and was not merged. "
                        "Your worktree is unchanged apart from the merges listed in `merged`. "
                        "Delegate that task again so it starts from your current branch, "
                        "or make the change yourself."
                    ),
                }
            tree.set_merge_state(db, node.node_id, tree.MERGED)
            merged.append(node.node_id)

        exit_code, output = run_test(test_cmd, caller.worktree, settings.limits.test_timeout_s)

    status = "passed" if exit_code == 0 else "tests_failed"
    _log_integration(caller, status, merged, None, exit_code)
    return {
        "status": status,
        "merged": merged,
        "test_exit_code": exit_code,
        "test_output": output,
    }


def _integrate_error(message: str) -> dict[str, Any]:
    return {"status": "error", "merged": [], "message": message}


def _log_integration(
    caller: Caller,
    status: str,
    merged: list[str],
    conflict_node_id: str | None,
    test_exit_code: int | None,
) -> None:
    log.write(
        {
            "event": log.INTEGRATE,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": caller.run_id,
            "node_id": caller.node_id,
            "status": status,
            "merged": merged,
            "conflict_node_id": conflict_node_id,
            "test_exit_code": test_exit_code,
        }
    )


# --- status and cleanup ------------------------------------------------------------------


def status(run_id: str, node_id: str = tree.ROOT_ID) -> dict[str, Any]:
    """The run's tree so far, and what `node_id` has left to spend."""
    db = paths.run_db(run_id)
    run = tree.get_run(db)
    nodes = tree.all_nodes(db)
    you = next(node for node in nodes if node.node_id == node_id)
    return {
        "run_id": run.run_id,
        "repo": run.repo_path,
        "run_budget_usd": run.budget_usd,
        # The root planner's own session isn't measured, so this is the children's spending.
        "run_spent_usd": round(sum(node.cost_usd for node in nodes), 4),
        "you": {
            "node_id": you.node_id,
            "branch": you.branch,
            "worktree": you.worktree,
            "budget_left_usd": round(tree.remaining_usd(db, node_id), 4),
        },
        "nodes": [
            {
                "node_id": node.node_id,
                "tier": node.tier,
                "kind": node.kind,
                "status": node.status,
                "merge": node.merge_state or "not merged",
                "cost_usd": round(node.cost_usd, 4),
                "budget_usd": node.budget_usd,
                "attempts": node.attempts,
                "branch": node.branch,
                "task": _first_line(node.task),
            }
            for node in nodes
        ],
    }


def _first_line(text: str, limit: int = 100) -> str:
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


def cleanup(run_id: str, *, delete_branches: bool) -> list[str]:
    """Remove a run's worktrees, and its branches too if asked. Returns what was removed."""
    db = paths.run_db(run_id)
    if not db.exists():
        raise KeyError(f"no run '{run_id}'")
    run = tree.get_run(db)
    repo = Path(run.repo_path)
    nodes = tree.all_nodes(db)

    removed: list[str] = []
    for node in nodes:
        if node.worktree and Path(node.worktree).exists():
            worktrees.remove_worktree(repo, Path(node.worktree))
            removed.append(f"worktree {node.worktree}")
    shutil.rmtree(paths.run_worktrees(run.repo_name, run.run_id), ignore_errors=True)
    worktrees.prune_worktrees(repo)

    if delete_branches:
        for node in nodes:
            if node.branch and worktrees.delete_branch(repo, node.branch):
                removed.append(f"branch {node.branch}")
    return removed
