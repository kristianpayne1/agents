"""The whole tree: planner -> heavy -> leaves, with each level in its own process."""

from pathlib import Path

from helpers import checkout_state, delegate, fake, git, leaf_call

from agents import cli, engine, log, paths, tree
from agents.config import Config

BOTH_FILES = "test -f a.txt && test -f b.txt"


def heavy_task() -> str:
    """A fake heavy splitter: hand two files to two leaves, merge them, run the tests."""
    return fake(
        delegate=[leaf_call("a.txt", kind="feature"), leaf_call("b.txt", kind="tests")],
        integrate=BOTH_FILES,
        cost=0.5,
    )


def test_feature_goes_down_the_tree_and_back_up(repo: Path, settings: Config) -> None:
    before = checkout_state(repo)
    root = engine.start_run(repo, settings)
    db = paths.run_db(root.run_id)

    heavy = delegate(
        root, settings, task=heavy_task(), test_cmd=BOTH_FILES, kind="feature", budget_usd=5.0
    )
    merged = engine.integrate(root, settings, node_ids=[heavy["node_id"]], test_cmd=BOTH_FILES)

    assert heavy["status"] == "passed"
    assert merged["status"] == "passed"
    # One root branch holds the result, and its tests pass.
    assert (root.worktree / "a.txt").exists()
    assert (root.worktree / "b.txt").exists()
    assert git(repo, "show", f"agents/{root.run_id}/root:a.txt") == "x"

    nodes = {node.node_id: node for node in tree.all_nodes(db)}
    assert sorted(nodes) == ["root", "root.1", "root.1.1", "root.1.2"]
    assert nodes["root.1"].tier == "heavy"
    assert nodes["root.1.1"].tier == "leaf"
    assert nodes["root.1.1"].parent_id == "root.1"
    assert all(nodes[node_id].merge_state == tree.MERGED for node_id in nodes if node_id != "root")
    # Each leaf branched from the heavy node, which branched from the root.
    assert nodes["root.1.1"].base_commit == nodes["root.1"].base_commit

    # The user's checkout never changed, and nothing was pushed (there is no remote).
    assert checkout_state(repo) == before
    assert git(repo, "remote") == ""


def test_budget_flows_back_up_through_the_levels(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)
    db = paths.run_db(root.run_id)

    delegate(root, settings, task=heavy_task(), test_cmd=BOTH_FILES, kind="feature", budget_usd=5.0)

    # Heavy spent 0.50 itself and its two leaves 0.01 each; the rest of its 5.00 came back.
    heavy = tree.get_node(db, "root.1")
    assert heavy is not None
    assert round(heavy.held_usd, 2) == 0.52
    assert round(tree.remaining_usd(db, "root"), 2) == 9.48
    assert round(engine.status(root.run_id)["run_spent_usd"], 2) == 0.52


def test_every_node_is_logged_once(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)

    delegate(root, settings, task=heavy_task(), test_cmd=BOTH_FILES, kind="feature", budget_usd=5.0)

    nodes = [entry for entry in log.read() if entry["event"] == "node"]
    assert sorted(entry["node_id"] for entry in nodes) == ["root.1", "root.1.1", "root.1.2"]
    assert {entry["tier"] for entry in nodes} == {"heavy", "leaf"}
    report = log.report(log.read())
    assert "heavy" in report and "leaf" in report
    assert "feature" in report and "tests" in report


def test_status_shows_the_tree(repo: Path, settings: Config) -> None:
    root = engine.start_run(repo, settings)
    delegate(root, settings, task=heavy_task(), test_cmd=BOTH_FILES, kind="feature", budget_usd=5.0)

    status = engine.status(root.run_id)
    text = cli.format_status(status)

    assert status["you"]["branch"] == f"agents/{root.run_id}/root"
    assert status["you"]["budget_left_usd"] == 9.48
    lines = text.splitlines()
    assert lines[1] == "Budget $10.00, spent $0.52, left $9.48"
    tree_lines = lines[3:]
    assert [line.split()[0] for line in tree_lines] == ["root", "root.1", "root.1.1", "root.1.2"]
    assert tree_lines[2].startswith("    root.1.1")
    assert "passed" in tree_lines[2] and "merged" in tree_lines[2]


def test_cleanup_removes_worktrees_and_keeps_branches(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    delegate(root, leaf_settings, **leaf_call("a.txt"))
    run_dir = paths.run_worktrees("repo", root.run_id)
    assert (run_dir / "root.1").exists()

    removed = engine.cleanup(root.run_id, delete_branches=False)

    assert len(removed) == 2
    assert not run_dir.exists()
    assert "agents/" not in git(repo, "worktree", "list")
    branches = git(repo, "branch", "--list", "agents/*", "--format=%(refname:short)").splitlines()
    assert branches == [f"agents/{root.run_id}/root", f"agents/{root.run_id}/root.1"]


def test_cleanup_can_delete_the_branches_too(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    delegate(root, leaf_settings, **leaf_call("a.txt"))

    engine.cleanup(root.run_id, delete_branches=True)

    assert git(repo, "branch", "--list", "agents/*") == ""
    assert git(repo, "branch", "--show-current") == "main"


def test_two_runs_in_the_same_minute_get_different_ids(repo: Path, settings: Config) -> None:
    first = engine.start_run(repo, settings)
    second = engine.start_run(repo, settings)

    assert first.run_id != second.run_id
    assert engine.latest_run_id() == second.run_id
