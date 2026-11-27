"""`delegate`: branching, refusals, budgets, retries, the depth cap and parallel siblings."""

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import checkout_state, delegate, delegate_all, fake, git, leaf_call

from agents import engine, log, paths, tree
from agents.config import Config
from agents.harness import base as harness_base
from agents.harness.base import Harness, HarnessResult, NodeRequest


def test_child_branches_from_the_parents_tip(repo: Path, leaf_settings: Config) -> None:
    before = checkout_state(repo)
    root = engine.start_run(repo, leaf_settings)
    # The parent commits something after the run started. The child must see it.
    (root.worktree / "contract.txt").write_text("the contract\n")
    git(root.worktree, "add", "--all")
    git(root.worktree, "commit", "--message", "add the contract")
    root_tip = git(root.worktree, "rev-parse", "HEAD")

    result = delegate(root, leaf_settings, **leaf_call("out.txt"))

    assert result["status"] == "passed"
    assert result["node_id"] == "root.1"
    assert result["branch"] == f"agents/{root.run_id}/root.1"
    assert "out.txt" in result["diff_stat"]
    node = tree.get_node(paths.run_db(root.run_id), "root.1")
    assert node is not None
    assert node.base_commit == root_tip
    assert (Path(node.worktree) / "contract.txt").exists()
    assert git(repo, "merge-base", result["branch"], root_tip) == root_tip
    # The child's work is on its own branch, not the parent's, until `integrate`.
    assert not (root.worktree / "out.txt").exists()
    assert checkout_state(repo) == before


