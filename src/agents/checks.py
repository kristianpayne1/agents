"""Task-size checks for leaf tasks. `delegate` enforces these; they aren't just advice."""

from agents.config import Limits

ADVICE = "Split this further, or keep it at the splitter with a reason."


def word_count(task: str, contract: str) -> int:
    return len(task.split()) + len(contract.split())


def check_leaf_task(
    *, task: str, contract: str, files: list[str], test_cmd: str | None, limits: Limits
) -> str | None:
    """Why this task is too big or too vague for a leaf, or None if it's fine."""
    problems: list[str] = []

    if not files:
        problems.append("`files` is missing: list the files the leaf may edit")
    elif len(files) > limits.leaf_max_files:
        problems.append(f"{len(files)} files is over the limit of {limits.leaf_max_files}")

    if not test_cmd or not test_cmd.strip():
        problems.append("`test_cmd` is missing: give the command that proves the task is done")

    words = word_count(task, contract)
    if words > limits.leaf_max_task_words:
        problems.append(
            f"task plus contract is {words} words, over the limit of {limits.leaf_max_task_words}"
        )

    if not problems:
        return None
    return f"Refused: {'; '.join(problems)}. {ADVICE}"
