"""`agents decisions`: one list of every decision, question and answer in a run."""

from pathlib import Path

from helpers import answer, delegate, fake, git, leaf_call

from agents import cli, engine
from agents.config import Config

DECIDED = "Done.\n\n## Decisions made\n\n- Tabs, not spaces. (a.txt)\n"
ASKS = "Half done.\n\n## Decision needed\n\nKeep the header row?\n"


def test_lists_decisions_answers_and_how_to_undo_them(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    # A leaf that decided something itself (so it comes back asking its parent to agree),
    # one that asked and was answered, and one that did neither.
    decided = delegate(
        root, leaf_settings, **leaf_call("a.txt", task=fake(write={"a.txt": "x\n"}, result=DECIDED))
    )
    asked = delegate(
        root,
        leaf_settings,
        **leaf_call(
            "b.txt",
            task=fake(write={"b.txt": "half\n"}, result=ASKS),
            test_cmd="grep done b.txt",
        ),
    )
    reply = "Yes, keep it.\n" + fake(write={"b.txt": "done\n"})
    answer(root, leaf_settings, node_id=asked["node_id"], answer=reply, budget_usd=1.0)
    delegate(root, leaf_settings, **leaf_call("c.txt"))
    engine.integrate(
        root, leaf_settings, node_ids=[decided["node_id"], asked["node_id"]], test_cmd="true"
    )

    found = engine.decisions(root.run_id)

    by_node = {node["node_id"]: node for node in found["nodes"]}
    assert sorted(by_node) == ["root.1", "root.2"]
    assert by_node["root.1"]["decisions"] == "- Tabs, not spaces. (a.txt)"
    assert by_node["root.2"]["answers"].startswith("Q: Keep the header row?\nA: Yes, keep it.")
    assert by_node["root.2"]["status"] == "passed"
    commit = by_node["root.1"]["merge_commit"]
    assert commit
    assert git(root.worktree, "log", "-1", "--format=%s", commit) == "Merge root.1 (feature)"

    text = cli.format_decisions(found)
    assert "root.1  leaf, needs_decision" in text
    assert "  decided:  - Tabs, not spaces. (a.txt)" in text
    assert "  answered: Q: Keep the header row?" in text
    assert f"git revert -m 1 {commit}" in text

    # The command it prints really does undo that node's work.
    git(root.worktree, "revert", "--no-edit", "-m", "1", commit)
    assert not (root.worktree / "a.txt").exists()
    assert (root.worktree / "b.txt").exists()


def test_says_so_when_nothing_was_decided(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    delegate(root, leaf_settings, **leaf_call("a.txt"))

    text = cli.format_decisions(engine.decisions(root.run_id))

    assert "No node recorded a decision or asked a question." in text


def test_unmerged_node_says_it_is_not_merged(repo: Path, leaf_settings: Config) -> None:
    root = engine.start_run(repo, leaf_settings)
    task = fake(write={"a.txt": "x\n"}, result=ASKS)
    delegate(root, leaf_settings, **leaf_call("a.txt", task=task))

    text = cli.format_decisions(engine.decisions(root.run_id))

    assert "  asked:    Keep the header row?" in text
    assert "  not merged into the root branch" in text
