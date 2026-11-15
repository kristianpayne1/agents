"""Where run state lives on this machine. Nothing here is inside a repo.

Everything sits under one state directory:

    <state>/log.jsonl                          one line per finished node
    <state>/runs/<run_id>.sqlite               the run's tree and budgets
    <state>/worktrees/<repo>/<run_id>/<node>   one git worktree per node
    <state>/locks/                             lock files
"""

import os
from pathlib import Path

ENV_STATE_DIR = "AGENTS_STATE_DIR"


def state_dir() -> Path:
    """$AGENTS_STATE_DIR, or agents/ under $XDG_STATE_HOME, or ~/.local/state/agents."""
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override)
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "agents"


def log_path() -> Path:
    return state_dir() / "log.jsonl"


def runs_dir() -> Path:
    return state_dir() / "runs"


def run_db(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.sqlite"


def run_worktrees(repo_name: str, run_id: str) -> Path:
    return state_dir() / "worktrees" / repo_name / run_id


def lock_file(name: str) -> Path:
    return state_dir() / "locks" / f"{name}.lock"
