"""Install a pinned, checksum-verified mise on a CI runner. Used by .github/workflows/ci.yml.

On your own machine mise comes from the dotfiles, so there's no need to run this. The
version and SHA-256 sums are the ones the dotfiles pin; update them together.

It needs only the Python that the runner already has.
"""

import hashlib
import os
import platform
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

MISE_VERSION = "v2026.10.21"
SHA256 = {
    "linux-x64": "f56cc56b462eaae60aa7a33dffd24f641847b52e098b69e4a5064c4b8bbe03cb",
    "linux-arm64": "1c05e4d4baaf1b4a9bf929e9c39b3512deb937c8d4c1bd52927499624018834f",
    "macos-x64": "722848fc0522a996bccca35ceef614b3926bb1ae97b145fe11bb08cf960b31f9",
    "macos-arm64": "d67b6006cb1144131910ce80d6cf52134f55b9c7da55648bd717df36b3f238d6",
}

BIN_DIR = Path.home() / ".local" / "bin"
SHIMS_DIR = Path.home() / ".local" / "share" / "mise" / "shims"


def current_platform() -> str:
    system = {"Linux": "linux", "Darwin": "macos"}[platform.system()]
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}[
        platform.machine().lower()
    ]
    return f"{system}-{machine}"


def main() -> int:
    name = current_platform()
    url = (
        "https://github.com/jdx/mise/releases/download/"
        f"{MISE_VERSION}/mise-{MISE_VERSION}-{name}.tar.gz"
    )
    print(f"downloading {url}")
    with urllib.request.urlopen(url) as response:
        archive = response.read()

    digest = hashlib.sha256(archive).hexdigest()
    if digest != SHA256[name]:
        print(f"checksum mismatch: expected {SHA256[name]}, got {digest}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / "mise.tar.gz"
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as tar:
            tar.extractall(tmp, filter="data")
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        (Path(tmp) / "mise" / "bin" / "mise").replace(BIN_DIR / "mise")

    # Later steps in the job find mise, and the tools it installs, on PATH.
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with Path(github_path).open("a") as file:
            file.write(f"{BIN_DIR}\n{SHIMS_DIR}\n")
    print(f"installed mise {MISE_VERSION} to {BIN_DIR / 'mise'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
