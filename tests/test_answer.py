"""`answer`: resuming a child that asked a question, in its own session and worktree."""

from pathlib import Path

import pytest
from helpers import answer, delegate, fake, git, leaf_call

from agents import engine, log, paths, tree
from agents.config import Config
from agents.harness import base as harness_base
from agents.harness.base import Harness, HarnessResult, NodeRequest

QUESTION = "Half done.\n\n## Decision needed\n\nShould the slug keep underscores?\n"


def asking_leaf(path: str = "out.txt", cost: float = 0.1) -> dict[str, object]:
    """A leaf that writes half its file, then asks. Its test needs the word `done`."""
    task = fake(write={path: "half\n"}, result=QUESTION, cost=cost)
    return leaf_call(path, task=task, test_cmd=f"grep done {path}")


def test_answered_leaf_carries_on_in_its_own_worktree(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    asked = delegate(root, leaf_settings, **asking_leaf())
    assert asked["status"] == "needs_decision"
    db = paths.run_db(root.run_id)
    before = tree.get_node(db, asked["node_id"])
    assert before is not None

    reply = "Drop them.\n" + fake(write={"out.txt": "done\n"}, result="Finished.", cost=0.2)
    result = answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=1.0)

    assert result["status"] == "passed"
    assert result["node_id"] == asked["node_id"]
    assert result["branch"] == asked["branch"]
    assert result["attempts"] == 2
    assert round(result["cost_usd"], 2) == 0.3
    assert result["question"] == ""
    after = tree.get_node(db, asked["node_id"])
    assert after is not None
    # The same session was carried on, and the answer is kept with the node.
    assert after.session_id == before.session_id != ""
    assert after.answers == f"Q: Should the slug keep underscores?\nA: {reply}"
    assert git(repo, "show", f"{result['branch']}:out.txt") == "done"


def test_answer_budget_is_reserved_then_refunded(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    db = paths.run_db(root.run_id)
    asked = delegate(root, leaf_settings, **asking_leaf(cost=0.1))
    assert round(tree.remaining_usd(db, "root"), 2) == 9.9

    reply = fake(write={"out.txt": "done\n"}, cost=0.2)
    answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=2.0)

    assert round(tree.remaining_usd(db, "root"), 2) == 9.7


def test_each_run_of_an_answered_node_is_logged_on_its_own(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)
    asked = delegate(root, leaf_settings, **asking_leaf(cost=0.1))

    reply = fake(write={"out.txt": "done\n"}, cost=0.2)
    answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=1.0)

    first, second = [entry for entry in log.read() if entry["event"] == log.NODE]
    assert (first["status"], first["answered"], first["cost_usd"]) == ("needs_decision", False, 0.1)
    assert (second["status"], second["answered"], second["cost_usd"]) == ("passed", True, 0.2)
    assert second["attempts"] == 1


def test_answered_node_can_ask_again(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    asked = delegate(root, leaf_settings, **asking_leaf())

    reply = fake(result="## Decision needed\n\nAnd hyphens?")
    result = answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=1.0)

    assert result["status"] == "needs_decision"
    assert result["question"] == "And hyphens?"


def test_resumed_agent_is_given_only_the_answer(
    repo: Path, leaf_settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = engine.start_run(repo, leaf_settings)
    asked = delegate(root, leaf_settings, **asking_leaf())

    requests: list[NodeRequest] = []
    real_run = harness_base.run

    async def run_and_keep(harness: Harness, request: NodeRequest, timeout_s: int) -> HarnessResult:
        requests.append(request)
        return await real_run(harness, request, timeout_s)

    monkeypatch.setattr(harness_base, "run", run_and_keep)
    reply = "Keep them.\n" + fake(write={"out.txt": "done\n"})
    answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=1.0)

    (request,) = requests
    assert request.resume_session_id
    assert request.prompt.startswith("## Answer from your parent\n\nKeep them.")
    assert "## Task" not in request.prompt


def test_a_retry_after_an_answer_starts_fresh_with_the_answers(
    repo: Path, leaf_settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = engine.start_run(repo, leaf_settings)
    asked = delegate(root, leaf_settings, **asking_leaf())

    requests: list[NodeRequest] = []
    real_run = harness_base.run

    async def run_and_keep(harness: Harness, request: NodeRequest, timeout_s: int) -> HarnessResult:
        requests.append(request)
        return await real_run(harness, request, timeout_s)

    monkeypatch.setattr(harness_base, "run", run_and_keep)
    # The resumed run still fails its test, so the leaf is retried in a new session.
    reply = "Keep them.\n" + fake(write={"out.txt": "still half\n"})
    result = answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=1.0)

    # The retry is a fresh session with the whole task again (whose fake instructions ask
    # again), plus the answers given so far.
    assert result["attempts"] == 3
    resumed, retry = requests
    assert resumed.resume_session_id
    assert retry.resume_session_id is None
    assert "## Answers from your parent" in retry.prompt
    assert "Keep them." in retry.prompt


def test_answer_is_refused_for_a_node_that_is_not_your_child(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = answer(root, leaf_settings, node_id="root.9", answer="yes", budget_usd=1.0)

    assert result["status"] == "refused"
    assert "not one of your children" in result["summary"]


def test_answer_is_refused_for_a_node_that_did_not_ask(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    passed = delegate(root, leaf_settings, **leaf_call("out.txt"))

    result = answer(root, leaf_settings, node_id=passed["node_id"], answer="yes", budget_usd=1.0)

    assert result["status"] == "refused"
    assert "not needs_decision" in result["summary"]


def test_answer_is_refused_when_empty_or_over_budget(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    asked = delegate(root, leaf_settings, **asking_leaf())
    node_id = asked["node_id"]

    empty = answer(root, leaf_settings, node_id=node_id, answer="  ", budget_usd=1.0)
    too_much = answer(root, leaf_settings, node_id=node_id, answer="yes", budget_usd=50.0)

    assert "`answer` is empty" in empty["summary"]
    assert "only $" in too_much["summary"]
    # Still waiting for an answer, so it can be answered properly afterwards.
    node = tree.get_node(paths.run_db(root.run_id), node_id)
    assert node is not None
    assert node.status == tree.NEEDS_DECISION


def test_answers_that_bring_back_questions_count_towards_the_limit(
    repo: Path, settings: Config
) -> None:
    root = engine.start_run(repo, settings)
    heavy = delegate(root, settings, task=fake(), kind="feature", budget_usd=5.0)
    node = tree.get_node(paths.run_db(root.run_id), heavy["node_id"])
    assert node is not None
    splitter = engine.Caller(root.run_id, node.node_id, node.depth, Path(node.worktree))

    asked = delegate(splitter, settings, **asking_leaf())
    again = fake(result="## Decision needed\n\nStill unsure.")
    second = answer(splitter, settings, node_id=asked["node_id"], answer=again, budget_usd=0.5)
    third = answer(splitter, settings, node_id=asked["node_id"], answer=again, budget_usd=0.5)
    fourth = answer(splitter, settings, node_id=asked["node_id"], answer=again, budget_usd=0.5)

    assert [second["status"], third["status"]] == ["needs_decision", "needs_decision"]
    assert fourth["status"] == "refused"
    assert "can't answer any more" in fourth["summary"]
