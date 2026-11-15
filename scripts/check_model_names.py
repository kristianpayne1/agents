"""Fail if a model name appears anywhere except roles.toml. CI runs this.

Code, prompts and docs refer to tiers, so that changing a model is a one-file change. The
names to look for come from roles.toml itself: every `model` value, every pool member's
name, and the family word inside a model id. A general pattern also catches model ids that
aren't in roles.toml right now.

    uv run python scripts/check_model_names.py
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROLES = REPO / "roles.toml"

# Files that may name models, or that are generated.
SKIPPED = {"roles.toml", "uv.lock", "mise.lock"}

# Something that looks like a model id: a vendor word, a family word, a version number.
MODEL_ID = r"\b[a-z]+-[a-z]+-\d+(?:[-.]\d+)*\b"


def names_in_roles() -> set[str]:
    with ROLES.open("rb") as file:
        roles = tomllib.load(file)

    names: set[str] = set()
    for tier in roles["tier"]:
        agents = [tier, *tier.get("pool", [])]
        for agent in agents:
            if "model" in agent:
                names.add(agent["model"])
                # The words inside the id, such as the family, but not the harness's own name.
                names.update(word for word in agent["model"].split("-") if word.isalpha())
                names.discard(agent["harness"])
        for member in tier.get("pool", []):
            names.add(member["name"])
    return names


def files_to_check() -> list[Path]:
    """Every file git knows about or would add, so ignored files (.venv) are left out."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [REPO / name for name in listed if name not in SKIPPED and (REPO / name).is_file()]


def main() -> int:
    names = sorted(names_in_roles(), key=len, reverse=True)
    pattern = re.compile(
        "|".join([MODEL_ID, *(rf"\b{re.escape(name)}\b" for name in names)]), re.IGNORECASE
    )

    problems: list[str] = []
    for path in files_to_check():
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            for match in pattern.finditer(line):
                problems.append(f"{path.relative_to(REPO)}:{number}: {match.group()}")

    if problems:
        print("Model names outside roles.toml:")
        print("\n".join(problems))
        return 1
    print("No model names outside roles.toml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
