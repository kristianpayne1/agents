"""The role prompts, kept as Markdown files next to this one."""

from pathlib import Path


def load(role: str) -> str:
    """The system prompt for a role: `splitter` or `leaf`."""
    return (Path(__file__).parent / f"{role}.md").read_text()
