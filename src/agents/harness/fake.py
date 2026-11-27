"""A harness that calls no model, for tests and dry runs.

Instead of an agent CLI it runs fake_agent.py, a small script that follows instructions
written in the task itself. See fake_agent.py for what a task can ask for.
"""

import json
import sys
from typing import Any

from agents.harness.base import Command, Harness, HarnessResult, NodeRequest


class FakeHarness(Harness):
    name = "fake"

    def build_command(self, request: NodeRequest) -> Command:
        argv = [sys.executable, "-m", "agents.harness.fake_agent"]
        if request.resume_session_id:
            argv += ["--resume", request.resume_session_id]
        return Command(argv=argv, stdin=request.prompt)

    def build_planner_command(self, request: NodeRequest) -> Command:
        raise NotImplementedError("the fake harness has no interactive planner")

    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> HarnessResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            output = (stderr or stdout).strip()[-2000:]
            return HarnessResult(
                ok=False,
                summary=f"fake agent exited with code {exit_code}: {output}",
                cost_usd=None,
            )
        return HarnessResult(
            ok=exit_code == 0,
            summary=payload.get("result", ""),
            cost_usd=self.extract_cost(payload),
            session_id=payload.get("session_id", ""),
        )

    def extract_cost(self, payload: dict[str, Any]) -> float | None:
        cost = payload.get("total_cost_usd")
        return float(cost) if isinstance(cost, int | float) else None
