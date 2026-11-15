"""`integrate`: clean merges, conflicts handed back, and what it refuses to merge."""

from pathlib import Path

from helpers import delegate, delegate_all, fake, git, leaf_call

from agents import engine, log, paths, tree
from agents.config import Config


def test_clean_merge_then_tests(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    children = delegate_all(root, leaf_settings, [leaf_call("a.txt"), leaf_call("b.txt")])
    node_ids = [child["node_id"] for child in children]

    result = engine.integrate(
        root, leaf_settings, node_ids=node_ids, test_cmd="test -f a.txt && test -f b.txt"
    )

    assert result["status"] == "passed"
    assert result["merged"] == node_ids
    assert result["test_exit_code"] == 0
    # --no-ff: one merge commit per child, so the tree's shape shows in the history.
    merges = git(root.worktree, "log", "--merges", "--format=%s").splitlines()
    assert sorted(merges) == ["Merge root.1 (feature)", "Merge root.2 (feature)"]
    db = paths.run_db(root.run_id)
    for node_id in node_ids:
        node = tree.get_node(db, node_id)
        assert node is not None and node.merge_state == tree.MERGED


def test_conflict_is_returned_to_the_caller(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    # Two siblings write the same file differently, on purpose.
    children = delegate_all(
        root,
        leaf_settings,
        [leaf_call("same.txt", content="from one\n"), leaf_call("same.txt", content="from two\n")],
    )
    assert [child["status"] for child in children] == ["passed", "passed"]

    result = engine.integrate(
        root, leaf_settings, node_ids=["root.1", "root.2"], test_cmd="test -f same.txt"
    )

    assert result["status"] == "conflict"
    assert result["merged"] == ["root.1"]
    assert result["conflict"]["node_id"] == "root.2"
    assert result["conflict"]["files"] == ["same.txt"]
    assert "from two" in result["conflict"]["details"]
    assert result["not_merged"] == ["root.2"]
    # The failed merge was aborted: the first child's work is in, nothing is half-merged.
    assert (root.worktree / "same.txt").read_text() == "from one\n"
    assert git(root.worktree, "status", "--porcelain") == ""

    statuses = {entry["node_id"]: entry["merge"] for entry in engine.status(root.run_id)["nodes"]}
    assert statuses["root.1"] == "merged"
    assert statuses["root.2"] == "conflict"
    entry = log.read()[-1]
    assert entry["event"] == "integrate"
    assert entry["status"] == "conflict"
    assert entry["conflict_node_id"] == "root.2"


def test_conflicting_task_can_be_delegated_again(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    delegate_all(
        root,
        leaf_settings,
        [leaf_call("same.txt", content="from one\n"), leaf_call("same.txt", content="from two\n")],
    )
    engine.integrate(root, leaf_settings, node_ids=["root.1", "root.2"], test_cmd="true")

    # The retry branches from the updated parent, so it merges cleanly.
    retry = delegate(root, leaf_settings, **leaf_call("same.txt", content="from two\n"))
    result = engine.integrate(
        root, leaf_settings, node_ids=[retry["node_id"]], test_cmd="grep two same.txt"
    )

    assert result["status"] == "passed"


def test_failing_tests_after_merge_are_reported(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    child = delegate(root, leaf_settings, **leaf_call("a.txt"))

    result = engine.integrate(
        root, leaf_settings, node_ids=[child["node_id"]], test_cmd="echo nope && exit 2"
    )

    assert result["status"] == "tests_failed"
    assert result["merged"] == ["root.1"]
    assert result["test_exit_code"] == 2
    assert result["test_output"] == "nope"


def test_empty_list_only_runs_the_tests(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)

    result = engine.integrate(root, leaf_settings, node_ids=[], test_cmd="test -f README.md")

    assert result["status"] == "passed"
    assert result["merged"] == []


def test_only_your_own_children_can_be_merged(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    delegate(root, leaf_settings, **leaf_call("a.txt"))
    someone_else = engine.Caller(root.run_id, "root.9", depth=0, worktree=root.worktree)

    result = engine.integrate(someone_else, leaf_settings, node_ids=["root.1"], test_cmd="true")

    assert result["status"] == "error"
    assert "not one of your children" in result["message"]


def test_refused_child_has_nothing_to_merge(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    refused = delegate(root, leaf_settings, **leaf_call("a.txt", files=[]))

    result = engine.integrate(root, leaf_settings, node_ids=[refused["node_id"]], test_cmd="true")

    assert result["status"] == "error"
    assert "is refused" in result["message"]


def test_uncommitted_changes_block_a_merge(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    child = delegate(root, leaf_settings, **leaf_call("a.txt"))
    (root.worktree / "README.md").write_text("edited but not committed\n")

    result = engine.integrate(root, leaf_settings, node_ids=[child["node_id"]], test_cmd="true")

    assert result["status"] == "error"
    assert "uncommitted changes" in result["message"]


def test_failed_child_can_still_be_merged_if_the_parent_decides_to(
    repo: Path, leaf_settings: Config
) -> None:
    root = engine.start_run(repo, leaf_settings)
    task = fake(write={"out.txt": "bad\n"})
    failed = delegate(
        root, leaf_settings, **leaf_call("out.txt", task=task, test_cmd="grep good out.txt")
    )
    assert failed["status"] == "failed"

    result = engine.integrate(
        root, leaf_settings, node_ids=[failed["node_id"]], test_cmd="test -f out.txt"
    )

    assert result["status"] == "passed"
