"""Pick which member of the leaf pool runs a task.

For now the pool has one member and `route` returns the first. Later rules (is the machine
awake, does the task fit the model's context, how often does this member pass this kind of
task) belong here, which is why `route` already gets the whole task.
"""

from dataclasses import dataclass

from agents.config import PoolMember


@dataclass(frozen=True)
class LeafTask:
    kind: str
    task: str
    contract: str
    files: list[str]
    test_cmd: str


def route(task: LeafTask, pool: tuple[PoolMember, ...]) -> PoolMember:
    return pool[0]
