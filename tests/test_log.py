"""The report built from the log."""

from typing import Any

from agents import log


def node(tier: str, kind: str, status: str, cost: float = 0.1, **extra: Any) -> dict[str, Any]:
    return {
        "event": "node",
        "run_id": "run-1",
        "tier": tier,
        "kind": kind,
        "status": status,
        "cost_usd": cost,
        "attempts": 1,
        "duration_s": 10.0,
        **extra,
    }


def row(report: str, name: str) -> str:
    """The first table row that starts with `name`, with single spaces between its cells."""
    line = next(line for line in report.splitlines() if line.startswith(name + " "))
    return " ".join(line.split())


def test_empty_log() -> None:
    assert log.report([]) == "No finished nodes in the log yet."


def test_report_has_cost_and_pass_rate_per_tier_and_kind() -> None:
    events = [
        node("heavy", "feature", "passed", cost=1.0),
        node("leaf", "tests", "passed", cost=0.02),
        node("leaf", "tests", "failed", cost=0.04, attempts=2),
        node("leaf", "feature", "passed", cost=0.02),
        node("leaf", "feature", "refused", cost=0.0),
        {"event": "integrate", "run_id": "run-1", "status": "conflict"},
        {"event": "integrate", "run_id": "run-1", "status": "passed"},
    ]

    report = log.report(events)

    assert "5 nodes over 1 runs, $1.08 in total" in report
    assert "2 integrate calls, 1 with a merge conflict" in report
    # name, nodes, passed, failed, asked, refused, pass rate, cost, per node, attempts, seconds
    assert row(report, "heavy") == "heavy 1 1 0 0 0 100% 1.00 1.00 1.0 10"
    assert row(report, "leaf") == "leaf 4 2 1 0 1 67% 0.08 0.03 1.3 10"
    assert row(report, "tests").startswith("tests 2 1 1 0 0 50%")


def test_nodes_without_a_cost_are_called_out() -> None:
    report = log.report([node("leaf", "tests", "passed", cost=0.0, cost_known=False)])

    assert "1 nodes reported no cost" in report


def test_write_then_read_round_trips() -> None:
    log.write(node("leaf", "tests", "passed"))
    log.write(node("leaf", "rename", "failed"))

    assert [event["kind"] for event in log.read()] == ["tests", "rename"]


def test_nodes_that_asked_are_counted_but_not_in_the_pass_rate() -> None:
    events = [
        node("leaf", "tests", "passed"),
        node("leaf", "tests", "needs_decision"),
    ]

    report = log.report(events)

    # name, nodes, passed, failed, asked, refused, pass rate, ...
    assert row(report, "leaf").startswith("leaf 2 1 0 1 0 100%")
