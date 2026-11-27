"""Run state: the tree of nodes and their budgets, in one small SQLite file per run.

Every splitter runs its own copy of the delegate server, so several processes read and write
this file at once. Each function here opens the file, does its work inside one write
transaction, and closes it again, so no process ever sees a half-finished update.

How budgets work:

- `budget_usd` is what a node was given for itself and everything below it.
- `cost_usd` is what the node's own agent process spent.
- `held_usd` is what the node counts against its parent: the whole budget while it runs,
  and only what its subtree really spent once it finishes. The drop from one to the other
  is the refund.
"""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT_ID = "root"

# A node's status. A node that never started is `refused` or `over_budget`. A node that
# stopped to ask its parent something is `needs_decision`.
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
NEEDS_DECISION = "needs_decision"
REFUSED = "refused"
OVER_BUDGET = "over_budget"

# A node's merge state, set by `integrate`.
NOT_MERGED = ""
MERGED = "merged"
CONFLICT = "conflict"

SCHEMA = """
CREATE TABLE run (
    run_id      TEXT PRIMARY KEY,
    repo_path   TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    budget_usd  REAL NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE node (
    node_id        TEXT PRIMARY KEY,
    parent_id      TEXT,
    depth          INTEGER NOT NULL,
    tier           TEXT NOT NULL,
    harness        TEXT NOT NULL,
    model          TEXT NOT NULL,
    kind           TEXT NOT NULL,
    task           TEXT NOT NULL,
    branch         TEXT NOT NULL DEFAULT '',
    worktree       TEXT NOT NULL DEFAULT '',
    base_commit    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,
    budget_usd     REAL NOT NULL,
    cost_usd       REAL NOT NULL DEFAULT 0,
    held_usd       REAL NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    summary        TEXT NOT NULL DEFAULT '',
    merge_state    TEXT NOT NULL DEFAULT '',
    test_exit_code INTEGER,
    started_at     REAL NOT NULL,
    finished_at    REAL,
    contract        TEXT NOT NULL DEFAULT '',
    files           TEXT NOT NULL DEFAULT '[]',
    test_cmd        TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL DEFAULT '',
    answers         TEXT NOT NULL DEFAULT '',
    question_rounds INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Run:
    run_id: str
    repo_path: str
    repo_name: str
    base_commit: str
    budget_usd: float
    created_at: float


@dataclass(frozen=True)
class Node:
    node_id: str
    parent_id: str | None
    depth: int
    tier: str
    harness: str
    model: str
    kind: str
    task: str
    branch: str
    worktree: str
    base_commit: str
    status: str
    budget_usd: float
    cost_usd: float
    held_usd: float
    attempts: int
    summary: str
    merge_state: str
    test_exit_code: int | None
    started_at: float
    finished_at: float | None
    # What the node was asked to do, kept so that it can be resumed with an answer.
    contract: str = ""
    files: str = "[]"  # a JSON list
    test_cmd: str = ""
    # The agent CLI's session, so that a node that asked a question can be resumed.
    session_id: str = ""
    # The questions it asked and the answers its parent gave, oldest first, as
    # "Q: ...\nA: ..." pairs separated by blank lines.
    answers: str = ""
    # How many of this node's delegate, delegate_many and answer calls brought back at least
    # one question. See Limits.max_question_rounds.
    question_rounds: int = 0

    @property
    def file_list(self) -> list[str]:
        return json.loads(self.files)


@contextmanager
def _transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the run file and hold its write lock until the block ends."""
    # isolation_level=None turns off Python's implicit transactions, so the BEGIN and
    # COMMIT below are the only ones. The timeout is how long to wait for another process.
    connection = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


def create_run(
    db_path: Path,
    *,
    run_id: str,
    repo_path: str,
    repo_name: str,
    base_commit: str,
    budget_usd: float,
    root_tier: str,
    root_harness: str,
    root_model: str,
    root_branch: str,
    root_worktree: str,
) -> None:
    """Create the run file with its root node. Fails if the file already exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        raise FileExistsError(db_path)
    now = time.time()
    with _transaction(db_path) as connection:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO run VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, repo_path, repo_name, base_commit, budget_usd, now),
        )
        connection.execute(
            """INSERT INTO node (node_id, parent_id, depth, tier, harness, model, kind, task,
                                 branch, worktree, base_commit, status, budget_usd, started_at)
               VALUES (?, NULL, 0, ?, ?, ?, 'root', '', ?, ?, ?, ?, ?, ?)""",
            (
                ROOT_ID,
                root_tier,
                root_harness,
                root_model,
                root_branch,
                root_worktree,
                base_commit,
                RUNNING,
                budget_usd,
                now,
            ),
        )


def get_run(db_path: Path) -> Run:
    with _transaction(db_path) as connection:
        row = connection.execute("SELECT * FROM run").fetchone()
    return Run(**dict(row))


def get_node(db_path: Path, node_id: str) -> Node | None:
    with _transaction(db_path) as connection:
        row = connection.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
    return Node(**dict(row)) if row else None


def all_nodes(db_path: Path) -> list[Node]:
    """Every node in the run, in the order they were created."""
    with _transaction(db_path) as connection:
        rows = connection.execute("SELECT * FROM node ORDER BY started_at, node_id").fetchall()
    return [Node(**dict(row)) for row in rows]


def remaining_usd(db_path: Path, node_id: str) -> float:
    """What a node can still hand out: its budget, less its own cost and its children."""
    with _transaction(db_path) as connection:
        return _remaining(connection, node_id)


def add_child(
    db_path: Path,
    *,
    parent_id: str,
    depth: int,
    tier: str,
    harness: str,
    model: str,
    kind: str,
    task: str,
    budget_usd: float,
    refusal: str | None,
    contract: str = "",
    files: list[str] | None = None,
    test_cmd: str = "",
) -> Node:
    """Record a new child and reserve its budget from the parent, in one step.

    The child comes back `running` if it may start. It comes back `refused` if the caller
    passed a refusal, or `over_budget` if the parent can't cover `budget_usd`; neither of
    those holds any budget.
    """
    now = time.time()
    with _transaction(db_path) as connection:
        sibling_count = connection.execute(
            "SELECT COUNT(*) FROM node WHERE parent_id = ?", (parent_id,)
        ).fetchone()[0]
        node_id = f"{parent_id}.{sibling_count + 1}"

        if refusal is not None:
            status, summary = REFUSED, refusal
        elif budget_usd > _remaining(connection, parent_id) + 1e-9:
            left = _remaining(connection, parent_id)
            status = OVER_BUDGET
            summary = f"asked for ${budget_usd:.2f} but only ${left:.2f} is left"
        else:
            status, summary = RUNNING, ""

        started = status == RUNNING
        connection.execute(
            """INSERT INTO node (node_id, parent_id, depth, tier, harness, model, kind, task,
                                 status, budget_usd, held_usd, summary, started_at, finished_at,
                                 contract, files, test_cmd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                parent_id,
                depth,
                tier,
                harness,
                model,
                kind,
                task,
                status,
                budget_usd,
                budget_usd if started else 0.0,
                summary,
                now,
                None if started else now,
                contract,
                json.dumps(files or []),
                test_cmd,
            ),
        )
        row = connection.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
    return Node(**dict(row))


