"""Git worktrees, branches and merges. Every git command the project runs is in this file.

Nothing here pushes, and nothing here touches the user's own checkout: all work happens in
worktrees under the state directory, on branches named `agents/<run_id>/<node_id>`.
"""

import fcntl
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    """A git command failed."""


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    # Filled in when the merge hit conflicts (the merge is then aborted):
    conflict_files: tuple[str, ...] = ()
    details: str = ""


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one git command in `cwd` and capture its output."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {output}")
    return result


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock that other processes (and threads) wait for.

    Don't `await` anything while holding it: a second request in the same process would
    block the event loop waiting for a lock that can then never be released.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def branch_name(run_id: str, node_id: str) -> str:
    # Node ids use dots (root.2.1), not slashes. Git stores each branch as a file, so
    # `root` and `root/2` can't both exist; `root` and `root.2` can.
    return f"agents/{run_id}/{node_id}"


def main_repo(path: Path) -> Path:
    """The main checkout that `path` belongs to, even when `path` is one of its worktrees."""
    result = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if result.returncode != 0:
        raise GitError(f"{path} is not inside a git repository")
    return Path(result.stdout.strip()).parent


def head_commit(cwd: Path, ref: str = "HEAD") -> str:
    result = git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise GitError(f"{cwd} has no commit at {ref} yet; make a first commit and try again")
    return result.stdout.strip()


def add_worktree(repo: Path, worktree: Path, branch: str, start_point: str) -> None:
    """Create `branch` at `start_point` and check it out in a new worktree."""
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), start_point)


def remove_worktree(repo: Path, worktree: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def prune_worktrees(repo: Path) -> None:
    git(repo, "worktree", "prune", check=False)


def delete_branch(repo: Path, branch: str) -> bool:
    return git(repo, "branch", "-D", branch, check=False).returncode == 0


def is_dirty(worktree: Path, *, include_untracked: bool = True) -> bool:
    """True if there are uncommitted changes (and, unless switched off, untracked files)."""
    untracked = "all" if include_untracked else "no"
    output = git(worktree, "status", "--porcelain", f"--untracked-files={untracked}").stdout
    return output.strip() != ""


def commit_all(worktree: Path, message: str) -> bool:
    """Commit everything in the worktree. Returns False if there was nothing to commit."""
    if not is_dirty(worktree):
        return False
    git(worktree, "add", "--all")
    git(worktree, "commit", "--message", message)
    return True


def diff_stat(worktree: Path, base_commit: str) -> str:
    """What changed on this worktree's branch since `base_commit`."""
    return git(worktree, "diff", "--stat", base_commit, "HEAD").stdout.rstrip()


def changed_files(worktree: Path, base_commit: str) -> list[str]:
    output = git(worktree, "diff", "--name-only", base_commit, "HEAD").stdout
    return [line for line in output.splitlines() if line]


def merge_no_ff(worktree: Path, branch: str, message: str) -> MergeResult:
    """Merge `branch` into the worktree's branch with a merge commit.

    On a conflict the merge is aborted, so the worktree is left exactly as it was, and the
    conflicting files and diff are returned for the caller to act on.
    """
    result = git(worktree, "merge", "--no-ff", "--message", message, branch, check=False)
    if result.returncode == 0:
        return MergeResult(merged=True)

    unmerged = git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    conflict_files = tuple(line for line in unmerged.splitlines() if line)
    if conflict_files:
        details = git(worktree, "diff", check=False).stdout
    else:
        # Not a content conflict (for example, the branch doesn't exist).
        details = (result.stderr or result.stdout).strip()
    git(worktree, "merge", "--abort", check=False)
    return MergeResult(merged=False, conflict_files=conflict_files, details=details)
