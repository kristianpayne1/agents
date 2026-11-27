from collections.abc import Callable
from pathlib import Path

import pytest

from agents import checks, config, router
from agents.config import Config


def test_default_roles_file_loads() -> None:
    settings = config.load(config.REPO_CONFIG)

    assert [tier.name for tier in settings.tiers] == ["planner", "heavy", "leaf"]
    assert settings.leaf_tier.pool
    assert settings.limits.max_depth == 2


def test_env_var_picks_the_config(tmp_path: Path, make_settings: Callable[..., Config]) -> None:
    make_settings(leaf_max_files=7)

    assert config.config_path() == tmp_path / "roles.toml"
    assert config.load().limits.leaf_max_files == 7


def test_tier_follows_depth(settings: Config) -> None:
    assert settings.tier_for_depth(0).name == "planner"
    assert settings.tier_for_depth(1).name == "heavy"
    assert settings.tier_for_depth(2).name == "leaf"


def test_depth_cap_forces_leaf_tier(make_settings: Callable[..., Config]) -> None:
    capped = make_settings(max_depth=1)

    assert capped.tier_for_depth(1).name == "leaf"


def test_depth_past_the_last_splitter_is_a_leaf(make_settings: Callable[..., Config]) -> None:
    deep = make_settings(max_depth=5)

    assert deep.tier_for_depth(2).name == "leaf"
    assert deep.tier_for_depth(4).name == "leaf"


def test_unknown_harness_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "roles.toml"
    path.write_text(
        """
        [[tier]]
        name = "planner"
        can_split = true
        harness = "nope"
        model = "m"

        [[tier]]
        name = "leaf"
        can_split = false
        pool = [{ name = "a", harness = "fake", model = "m" }]

        [limits]
        max_depth = 1
        run_budget_usd = 1.0
        leaf_max_files = 3
        leaf_max_task_words = 300
        leaf_max_attempts = 2
        """
    )

    with pytest.raises(config.ConfigError, match="unknown harness 'nope'"):
        config.load(path)


def test_leaf_tier_needs_a_pool(tmp_path: Path) -> None:
    path = tmp_path / "roles.toml"
    path.write_text(
        """
        [[tier]]
        name = "planner"
        can_split = true
        harness = "fake"
        model = "m"

        [[tier]]
        name = "leaf"
        can_split = false

        [limits]
        max_depth = 1
        run_budget_usd = 1.0
        leaf_max_files = 3
        leaf_max_task_words = 300
        leaf_max_attempts = 2
        """
    )

    with pytest.raises(config.ConfigError, match="needs a pool"):
        config.load(path)


def test_router_returns_the_first_pool_member(settings: Config) -> None:
    task = router.LeafTask(kind="tests", task="t", contract="", files=["a"], test_cmd="true")

    assert router.route(task, settings.leaf_tier.pool) == settings.leaf_tier.pool[0]


def test_leaf_task_checks(settings: Config) -> None:
    limits = settings.limits

    def check(
        task: str = "do it",
        contract: str = "",
        files: list[str] | None = None,
        test_cmd: str | None = "true",
    ) -> str:
        files = ["a.py"] if files is None else files
        refusal = checks.check_leaf_task(
            task=task, contract=contract, files=files, test_cmd=test_cmd, limits=limits
        )
        return refusal or ""

    assert check() == ""
    assert "`files` is missing" in check(files=[])
    assert "4 files is over the limit of 3" in check(files=["a", "b", "c", "d"])
    assert "`test_cmd` is missing" in check(test_cmd=None)
    assert "over the limit of 300" in check(task="word " * 200, contract="word " * 101)
    assert "Split this further" in check(files=[])


def test_max_question_rounds_must_be_at_least_one(make_settings: Callable[..., Config]) -> None:
    with pytest.raises(config.ConfigError, match="max_question_rounds must be at least 1"):
        make_settings(max_question_rounds=0)


def test_max_question_rounds_defaults_when_left_out(tmp_path: Path) -> None:
    roles = config.REPO_CONFIG.read_text().replace("max_question_rounds = 3", "")
    path = tmp_path / "older-roles.toml"
    path.write_text(roles)

    assert config.load(path).limits.max_question_rounds == 3
