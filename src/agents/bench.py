"""Compare the tree with a plain single-model session on the same task: `agents bench`.

Every arm starts from the same commit, gets the same task and the same permissions, and is
judged the same way: by an acceptance command that the arms never see. The arms are:

    tree          the full delegation tree, with the planner run headless
    solo:<tier>   one headless session using that tier's model, working alone

Each arm is run several times, because one run of a model says little. Runs go one after
another, taking turns between the arms, so that no arm gets all the quiet hours.

What is left behind for each run is one branch, `bench/<bench_id>/<number>`. The numbers are
shuffled, so the diffs can be reviewed without knowing which arm wrote them. The key is in
results.jsonl under the state directory.
"""

import asyncio
import json
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import engine, paths, worktrees
from agents.config import Config, PoolMember
from agents.harness import base as harness_base
from agents.harness import get_harness

TREE = "tree"
SOLO_PREFIX = "solo:"

SOLO_PROMPT = """\
You are working alone on one task in a git repository. Nobody is watching this session and
nobody can answer questions, so make reasonable choices and finish the task.

You can edit files. You can run the test command exactly as written, if you were given one,
and `git status`, `diff`, `log`, `show`, `add` and `commit`. Other shell commands are denied,
so don't spend turns on them.

When the work is done, commit it with a plain one-line message, like
`git commit -m "Add parse_order"`, and finish with a short summary of what you did.
"""


@dataclass(frozen=True)
class Benchmark:
    """What stays the same for every run."""

    bench_id: str
    repo: Path
    base_commit: str
    task: str
    test_cmd: str | None
    accept_cmd: str
    accept_dir: Path | None


@dataclass(frozen=True)
class Finished:
    """One arm's finished work, before it is judged."""

    worktree: Path
    agent_ok: bool
    summary: str
    cost_usd: float
    cost_known: bool
    seconds: float
    # A tree run has a run id; a solo run has a working branch. Both are tidied up later.
    tree_run_id: str | None = None
    work_branch: str | None = None


def default_arms(settings: Config) -> list[str]:
    """The tree, and one solo arm for each splitter tier."""
    return [TREE, *(f"{SOLO_PREFIX}{tier.name}" for tier in settings.tiers[:-1])]


def solo_agent(settings: Config, arm: str) -> PoolMember:
    """The harness and model a `solo:<tier>` arm uses."""
    tier_name = arm.removeprefix(SOLO_PREFIX)
    for tier in settings.tiers:
        if tier.name == tier_name:
            # The leaf tier is a pool; its first member stands for it.
            return tier.splitter_agent() if tier.can_split else tier.pool[0]
    known = ", ".join(tier.name for tier in settings.tiers)
    raise KeyError(f"no tier '{tier_name}' for arm '{arm}' (tiers: {known})")


def check_arms(settings: Config, arms: list[str]) -> None:
    for arm in arms:
        if arm == TREE:
            continue
        if not arm.startswith(SOLO_PREFIX):
            raise KeyError(f"unknown arm '{arm}': use '{TREE}' or '{SOLO_PREFIX}<tier>'")
        solo_agent(settings, arm)


def run(
    directory: Path,
    settings: Config,
    *,
    task: str,
    accept_cmd: str,
    accept_dir: Path | None = None,
    test_cmd: str | None = None,
    arms: list[str] | None = None,
    runs: int = 3,
) -> list[dict[str, Any]]:
    """Run every arm `runs` times in the repo that `directory` is in. Returns one result each."""
    arms = arms or default_arms(settings)
    check_arms(settings, arms)
    benchmark = Benchmark(
        bench_id=time.strftime("%Y%m%d-%H%M%S"),
        repo=worktrees.main_repo(directory),
        base_commit=worktrees.head_commit(directory),
        task=task,
        test_cmd=test_cmd,
        accept_cmd=accept_cmd,
        accept_dir=accept_dir,
    )
    # Review branches get shuffled numbers, so a number doesn't give the arm away.
    review_numbers = list(range(1, len(arms) * runs + 1))
    random.shuffle(review_numbers)

    results: list[dict[str, Any]] = []
    for run_number in range(1, runs + 1):
        for arm in arms:
            print(f"{arm}, run {run_number} of {runs} ...", flush=True)
            if arm == TREE:
                finished = asyncio.run(_run_tree(benchmark, settings, directory))
            else:
                label = f"{arm.replace(':', '-')}-{run_number}"
                finished = asyncio.run(_run_solo(benchmark, settings, arm, label))
            review_branch = f"bench/{benchmark.bench_id}/{review_numbers.pop()}"
            result = _judge_and_tidy(benchmark, settings, finished, review_branch)
            result = {"bench_id": benchmark.bench_id, "arm": arm, "run": run_number, **result}
            _save(benchmark.bench_id, result)
            results.append(result)
    return results


async def _run_tree(benchmark: Benchmark, settings: Config, directory: Path) -> Finished:
    started = time.monotonic()
    root = engine.start_run(directory, settings)
    result = await engine.run_root_headless(
        root, settings, task=benchmark.task, test_cmd=benchmark.test_cmd
    )
    seconds = time.monotonic() - started
    worktrees.commit_all(root.worktree, "bench: uncommitted work from the planner")
    # The planner's own cost, plus everything it delegated.
    delegated = engine.status(root.run_id)["run_spent_usd"]
    return Finished(
        worktree=root.worktree,
        agent_ok=result.ok,
        summary=result.summary,
        cost_usd=(result.cost_usd or 0.0) + delegated,
        cost_known=result.cost_usd is not None,
        seconds=seconds,
        tree_run_id=root.run_id,
    )


