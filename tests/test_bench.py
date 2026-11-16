"""`agents bench`: the tree and solo sessions on the same task, judged the same way."""

import json
from pathlib import Path

import pytest
from helpers import checkout_state, fake, git

from agents import bench, paths
from agents.config import Config

# Every arm gets this task. The fake agent writes one file, and each arm reports a cost.
TASK = fake(write={"out.txt": "done\n"}, cost=0.25)


def test_every_arm_runs_from_the_same_commit_and_is_judged(repo: Path, settings: Config) -> None:
    before = checkout_state(repo)

    results = bench.run(repo, settings, task=TASK, accept_cmd="grep done out.txt", runs=2)

    assert [(result["arm"], result["run"]) for result in results] == [
        ("tree", 1),
        ("solo:planner", 1),
        ("solo:heavy", 1),
        ("tree", 2),
        ("solo:planner", 2),
        ("solo:heavy", 2),
    ]
    assert all(result["accepted"] for result in results)
    assert all(result["cost_usd"] == 0.25 for result in results)
    assert all(result["files_changed"] == 1 and result["lines_added"] == 1 for result in results)
    # Each result is kept as one branch, cut from the commit the benchmark started at.
    for result in results:
        assert git(repo, "show", f"{result['review_branch']}:out.txt") == "done"
        assert git(repo, "merge-base", result["review_branch"], "main") == before[0]
    assert checkout_state(repo) == before


def test_only_the_review_branches_are_left_behind(repo: Path, settings: Config) -> None:
    results = bench.run(repo, settings, task=TASK, accept_cmd="true", runs=1)

    bench_id = results[0]["bench_id"]
    branches = git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert sorted(branches) == [
        f"bench/{bench_id}/1",
        f"bench/{bench_id}/2",
        f"bench/{bench_id}/3",
        "main",
    ]
    assert len(git(repo, "worktree", "list").splitlines()) == 1
    assert list(paths.state_dir().rglob("out.txt")) == []


def test_acceptance_files_judge_the_work_without_becoming_part_of_it(
    repo: Path, settings: Config, tmp_path: Path
) -> None:
    hidden = tmp_path / "hidden"
    (hidden / "checks").mkdir(parents=True)
    (hidden / "checks" / "expected.txt").write_text("done\n")

    results = bench.run(
        repo,
        settings,
        task=TASK,
        accept_cmd="diff out.txt checks/expected.txt",
        accept_dir=hidden,
        arms=["solo:heavy"],
        runs=1,
    )

    assert results[0]["accepted"]
    files = git(repo, "ls-tree", "-r", "--name-only", results[0]["review_branch"]).splitlines()
    assert files == ["README.md", "out.txt"]


def test_work_that_fails_acceptance_is_recorded_as_such(repo: Path, settings: Config) -> None:
    results = bench.run(
        repo, settings, task=TASK, accept_cmd="grep nope out.txt", arms=["tree"], runs=1
    )

    assert not results[0]["accepted"]
    assert results[0]["accept_exit_code"] == 1
    saved = [json.loads(line) for line in bench.results_path(results[0]["bench_id"]).open()]
    assert saved == results


def test_solo_arm_uses_that_tiers_model(settings: Config) -> None:
    assert bench.default_arms(settings) == ["tree", "solo:planner", "solo:heavy"]
    assert bench.solo_agent(settings, "solo:heavy").model == "fake-heavy"
    assert bench.solo_agent(settings, "solo:leaf").model == "fake-leaf"
    with pytest.raises(KeyError, match="no tier 'nope'"):
        bench.check_arms(settings, ["tree", "solo:nope"])
    with pytest.raises(KeyError, match="unknown arm 'both'"):
        bench.check_arms(settings, ["both"])


def test_summary_averages_per_arm_and_hides_which_arm_wrote_which() -> None:
    def result(arm: str, branch: int, accepted: bool, cost: float) -> dict[str, object]:
        return {
            "bench_id": "b1",
            "arm": arm,
            "review_branch": f"bench/b1/{branch}",
            "accepted": accepted,
            "cost_usd": cost,
            "seconds": 60.0,
            "files_changed": 2,
            "lines_added": 40,
            "lines_deleted": 4,
        }

    text = bench.summary(
        [
            result("tree", 10, True, 1.0),
            result("solo:heavy", 2, True, 0.5),
            result("tree", 1, False, 2.0),
        ]
    )

    rows = {line.split()[0]: " ".join(line.split()) for line in text.splitlines() if line.strip()}
    assert rows["tree"] == "tree 2 1/2 1.50 60 2.0 40 4"
    assert rows["solo:heavy"] == "solo:heavy 1 1/1 0.50 60 2.0 40 4"
    listed = [line.strip() for line in text.splitlines() if line.startswith("  bench/")]
    assert listed == ["bench/b1/1", "bench/b1/2", "bench/b1/10"]