def test_worktrees_live_outside_the_repo(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    delegate(root, leaf_settings, **leaf_call("out.txt"))

    run_dir = paths.state_dir() / "worktrees" / "repo" / root.run_id
    assert root.worktree == run_dir / "root"
    assert (run_dir / "root.1" / "out.txt").exists()
    assert not root.worktree.is_relative_to(repo)


def test_leaf_without_files_is_refused(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", files=[]))

    assert result["status"] == "refused"
    assert "`files` is missing" in result["summary"]
    assert "Split this further" in result["summary"]


def test_leaf_with_too_many_files_is_refused(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", files=["a", "b", "c", "d"]))

    assert result["status"] == "refused"
    assert "4 files is over the limit of 3" in result["summary"]


def test_leaf_without_test_cmd_is_refused(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", test_cmd=None))

    assert result["status"] == "refused"
    assert "`test_cmd` is missing" in result["summary"]


def test_leaf_with_too_many_words_is_refused(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", contract="word " * 301))

    assert result["status"] == "refused"
    assert "over the limit of 300" in result["summary"]


def test_refused_task_runs_nothing_and_is_recorded(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    db = paths.run_db(root.run_id)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", files=[]))

    node = tree.get_node(db, result["node_id"])
    assert node is not None
    assert node.status == tree.REFUSED
    assert node.worktree == ""
    assert result["branch"] == ""
    assert tree.remaining_usd(db, "root") == 10.0
    statuses = {entry["node_id"]: entry["status"] for entry in engine.status(root.run_id)["nodes"]}
    assert statuses["root.1"] == "refused"
    assert log.read()[-1]["status"] == "refused"


def test_splitter_task_needs_no_files_or_test_cmd(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)

    result = delegate(root, settings, task=fake(), kind="feature", budget_usd=1.0)

    assert result["status"] == "passed"
    node = tree.get_node(paths.run_db(root.run_id), result["node_id"])
    assert node is not None
    assert node.tier == "heavy"


def test_unknown_kind_is_refused(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", kind="magic"))

    assert result["status"] == "refused"
    assert "unknown kind 'magic'" in result["summary"]


def test_budget_is_reserved_then_the_unspent_part_refunded(
    repo: Path, leaf_settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = engine.start_run(repo, leaf_settings)
    db = paths.run_db(root.run_id)

    # Look at the parent's budget at the moment the child's agent starts.
    left_while_running: list[float] = []
    real_run = harness_base.run

    async def run_and_look(harness: Harness, request: NodeRequest, timeout_s: int) -> HarnessResult:
        left_while_running.append(tree.remaining_usd(db, "root"))
        return await real_run(harness, request, timeout_s)

    monkeypatch.setattr(harness_base, "run", run_and_look)

    task = fake(write={"out.txt": "x\n"}, cost=0.5)
    result = delegate(root, leaf_settings, **leaf_call("out.txt", task=task, budget_usd=4.0))

    assert result["status"] == "passed"
    assert result["cost_usd"] == 0.5
    assert left_while_running == [6.0]
    assert tree.remaining_usd(db, "root") == 9.5


def test_asking_for_more_than_is_left_is_over_budget(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    db = paths.run_db(root.run_id)

    result = delegate(root, leaf_settings, **leaf_call("out.txt", budget_usd=11.0))

    assert result["status"] == "over_budget"
    assert "only $10.00 is left" in result["summary"]
    assert tree.remaining_usd(db, "root") == 10.0
    assert log.read()[-1]["status"] == "over_budget"


def test_parallel_siblings_cannot_reserve_the_same_money(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    results = delegate_all(
        root,
        leaf_settings,
        [leaf_call("a.txt", budget_usd=6.0), leaf_call("b.txt", budget_usd=6.0)],
    )

    assert sorted(result["status"] for result in results) == ["over_budget", "passed"]


def test_leaf_retries_then_fails(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    task = fake(write={"out.txt": "bad\n"}, cost=0.1)
    result = delegate(
        root, leaf_settings, **leaf_call("out.txt", task=task, test_cmd="grep good out.txt")
    )

    assert result["status"] == "failed"
    assert result["attempts"] == 2
    assert result["cost_usd"] == 0.2
    entry = log.read()[-1]
    assert entry["status"] == "failed"
    assert entry["attempts"] == 2
    assert entry["test_exit_code"] == 1


def test_leaf_retries_then_passes(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    task = fake(attempts=[{"write": {"out.txt": "bad\n"}}, {"write": {"out.txt": "good\n"}}])
    result = delegate(
        root, leaf_settings, **leaf_call("out.txt", task=task, test_cmd="grep good out.txt")
    )

    assert result["status"] == "passed"
    assert result["attempts"] == 2
    assert "good" in result["test_output"]


def test_retry_prompt_carries_the_failed_test_output() -> None:
    prompt = engine.node_prompt(
        is_leaf=True,
        task="Implement parse_order()",
        contract="def parse_order(text: str) -> Order",
        files=["orders.py"],
        test_cmd="pytest tests/test_orders.py",
        budget_usd=1.0,
        children_are_leaves=False,
        failed_test_output="AssertionError: 3 != 4",
    )

    assert "## Files you may edit\n\n- orders.py" in prompt
    assert "`pytest tests/test_orders.py`" in prompt
    assert "def parse_order(text: str) -> Order" in prompt
    assert "AssertionError: 3 != 4" in prompt


def test_leaf_that_does_not_commit_is_committed_by_the_server(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)

    task = fake(write={"out.txt": "x\n"}, commit=False)
    result = delegate(root, leaf_settings, **leaf_call("out.txt", task=task))

    assert result["status"] == "passed"
    assert git(repo, "show", f"{result['branch']}:out.txt") == "x"
    assert "uncommitted work from root.1" in git(repo, "log", "-1", "--format=%s", result["branch"])


def test_leaf_editing_outside_its_files_is_flagged(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    task = fake(write={"out.txt": "x\n", "other.txt": "y\n"})
    result = delegate(root, leaf_settings, **leaf_call("out.txt", task=task))

    assert result["status"] == "passed"
    assert "edited files outside its list: other.txt" in result["summary"]


def test_agent_that_crashes_is_a_failed_node(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)

    result = delegate(root, settings, task=fake(exit=3), kind="other", budget_usd=1.0)

    assert result["status"] == "failed"
    assert log.read()[-1]["status"] == "failed"


def test_depth_cap_decides_the_childs_tier(
    repo: Path, make_settings: Callable[..., Config]
) -> None:
    two_levels = make_settings(max_depth=2)
    root = engine.start_run(repo, two_levels)
    as_heavy = delegate(root, two_levels, task=fake(), kind="other", budget_usd=1.0)

    one_level = make_settings(max_depth=1)
    as_leaf = delegate(root, one_level, **leaf_call("out.txt"))

    db = paths.run_db(root.run_id)
    heavy_node = tree.get_node(db, as_heavy["node_id"])
    leaf_node = tree.get_node(db, as_leaf["node_id"])
    assert heavy_node is not None and heavy_node.tier == "heavy"
    assert leaf_node is not None and leaf_node.tier == "leaf"


def test_a_leaf_cannot_delegate(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    leaf = engine.Caller(root.run_id, "root.1", depth=1, worktree=root.worktree)

    result = delegate(leaf, leaf_settings, **leaf_call("out.txt"))

    assert result["status"] == "refused"
    assert "a leaf can't delegate" in result["summary"]


def test_parallel_siblings_overlap_and_get_their_own_worktrees(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)

    slow_a = fake(write={"a.txt": "a\n"}, sleep=0.3)
    slow_b = fake(write={"b.txt": "b\n"}, sleep=0.3)
    results = delegate_all(
        root,
        leaf_settings,
        [leaf_call("a.txt", task=slow_a), leaf_call("b.txt", task=slow_b)],
    )

    assert [result["status"] for result in results] == ["passed", "passed"]
    db = paths.run_db(root.run_id)
    first = tree.get_node(db, "root.1")
    second = tree.get_node(db, "root.2")
    assert first is not None and second is not None
    assert first.worktree != second.worktree
    assert first.finished_at is not None and second.finished_at is not None
    # Each started before the other finished: they really ran at the same time.
    assert first.started_at < second.finished_at
    assert second.started_at < first.finished_at


def test_log_line_has_every_field(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    delegate(root, leaf_settings, **leaf_call("out.txt", contract="keep the name out.txt"))

    entry = log.read()[-1]
    assert entry["event"] == "node"
    assert entry["run_id"] == root.run_id
    assert entry["node_id"] == "root.1"
    assert entry["parent_id"] == "root"
    assert entry["depth"] == 1
    assert entry["tier"] == "leaf"
    assert entry["model"] == "fake-leaf"
    assert entry["kind"] == "feature"
    assert entry["file_count"] == 1
    assert entry["task_words"] > 4
    assert entry["attempts"] == 1
    assert entry["status"] == "passed"
    assert entry["cost_usd"] == 0.01
    assert entry["duration_s"] >= 0
    assert entry["test_exit_code"] == 0
    assert entry["decisions_made"] == 0


def test_test_command_that_hangs_is_stopped_with_everything_it_started(
    tmp_path: Path,
) -> None:
    # The command starts a second process in the background, notes its id, then hangs.
    test_cmd = "sleep 30 & echo $! > background.pid; sleep 30"

    started = time.monotonic()
    exit_code, output = engine.run_test(test_cmd, tmp_path, timeout_s=1)

    assert exit_code == engine.TEST_TIMEOUT_EXIT_CODE
    assert "timed out after 1s" in output
    assert time.monotonic() - started < 10
    background = int((tmp_path / "background.pid").read_text())
    assert process_ends(background)


def process_ends(pid: int) -> bool:
    """True once the process is gone. Gives it a moment, since a kill isn't instant."""
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_splitter_prompt_says_what_its_children_are(settings: Config) -> None:
    def prompt_for(depth: int) -> str:
        return engine.node_prompt(
            is_leaf=False,
            task="Build the orders module",
            contract="",
            files=[],
            test_cmd=None,
            budget_usd=2.0,
            children_are_leaves=engine.children_are_leaves(settings, depth),
            failed_test_output="",
        )

    assert "delegate to are splitters" in prompt_for(depth=0)
    assert "delegate to are leaves" in prompt_for(depth=1)
    assert "needs `files` and a `test_cmd`" in prompt_for(depth=1)


def test_splitter_prompt_gives_the_leaf_limits(settings: Config) -> None:
    prompt = engine.node_prompt(
        is_leaf=False,
        task="Build the orders module",
        contract="",
        files=[],
        test_cmd=None,
        budget_usd=2.0,
        children_are_leaves=True,
        failed_test_output="",
        limits=settings.limits,
    )

    limits = settings.limits
    assert f"at most {limits.leaf_max_files} files" in prompt
    assert f"at most {limits.leaf_max_task_words} words" in prompt


def test_delegate_many_runs_children_together_and_answers_in_order(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)
    calls = [
        leaf_call("a.txt", task=fake(write={"a.txt": "a\n"}, sleep=0.3)),
        leaf_call("b.txt", task=fake(write={"b.txt": "b\n"}, sleep=0.3)),
        leaf_call("c.txt", files=[]),
    ]

    results = asyncio.run(engine.delegate_many(root, leaf_settings, calls))

    assert [r["status"] for r in results] == ["passed", "passed", "refused"]
    assert "a.txt" in results[0]["diff_stat"] and "b.txt" in results[1]["diff_stat"]
    db = paths.run_db(root.run_id)
    first, second = (
        tree.get_node(db, results[0]["node_id"]),
        tree.get_node(db, results[1]["node_id"]),
    )
    assert first is not None and second is not None
    assert first.finished_at is not None and second.finished_at is not None
    assert first.started_at < second.finished_at
    assert second.started_at < first.finished_at


QUESTION = """Wrote the parser.

## Decision needed

Should an empty order be an error or an empty list?
Options: A) raise ValueError B) return []
I'd pick: A, because the contract says orders are never empty.
"""


def test_leaf_that_asks_for_a_decision_is_not_retried(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    # The test fails, which would normally mean a retry. A question stops that.
    task = fake(write={"out.txt": "half\n"}, result=QUESTION)
    result = delegate(
        root, leaf_settings, **leaf_call("out.txt", task=task, test_cmd="grep done out.txt")
    )

    assert result["status"] == "needs_decision"
    assert result["attempts"] == 1
    assert result["question"].startswith("Should an empty order be an error")
    assert result["question"].endswith("orders are never empty.")
    assert result["summary"] == QUESTION
    assert log.read()[-1]["status"] == "needs_decision"
    node = tree.get_node(paths.run_db(root.run_id), result["node_id"])
    assert node is not None
    assert node.status == tree.NEEDS_DECISION


def test_splitter_can_pass_a_question_up(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)

    result = delegate(root, settings, task=fake(result=QUESTION), kind="feature", budget_usd=1.0)

    assert result["status"] == "needs_decision"
    assert "empty order" in result["question"]


def test_node_that_does_not_ask_has_no_question(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = delegate(root, leaf_settings, **leaf_call("out.txt"))

    assert result["status"] == "passed"
    assert result["question"] == ""


def test_decision_request_reads_the_section_up_to_the_next_heading() -> None:
    summary = "Did half.\n\n## decision NEEDED\n\nWhich way?\n\n## Notes\n\nSomething else."

    assert engine.decision_request(summary) == "Which way?"
    assert engine.decision_request("All done, nothing to ask.") is None
    assert "gave no question" in (engine.decision_request("## Decision needed\n") or "")


def test_child_splitter_prompt_says_to_ask_its_parent(settings: Config) -> None:
    arguments = {
        "is_leaf": False,
        "task": "Build the orders module",
        "contract": "",
        "files": [],
        "test_cmd": None,
        "budget_usd": 5.0,
        "children_are_leaves": True,
        "failed_test_output": "",
    }

    child = engine.node_prompt(**arguments)
    root = engine.node_prompt(**arguments, has_parent=False)

    assert "ask your parent" in child
    assert engine.DECISION_HEADING in child
    assert engine.DECISIONS_MADE_HEADING in child
    assert "Once 3 of your" not in child  # no limits given
    with_limits = engine.node_prompt(**arguments, limits=settings.limits)
    assert "Once 3 of your `delegate`, `delegate_many` or `answer` calls" in with_limits
    assert "ask your parent" not in root
    assert "Decide open questions yourself" in root


DECIDED = """Wrote the parser.

## Decisions made

- An empty order raises ValueError, not an empty list. (orders.py)
- Quantities are rounded down, not up. (orders.py)
"""


def test_splitter_low_risk_decisions_come_back_with_a_passed_result(
    repo: Path, settings: Config
) -> None:
    root = engine.start_run(repo, settings)

    result = delegate(root, settings, task=fake(result=DECIDED), kind="feature", budget_usd=1.0)

    assert result["status"] == "passed"
    assert result["question"] == ""
    assert result["decisions"].startswith("- An empty order raises ValueError")
    assert result["decisions"].endswith("rounded down, not up. (orders.py)")
    assert log.read()[-1]["decisions_made"] == 2


def test_leaf_that_decides_for_itself_needs_its_parent_to_agree(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)

    # The test passes, but a leaf may not make decisions, so the work doesn't count yet.
    task = fake(write={"out.txt": "x\n"}, result=DECIDED)
    result = delegate(root, leaf_settings, **leaf_call("out.txt", task=task))

    assert result["status"] == "needs_decision"
    assert result["attempts"] == 1
    assert result["question"] == engine.LEAF_DECIDED_QUESTION
    assert "rounded down" in result["decisions"]
    assert log.read()[-1]["decisions_made"] == 2


def test_node_can_record_decisions_and_ask_in_one_message(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)

    message = DECIDED + "\n## Decision needed\n\nShould orders be stored as JSON or CSV?\n"
    task = fake(write={"out.txt": "x\n"}, result=message)
    result = delegate(root, leaf_settings, **leaf_call("out.txt", task=task))

    assert result["status"] == "needs_decision"
    assert result["question"] == "Should orders be stored as JSON or CSV?"
    assert "rounded down" in result["decisions"]
    assert "JSON" not in result["decisions"]


def _heavy_caller(root: engine.Caller, settings: Config) -> engine.Caller:
    """A heavy splitter under the root, to make calls as, the way its own server would."""
    heavy = delegate(root, settings, task=fake(), kind="feature", budget_usd=5.0)
    node = tree.get_node(paths.run_db(root.run_id), heavy["node_id"])
    assert node is not None
    return engine.Caller(root.run_id, node.node_id, node.depth, Path(node.worktree))


def _asking_leaf(path: str) -> dict[str, object]:
    return leaf_call(path, task=fake(write={path: "x\n"}, result="## Decision needed\n\nWhich?"))


def test_splitter_whose_children_keep_asking_must_ask_its_parent(
    repo: Path, settings: Config
) -> None:
    root = engine.start_run(repo, settings)
    splitter = _heavy_caller(root, settings)

    asked = [delegate(splitter, settings, **_asking_leaf(f"q{n}.txt")) for n in range(3)]
    fourth = delegate(splitter, settings, **leaf_call("done.txt"))

    assert [result["status"] for result in asked] == ["needs_decision"] * 3
    assert fourth["status"] == "refused"
    assert "3 of your calls have brought back questions" in fourth["summary"]
    assert "ask your parent" in fourth["summary"]


def test_children_asking_together_count_as_one_round(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)
    splitter = _heavy_caller(root, settings)

    # Four leaves at once all ask. One call, so one round, and the splitter can carry on.
    asked = asyncio.run(
        engine.delegate_many(splitter, settings, [_asking_leaf(f"q{n}.txt") for n in range(4)])
    )
    after = delegate(splitter, settings, **leaf_call("done.txt"))

    assert [result["status"] for result in asked] == ["needs_decision"] * 4
    assert after["status"] == "passed"
    node = tree.get_node(paths.run_db(root.run_id), splitter.node_id)
    assert node is not None
    assert node.question_rounds == 1


def test_the_planner_is_never_stopped_from_delegating_by_questions(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)

    results = [delegate(root, leaf_settings, **_asking_leaf(f"q{n}.txt")) for n in range(4)]

    assert [result["status"] for result in results] == ["needs_decision"] * 4