def set_workspace(
    db_path: Path, node_id: str, *, branch: str, worktree: str, base_commit: str
) -> None:
    with _transaction(db_path) as connection:
        connection.execute(
            "UPDATE node SET branch = ?, worktree = ?, base_commit = ? WHERE node_id = ?",
            (branch, worktree, base_commit, node_id),
        )


def finish_node(
    db_path: Path,
    node_id: str,
    *,
    status: str,
    cost_usd: float,
    attempts: int,
    summary: str,
    test_exit_code: int | None,
    session_id: str = "",
) -> Node:
    """Record a node's result and refund what its subtree didn't spend."""
    with _transaction(db_path) as connection:
        children_held = _children_held(connection, node_id)
        connection.execute(
            """UPDATE node
               SET status = ?, cost_usd = ?, held_usd = ?, attempts = ?, summary = ?,
                   test_exit_code = ?, finished_at = ?, session_id = ?
               WHERE node_id = ?""",
            (
                status,
                cost_usd,
                cost_usd + children_held,
                attempts,
                summary,
                test_exit_code,
                time.time(),
                session_id,
                node_id,
            ),
        )
        row = connection.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
    return Node(**dict(row))


def add_question_round(db_path: Path, node_id: str) -> None:
    """Count one more call of this node's that brought back a question."""
    with _transaction(db_path) as connection:
        connection.execute(
            "UPDATE node SET question_rounds = question_rounds + 1 WHERE node_id = ?",
            (node_id,),
        )


def reopen_node(db_path: Path, node_id: str, *, budget_usd: float, answer: str) -> str | None:
    """Start a node that asked a question again, with `budget_usd` more to spend.

    The extra budget is reserved from the parent in the same step, as `add_child` does.
    Returns why the node can't be reopened, or None once it has been.
    """
    with _transaction(db_path) as connection:
        row = connection.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
        if row is None:
            return f"no node '{node_id}' in this run"
        node = Node(**dict(row))
        if node.status != NEEDS_DECISION:
            return f"'{node_id}' is {node.status}, not needs_decision: there is nothing to answer"
        if not node.session_id:
            return f"'{node_id}' has no session to resume. Delegate its task again instead."
        assert node.parent_id is not None
        left = _remaining(connection, node.parent_id)
        if budget_usd > left + 1e-9:
            return f"asked for ${budget_usd:.2f} but only ${left:.2f} is left"

        # The node's budget becomes what it has used so far plus the new slice, and while it
        # runs it holds all of that against its parent, as a new child would.
        new_budget = node.cost_usd + _children_held(connection, node_id) + budget_usd
        answers = node.answers + ("\n\n" if node.answers else "") + answer.strip()
        connection.execute(
            """UPDATE node
               SET status = ?, budget_usd = ?, held_usd = ?, answers = ?, finished_at = NULL
               WHERE node_id = ?""",
            (RUNNING, new_budget, new_budget, answers, node_id),
        )
    return None


def set_merge_state(db_path: Path, node_id: str, merge_state: str) -> None:
    with _transaction(db_path) as connection:
        connection.execute(
            "UPDATE node SET merge_state = ? WHERE node_id = ?", (merge_state, node_id)
        )


def _children_held(connection: sqlite3.Connection, node_id: str) -> float:
    return connection.execute(
        "SELECT COALESCE(SUM(held_usd), 0) FROM node WHERE parent_id = ?", (node_id,)
    ).fetchone()[0]


def _remaining(connection: sqlite3.Connection, node_id: str) -> float:
    row = connection.execute(
        "SELECT budget_usd, cost_usd FROM node WHERE node_id = ?", (node_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no node '{node_id}' in this run")
    return row["budget_usd"] - row["cost_usd"] - _children_held(connection, node_id)
