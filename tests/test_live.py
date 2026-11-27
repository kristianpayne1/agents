"""One end-to-end run with real models on a toy repo.

It costs money, so it never runs by default:

    uv run pytest -m live -s

It uses whatever roles.toml is in effect, so $AGENTS_CONFIG can point it at cheaper models.
To run only the orders example: `uv run pytest -m live -s -k orders`.
The real planner is interactive; here the planner tier is run headless, like any splitter.
"""

import asyncio
import shutil
from pathlib import Path

import pytest
from helpers import checkout_state, git

from agents import cli, config, engine, example, log, paths, tree

TEST_CMD = "python3 -m unittest"

TASK = """\
Add a `slugify(text: str) -> str` function to textutils.py. It lower-cases the text, turns
each run of characters that aren't letters or digits into a single hyphen, and strips hyphens
from both ends. Cover it with unit tests in test_textutils.py. The tests run with
`python3 -m unittest`.

Use the delegation tree for this, because the point of this run is to exercise it. Delegate
the feature to one child. That child should hand the tests and the implementation to leaves
as two separate tasks, then merge them. When it comes back, merge its branch into yours with
`integrate` and check that the tests pass.
"""


@pytest.mark.live
def test_real_models_take_a_feature_down_the_tree_and_back(tmp_path: Path) -> None:
    repo = tmp_path / "toy"
    repo.mkdir()
    git(repo, "init", "--initial-branch", "main")
    (repo / "textutils.py").write_text('"""Small text helpers."""\n')
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "add", "--all")
    git(repo, "commit", "--message", "first commit")
    before = checkout_state(repo)

    settings = config.load()
    root = engine.start_run(repo, settings)

    result = asyncio.run(engine.run_root_headless(root, settings, task=TASK, test_cmd=TEST_CMD))

    print()
    print(cli.format_status(engine.status(root.run_id)))
    print(f"\nPlanner's own cost: ${result.cost_usd}\n\n{result.summary}\n")
    for entry in log.read():
        if entry["event"] == log.NODE:
            denials = entry["permission_denials"]
            print(f"{entry['node_id']}: {entry['status']}, {denials} permission denials")

    assert result.ok, result.summary
    # One root branch holds the feature, and its tests pass.
    assert "def slugify" in (root.worktree / "textutils.py").read_text()
    exit_code, output = engine.run_test(TEST_CMD, root.worktree, settings.limits.test_timeout_s)
    assert exit_code == 0, output
    assert git(root.worktree, "status", "--porcelain", "--untracked-files=no") == ""

    # The work went through a splitter to leaves, not straight from the planner.
    nodes = tree.all_nodes(paths.run_db(root.run_id))
    leaf_tier = settings.leaf_tier.name
    splitters = [node for node in nodes if node.depth == 1 and node.tier != leaf_tier]
    leaves = [node for node in nodes if node.tier == leaf_tier]
    assert any(node.status == tree.PASSED for node in splitters)
    # A leaf that wrote tests before the code existed may rightly be `failed`, so count the
    # leaves whose work was merged, and ask for at least one that passed.
    assert sum(1 for node in leaves if node.merge_state == tree.MERGED) >= 2
    assert any(node.status == tree.PASSED for node in leaves)

    assert checkout_state(repo) == before


ORDERS_TASK_NOTE = """

Use the delegation tree for this: hand the work to children rather than doing it yourself.
Nobody is watching this run, so settle open questions yourself as your instructions say, and
end with every decision made in the tree.
"""


@pytest.mark.live
def test_real_models_record_decisions_on_the_orders_example(tmp_path: Path) -> None:
    """The orders task leaves choices open. A real run should finish it, and the choices
    should show up as recorded decisions or questions, not be made silently."""
    repo = example.make_repo(tmp_path / "orders")
    before = checkout_state(repo)
    settings = config.load()
    root = engine.start_run(repo, settings)
    task = (repo / "TASK.md").read_text() + ORDERS_TASK_NOTE

    result = asyncio.run(
        engine.run_root_headless(root, settings, task=task, test_cmd=example.TEST_CMD)
    )

    print()
    print(cli.format_status(engine.status(root.run_id)))
    print()
    print(cli.format_decisions(engine.decisions(root.run_id)))
    print(f"\nPlanner's own cost: ${result.cost_usd}\n\n{result.summary}\n")

    assert result.ok, result.summary
    shutil.copytree(example.example_dir() / "accept", root.worktree, dirs_exist_ok=True)
    exit_code, output = engine.run_test(example.ACCEPT_CMD, root.worktree, 600)
    assert exit_code == 0, output
    assert engine.decisions(root.run_id)["nodes"], "no node recorded a decision or a question"
    assert checkout_state(repo) == before
