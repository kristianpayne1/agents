"""Rewrite both lockfiles after changing a version in mise.toml or pyproject.toml.

    uv run python scripts/lock.py

mise.lock gets a checksum for every platform the dotfiles support, and uv.lock gets the
hashes of every Python package.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLATFORMS = "linux-x64,linux-arm64,macos-x64,macos-arm64"


def main() -> int:
    commands = [
        ["mise", "lock", "--platform", PLATFORMS],
        ["uv", "lock"],
    ]
    for command in commands:
        print(" ".join(command))
        result = subprocess.run(command, cwd=REPO, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
