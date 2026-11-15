"""Harness adapters: one per agent CLI. `get_harness` looks one up by its roles.toml name."""

from agents.harness.base import Harness
from agents.harness.claude import ClaudeHarness
from agents.harness.fake import FakeHarness

_HARNESSES: dict[str, Harness] = {
    ClaudeHarness.name: ClaudeHarness(),
    FakeHarness.name: FakeHarness(),
}

HARNESS_NAMES = tuple(_HARNESSES)


def get_harness(name: str) -> Harness:
    return _HARNESSES[name]
