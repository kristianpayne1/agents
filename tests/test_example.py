"""The example repo, and a whole run on it with the fake harness.

The scenario test plays out what a real run should do on the orders example: the planner
hands the feature to a heavy splitter, whose implementation leaf stops to ask what a bad line
should do. The splitter answers, the leaf carries on in its own session, and everything is
merged up to the root, where the hidden acceptance tests pass. No model is called: the fake
agent writes the code a model would.
"""

import shutil
from pathlib import Path

import pytest
from helpers import checkout_state, delegate, fake, git, leaf_call

from agents import cli, engine, example, log, paths, tree
from agents.config import Config

ORDERS_PY = '''"""Orders for a small shop: read them and add them up."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

HEADER = "order_id,sku,quantity,unit_price"


@dataclass(frozen=True)
class Order:
    order_id: str
    sku: str
    quantity: int
    unit_price: Decimal

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class Summary:
    count: int
    total: Decimal
    best_seller: str | None


def parse_line(line: str) -> Order:
    """One order from a line like `A1,widget,3,2.50`."""
    order_id, sku, quantity, unit_price = (field.strip() for field in line.split(","))
    return Order(order_id, sku, int(quantity), Decimal(unit_price))


def load_orders(path: str | Path) -> list[Order]:
    orders = []
    for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip() or line.strip() == HEADER:
            continue
        try:
            orders.append(parse_line(line))
        except ValueError as error:
            raise ValueError(f"line {number}: {error}") from error
    return orders


def summarise(orders: list[Order]) -> Summary:
    quantities: Counter[str] = Counter()
    for order in orders:
        quantities[order.sku] += order.quantity
    best = quantities.most_common(1)[0][0] if quantities else None
    return Summary(len(orders), sum((order.total for order in orders), Decimal(0)), best)
'''

TESTS_PY = """import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from orders import Order, load_orders, parse_line, summarise


class OrdersTest(unittest.TestCase):
    def test_parse_line(self) -> None:
        self.assertEqual(parse_line("A1,widget,3,2.50"), Order("A1", "widget", 3, Decimal("2.50")))

    def test_bad_line_names_its_number(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "orders.csv"
            path.write_text("A1,widget,3,2.50\\nA2,widget,lots,2.50\\n")
            with self.assertRaisesRegex(ValueError, "line 2"):
                load_orders(path)

    def test_summary(self) -> None:
        orders = [parse_line("A1,widget,3,2.50"), parse_line("A2,gadget,1,10.00")]
        summary = summarise(orders)
        self.assertEqual(summary.count, 2)
        self.assertEqual(summary.total, Decimal("17.50"))
        self.assertEqual(summary.best_seller, "widget")


if __name__ == "__main__":
    unittest.main()
"""

BAD_LINE_QUESTION = (
    "Wrote load_orders and summarise.\n\n## Decision needed\n\n"
    "What should load_orders do with a line it can't read?\n"
    "Options: A) raise ValueError naming the line number B) skip it and return the rest\n"
    "I'd pick: A, because the task says a bad line must not go unnoticed.\n"
)


def test_example_repo_starts_green_with_one_commit(tmp_path: Path) -> None:
    repo = example.make_repo(tmp_path / "orders")

    assert git(repo, "log", "--format=%s") == "Start the orders example"
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "TASK.md").exists()
    # The acceptance tests are kept out, so no node can see them.
    assert not (repo / "test_accept.py").exists()
    exit_code, output = engine.run_test(example.TEST_CMD, repo, 60)
    assert exit_code == 0, output


def test_acceptance_tests_fail_before_the_task_is_done(tmp_path: Path) -> None:
    repo = example.make_repo(tmp_path / "orders")
    shutil.copytree(example.example_dir() / "accept", repo, dirs_exist_ok=True)

    exit_code, output = engine.run_test(example.ACCEPT_CMD, repo, 60)

    assert exit_code != 0
    assert "load_orders" in output


def test_acceptance_tests_pass_on_a_finished_example(tmp_path: Path) -> None:
    repo = example.make_repo(tmp_path / "orders")
    (repo / "orders.py").write_text(ORDERS_PY)
    shutil.copytree(example.example_dir() / "accept", repo, dirs_exist_ok=True)

    exit_code, output = engine.run_test(example.ACCEPT_CMD, repo, 60)

    assert exit_code == 0, output