async def _run_solo(benchmark: Benchmark, settings: Config, arm: str, label: str) -> Finished:
    agent = solo_agent(settings, arm)
    worktree = paths.bench_dir(benchmark.bench_id) / "worktrees" / label
    branch = f"bench/{benchmark.bench_id}/work/{label}"
    worktrees.add_worktree(benchmark.repo, worktree, branch, benchmark.base_commit)

    request = harness_base.NodeRequest(
        # The leaf role means: no delegate server. The solo arm really is on its own.
        role=harness_base.LEAF,
        model=agent.model,
        effort=agent.effort,
        system_prompt=SOLO_PROMPT,
        prompt=solo_prompt(benchmark.task, benchmark.test_cmd),
        worktree=worktree,
        budget_usd=settings.limits.run_budget_usd,
        env={},
        delegate_server=None,
        allowed_commands=[benchmark.test_cmd] if benchmark.test_cmd else [],
    )
    started = time.monotonic()
    result = await harness_base.run(
        get_harness(agent.harness), request, settings.limits.node_timeout_s
    )
    seconds = time.monotonic() - started
    worktrees.commit_all(worktree, "bench: uncommitted work")
    return Finished(
        worktree=worktree,
        agent_ok=result.ok,
        summary=result.summary,
        cost_usd=result.cost_usd or 0.0,
        cost_known=result.cost_usd is not None,
        seconds=seconds,
        work_branch=branch,
    )


def solo_prompt(task: str, test_cmd: str | None) -> str:
    sections = [f"## Task\n\n{task.strip()}"]
    if test_cmd:
        sections.append(f"## Test command\n\n`{test_cmd}`\n\nRun it exactly as written.")
    return "\n\n".join(sections) + "\n"


def _judge_and_tidy(
    benchmark: Benchmark, settings: Config, finished: Finished, review_branch: str
) -> dict[str, Any]:
    """Run the acceptance command on the finished work, keep one branch, remove the rest."""
    commit = worktrees.head_commit(finished.worktree)
    files, added, deleted = worktrees.diff_totals(finished.worktree, benchmark.base_commit)

    # The acceptance files go in after the work is committed, so they aren't part of it.
    if benchmark.accept_dir is not None:
        shutil.copytree(benchmark.accept_dir, finished.worktree, dirs_exist_ok=True)
    exit_code, output = engine.run_test(
        benchmark.accept_cmd, finished.worktree, settings.limits.test_timeout_s
    )

    worktrees.create_branch(benchmark.repo, review_branch, commit)
    if finished.tree_run_id is not None:
        engine.cleanup(finished.tree_run_id, delete_branches=True)
    if finished.work_branch is not None:
        worktrees.remove_worktree(benchmark.repo, finished.worktree)
        worktrees.delete_branch(benchmark.repo, finished.work_branch)

    return {
        "review_branch": review_branch,
        "commit": commit,
        "accepted": exit_code == 0,
        "accept_exit_code": exit_code,
        "accept_output": output,
        "agent_ok": finished.agent_ok,
        "cost_usd": round(finished.cost_usd, 4),
        "cost_known": finished.cost_known,
        "seconds": round(finished.seconds, 1),
        "files_changed": files,
        "lines_added": added,
        "lines_deleted": deleted,
        "tree_run_id": finished.tree_run_id,
        "summary": finished.summary,
    }


def results_path(bench_id: str) -> Path:
    return paths.bench_dir(bench_id) / "results.jsonl"


def _save(bench_id: str, result: dict[str, Any]) -> None:
    path = results_path(bench_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(result) + "\n")


def summary(results: list[dict[str, Any]]) -> str:
    """One row per arm, then the review branches without saying which arm wrote which."""
    arms = list(dict.fromkeys(result["arm"] for result in results))
    header = (
        f"{'arm':<14} {'runs':>4} {'accepted':>8} {'cost':>8} {'seconds':>8} "
        f"{'files':>6} {'+lines':>7} {'-lines':>7}"
    )
    lines = [header, "-" * len(header)]
    for arm in arms:
        group = [result for result in results if result["arm"] == arm]
        accepted = sum(1 for result in group if result["accepted"])
        lines.append(
            f"{arm:<14} {len(group):>4} {f'{accepted}/{len(group)}':>8} "
            f"{_mean(group, 'cost_usd'):>8.2f} {_mean(group, 'seconds'):>8.0f} "
            f"{_mean(group, 'files_changed'):>6.1f} {_mean(group, 'lines_added'):>7.0f} "
            f"{_mean(group, 'lines_deleted'):>7.0f}"
        )
    lines += ["", "Cost, seconds, files and lines are averages per run.", ""]

    lines.append("Branches to review (which arm wrote which is in the results file):")
    branches = [result["review_branch"] for result in results]
    for branch in sorted(branches, key=lambda branch: int(branch.rsplit("/", 1)[1])):
        lines.append(f"  {branch}")
    if results:
        lines += ["", f"Results: {results_path(results[0]['bench_id'])}"]
    return "\n".join(lines)


def _mean(results: list[dict[str, Any]], key: str) -> float:
    return sum(result[key] for result in results) / len(results)
