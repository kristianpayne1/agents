"""`agents example`: make a small repo to try the tree on.

The example lives in examples/<name>/ in this repo:

- repo/ is what the new repo starts as: a little code, its tests, and TASK.md, a task that
  deliberately leaves some choices open, so that a run has decisions to make and questions
  to ask.
- accept/ holds acceptance tests that check only what the task settles. They stay out of the
  new repo, so no node sees them; `agents bench --accept-dir` copies them in to judge a
  result.
"""

import shutil
from pathlib import Path

from agents import worktrees

# The examples ship at the top of the repo, two levels above this package (src/agents/).
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
DEFAULT_EXAMPLE = "orders"
TEST_CMD = "python3 -m unittest"
ACCEPT_CMD = "python3 -m unittest test_accept"


def example_dir(name: str = DEFAULT_EXAMPLE) -> Path:
    path = EXAMPLES_DIR / name
    if not (path / "repo").is_dir():
        known = ", ".join(sorted(item.name for item in EXAMPLES_DIR.iterdir() if item.is_dir()))
        raise KeyError(f"no example '{name}' (known: {known})")
    return path


def make_repo(destination: Path, name: str = DEFAULT_EXAMPLE) -> Path:
    """Copy an example's starting files to `destination` as a new git repo with one commit."""
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"{destination} already exists and isn't empty")
    shutil.copytree(example_dir(name) / "repo", destination, dirs_exist_ok=True)
    worktrees.git(destination, "init", "--initial-branch", "main")
    worktrees.git(destination, "add", "--all")
    worktrees.git(destination, "commit", "--message", f"Start the {name} example")
    return destination


def next_steps(destination: Path, name: str = DEFAULT_EXAMPLE) -> str:
    """How to use the new repo, as text to print."""
    accept = example_dir(name) / "accept"
    return "\n".join(
        [
            f"Made {destination}, a git repo with the {name} example.",
            "",
            "Run the tree on it, and answer the planner's questions as they come:",
            f"  cd {destination}",
            '  agents run "Do the task in TASK.md"',
            "",
            "Afterwards, see what was decided and asked, and how to undo any of it:",
            "  agents decisions",
            "",
            "Or compare the tree with single-model sessions (real model calls):",
            f'  agents bench --task TASK.md --test-cmd "{TEST_CMD}" \\',
            f'    --accept-dir {accept} --accept-cmd "{ACCEPT_CMD}"',
        ]
    )
