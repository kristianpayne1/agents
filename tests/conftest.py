"""Fixtures: every test gets its own state directory, git settings and temporary repo."""

from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import git, write_roles

from agents import config, engine, paths
from agents.config import Config

RUN_CONTEXT = [
    engine.ENV_RUN_ID,
    engine.ENV_NODE_ID,
    engine.ENV_DEPTH,
    engine.ENV_BUDGET,
    engine.ENV_WORKTREE,
    engine.ENV_ATTEMPT,
]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the real state directory and the user's git config."""
    monkeypatch.setenv(paths.ENV_STATE_DIR, str(tmp_path / "state"))

    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")

    # If the tests are run from inside a real run, its context must not leak in.
    for name in RUN_CONTEXT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with one commit, standing in for the user's checkout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch", "main")
    (repo / "README.md").write_text("# toy repo\n")
    git(repo, "add", "--all")
    git(repo, "commit", "--message", "first commit")
    return repo


@pytest.fixture
def make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Config]:
    """Write a roles.toml that uses the fake harness, with any limits overridden."""

    def make(**limits: float) -> Config:
        path = tmp_path / "roles.toml"
        monkeypatch.setenv(config.ENV_CONFIG, str(path))
        return write_roles(path, **limits)

    return make


@pytest.fixture
def settings(make_settings: Callable[..., Config]) -> Config:
    """planner -> heavy -> leaf, the same shape as the default config."""
    return make_settings()


@pytest.fixture
def leaf_settings(make_settings: Callable[..., Config]) -> Config:
    """max_depth = 1, so the root's children are leaves. Handy for testing leaves alone."""
    return make_settings(max_depth=1)
