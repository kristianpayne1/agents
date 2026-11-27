"""The JSONL log and the report built from it.

One line is written per finished node (and one per `integrate` call). The log is what later
decisions are made from: how big a leaf task can be, whether a mid tier is needed, whether
local leaves are worth building. `agents report` summarises it per tier and per kind.
"""

import json
from collections import defaultdict
from typing import Any

from agents import paths

NODE = "node"
INTEGRATE = "integrate"


def write(event: dict[str, Any]) -> None:
    """Append one event. A single short write in append mode doesn't interleave with others."""
    path = paths.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(event) + "\n")


def read() -> list[dict[str, Any]]:
    path = paths.log_path()
    if not path.exists():
        return []
    with path.open() as file:
        return [json.loads(line) for line in file if line.strip()]


def report(events: list[dict[str, Any]]) -> str:
    """Cost and pass rate per tier and per kind, as plain text."""
    nodes = [event for event in events if event.get("event") == NODE]
    if not nodes:
        return "No finished nodes in the log yet."

    integrations = [event for event in events if event.get("event") == INTEGRATE]
    conflicts = sum(1 for event in integrations if event["status"] == "conflict")
    runs = {event["run_id"] for event in nodes}
    total_cost = sum(event["cost_usd"] for event in nodes)

    lines = [
        f"{len(nodes)} nodes over {len(runs)} runs, ${total_cost:.2f} in total",
        f"{len(integrations)} integrate calls, {conflicts} with a merge conflict",
        "",
        *_table("tier", nodes),
        "",
        *_table("kind", nodes),
    ]
    unknown_cost = sum(1 for event in nodes if not event.get("cost_known", True))
    if unknown_cost:
        lines += ["", f"{unknown_cost} nodes reported no cost; their cost is counted as $0."]
    return "\n".join(lines)


def _table(group_by: str, nodes: list[dict[str, Any]]) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        groups[node[group_by]].append(node)

    header = (
        f"{group_by:<12} {'nodes':>5} {'passed':>6} {'failed':>6} {'asked':>5} {'refused':>7} "
        f"{'pass rate':>9} {'cost':>8} {'per node':>8} {'attempts':>8} {'seconds':>8}"
    )
    lines = [header, "-" * len(header)]
    for name in sorted(groups):
        group = groups[name]
        passed = _count(group, "passed")
        failed = _count(group, "failed")
        # Nodes that stopped to ask their parent for a decision.
        asked = _count(group, "needs_decision")
        refused = _count(group, "refused") + _count(group, "over_budget")
        # Pass rate is out of the nodes that ran to the end, without stopping to ask.
        finished = passed + failed
        pass_rate = f"{100 * passed / finished:.0f}%" if finished else "-"
        ran = [node for node in group if node["status"] in ("passed", "failed", "needs_decision")]
        cost = sum(node["cost_usd"] for node in group)
        lines.append(
            f"{name:<12} {len(group):>5} {passed:>6} {failed:>6} {asked:>5} {refused:>7} "
            f"{pass_rate:>9} {cost:>8.2f} {_average(ran, 'cost_usd'):>8.2f} "
            f"{_average(ran, 'attempts'):>8.1f} {_average(ran, 'duration_s'):>8.0f}"
        )
    return lines


def _count(nodes: list[dict[str, Any]], status: str) -> int:
    return sum(1 for node in nodes if node["status"] == status)


def _average(nodes: list[dict[str, Any]], key: str) -> float:
    if not nodes:
        return 0.0
    return sum(node[key] for node in nodes) / len(nodes)
