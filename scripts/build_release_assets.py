from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ASSETS = ROOT / "release-assets"
STAGING = ROOT / ".release-build"

COMMON_FILES = [
    "pyproject.toml",
    "README.md",
]

WINDOWS_FILES = [
    "START_HERE_WINDOWS.txt",
    "Install The Matrix.cmd",
    "Start The Matrix.cmd",
    "install.cmd",
    "start.cmd",
    "install.ps1",
]

POSIX_FILES = [
    "START_HERE_MAC_LINUX.txt",
    "install.sh",
    "start.sh",
]


def main() -> None:
    version = _project_version()
    tag = _release_tag(version)
    repo = os.environ.get("GITHUB_REPOSITORY", "anupa-perera/the-matrix-agent-system")

    _reset_dir(ASSETS)
    _reset_dir(STAGING)

    windows_root = _stage_bundle(
        f"the-matrix-agent-system-{tag}-windows",
        COMMON_FILES + WINDOWS_FILES,
    )
    posix_root = _stage_bundle(
        f"the-matrix-agent-system-{tag}-mac-linux",
        COMMON_FILES + POSIX_FILES,
    )

    windows_zip = ASSETS / "the-matrix-agent-system-windows.zip"
    posix_tgz = ASSETS / "the-matrix-agent-system-mac-linux.tar.gz"

    _zip_dir(windows_root, windows_zip)
    _tar_gz_dir(posix_root, posix_tgz)

    notes = ASSETS / "RELEASE_NOTES.md"
    notes.write_text(_release_notes(tag, repo, windows_zip.name, posix_tgz.name), encoding="utf-8")

    checksum_targets = sorted(DIST.glob(f"*{version}*")) + sorted(
        path for path in ASSETS.glob("*") if path.name != "SHA256SUMS.txt"
    )
    _write_checksums(checksum_targets, ASSETS / "SHA256SUMS.txt")


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _release_tag(version: str) -> str:
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    if re.fullmatch(r"v\d+\.\d+\.\d+", ref_name):
        return ref_name
    return f"v{version}"


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _stage_bundle(folder_name: str, files: list[str]) -> Path:
    target = STAGING / folder_name
    target.mkdir(parents=True, exist_ok=True)

    for file_name in files:
        source = ROOT / file_name
        if source.exists():
            shutil.copy2(source, target / file_name)

    package_target = target / "main" / "src"
    shutil.copytree(
        ROOT / "main" / "src",
        package_target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return target


def _zip_dir(source: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent))


def _tar_gz_dir(source: Path, output: Path) -> None:
    with tarfile.open(output, "w:gz") as archive:
        archive.add(source, arcname=source.name)


def _write_checksums(paths: list[Path], output: Path) -> None:
    lines = []
    for path in paths:
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _release_notes(tag: str, repo: str, windows_zip: str, posix_tgz: str) -> str:
    version = tag.removeprefix("v")
    wheel = f"the_matrix_agent_system-{version}-py3-none-any.whl"
    source_zip = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
    raw_base = f"https://raw.githubusercontent.com/{repo}/{tag}"
    windows_command = (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        f'"$env:MATRIX_SOURCE=\'{source_zip}\'; '
        f"irm '{raw_base}/install.ps1' | iex\""
    )
    posix_command = (
        f"curl -fsSL {raw_base}/install.sh | "
        f'MATRIX_SOURCE="{source_zip}" sh'
    )
    return f"""## {tag}

This release packages the local-first Matrix-inspired agent framework for normal users and developers.

### What's New

- Recurring requests now start automatically: ask once and The Operator schedules and runs the goal without a manual activation step.
- New recurring missions: The Operator can run full agent tasks on a schedule, not just desktop notifications, with Neo's safety review on every run.
- Recurring requests understand more phrasing: numeric intervals, `hourly`, `daily`, `weekly`, `every morning`, and `set up a recurring task to ...`.
- Agents now work in an iterative tool loop, reacting to tool results with more tool calls instead of a single fixed pass.
- Agents gained `notify` and `schedule` tools, so they can send desktop notifications and set up recurring goals themselves mid-mission.
- Recurring goals auto-pause after repeated failures, and the number of open recurring goals is capped for safety.
- Recurring goal editing in the browser now covers recurring missions and intervals up to 7 days.
- Set the `operator_auto_activate` preference to false to return to review-first goal activation.

### Previously Added

- Browser missions open a live status cockpit, and Ask the Oracle streams read-only answers.
- Run Mission pauses for missing details and opens a clarification popup before launch.
- The dashboard Needs You queue collects clarifications, approvals, and Operator activations.
- Reusable agents can be run, edited in plain language, paused, resumed, and excluded from future reuse.
- Provider support covers OpenRouter, OpenAI, Anthropic, Gemini, Mistral, local models, and OpenAI Codex sign-in.

### Easiest Windows Install

1. Download `{windows_zip}` from the Assets section below.
2. Right-click the ZIP file and choose **Extract All**.
3. Open the extracted folder.
4. Open `START_HERE_WINDOWS.txt` if you want step-by-step help.
5. Double-click `Install The Matrix.cmd`.

Windows may show a security warning because this preview is not code-signed yet. Only continue if this release page is the GitHub repository you trust.

### Easiest macOS / Linux Install

1. Download `{posix_tgz}` from the Assets section below.
2. Extract it.
3. Open `START_HERE_MAC_LINUX.txt`.
4. Run `sh install.sh` from the extracted folder.

### One-Line Install

Windows PowerShell:

```powershell
{windows_command}
```

macOS / Linux:

```sh
{posix_command}
```

### For Developers

Install the wheel from Assets, or use:

```sh
uv tool install ./{wheel}
```

### Verification

`SHA256SUMS.txt` contains checksums for the release files. The installer stores secrets through Keymaker and does not write API keys to Obsidian, SQLite, or logs.
"""


if __name__ == "__main__":
    main()