def test_make_repo_will_not_fill_a_folder_that_has_files(tmp_path: Path) -> None:
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "notes.txt").write_text("mine\n")

    with pytest.raises(FileExistsError):
        example.make_repo(tmp_path / "orders")


def test_unknown_example_is_named(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match=r"no example 'nope'.*orders"):
        example.make_repo(tmp_path / "x", name="nope")


def test_next_steps_say_how_to_run_and_judge_it(tmp_path: Path) -> None:
    text = example.next_steps(tmp_path / "orders")

    assert 'agents run "Do the task in TASK.md"' in text
    assert "agents decisions" in text
    assert str(example.example_dir() / "accept") in text


def test_a_question_goes_up_is_answered_and_the_work_comes_back_down(
    tmp_path: Path, settings: Config
) -> None:
    repo = example.make_repo(tmp_path / "orders")
    before = checkout_state(repo)
    root = engine.start_run(repo, settings)

    implement = leaf_call(
        "orders.py",
        task=fake(
            write={"orders.py": ORDERS_PY.replace('f"line {number}: {error}"', '"?"')},
            result=BAD_LINE_QUESTION,
        ),
        test_cmd=example.TEST_CMD,
        kind="feature",
    )
    write_tests = leaf_call(
        "test_orders.py",
        task=fake(write={"test_orders.py": TESTS_PY}),
        # The tests can't pass before the code is merged, so this only checks they compile.
        test_cmd="python3 -m py_compile test_orders.py",
        kind="tests",
    )
    reply = "A: raise ValueError naming the line number.\n" + fake(
        write={"orders.py": ORDERS_PY}, result="Done: a bad line raises ValueError."
    )
    heavy_task = fake(
        delegate=[implement, write_tests],
        answers=[{"child": 0, "answer": reply, "budget_usd": 1.0}],
        integrate=example.TEST_CMD,
        result=(
            "Implemented load_orders and summarise, with tests.\n\n## Decisions made\n\n"
            "- A bad line raises ValueError naming its line number. (root.1.1, orders.py)\n"
            "- Summary.best_seller is None for no orders. (root.1, orders.py)\n"
        ),
    )

    heavy = delegate(
        root, settings, task=heavy_task, test_cmd=example.TEST_CMD, kind="feature", budget_usd=5.0
    )
    merged = engine.integrate(
        root, settings, node_ids=[heavy["node_id"]], test_cmd=example.TEST_CMD
    )

    assert heavy["status"] == "passed"
    assert merged["status"] == "passed"
    assert "Summary.best_seller is None" in heavy["decisions"]

    # The leaf asked once, was answered in its own session, and passed.
    db = paths.run_db(root.run_id)
    leaf = tree.get_node(db, "root.1.1")
    assert leaf is not None
    assert leaf.status == tree.PASSED
    assert leaf.answers.startswith("Q: What should load_orders do with a line it can't read?")
    assert leaf.merge_state == tree.MERGED
    runs = [entry for entry in log.read() if entry.get("node_id") == "root.1.1"]
    assert [(entry["status"], entry["answered"]) for entry in runs] == [
        ("needs_decision", False),
        ("passed", True),
    ]
    heavy_node = tree.get_node(db, "root.1")
    assert heavy_node is not None
    assert heavy_node.question_rounds == 1

    # The hidden acceptance tests pass on the root branch.
    shutil.copytree(example.example_dir() / "accept", root.worktree, dirs_exist_ok=True)
    exit_code, output = engine.run_test(example.ACCEPT_CMD, root.worktree, 60)
    assert exit_code == 0, output

    # Everything decided and asked is in one place, with how to undo it.
    text = cli.format_decisions(engine.decisions(root.run_id))
    assert "root.1  heavy, passed" in text
    assert "root.1.1  leaf, passed" in text
    assert "answered: Q: What should load_orders do with a line it can't read?" in text
    assert "git revert -m 1" in text

    assert checkout_state(repo) == before
