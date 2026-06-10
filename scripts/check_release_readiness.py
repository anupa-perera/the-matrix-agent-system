from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ASSETS = ROOT / "release-assets"
SITE_INDEX = ROOT / "site" / "index.html"
INSTALLER = ROOT / "install.ps1"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def main() -> None:
    version = _project_version()
    tag = f"v{version}"
    problems: list[str] = []

    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"pyproject.toml version must be semver, got `{version}`.")

    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    if ref_name.startswith("v") and ref_name != tag:
        problems.append(f"tag `{ref_name}` does not match pyproject version `{tag}`.")

    _check_dist(version, problems)
    _check_release_assets(version, tag, problems)
    _check_pages(problems)
    _check_windows_installer(problems)
    _check_release_workflow(problems)

    if problems:
        for problem in problems:
            print(f"release readiness: {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(f"release readiness: ok for {tag}")


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _check_dist(version: str, problems: list[str]) -> None:
    expected = {
        f"the_matrix_agent_system-{version}-py3-none-any.whl",
        f"the_matrix_agent_system-{version}.tar.gz",
    }
    existing = {path.name for path in DIST.glob("*") if path.is_file()}
    missing = expected - existing
    if missing:
        problems.append(f"dist is missing current package files: {', '.join(sorted(missing))}.")


def _check_release_assets(version: str, tag: str, problems: list[str]) -> None:
    expected = {
        "RELEASE_NOTES.md",
        "SHA256SUMS.txt",
        "the-matrix-agent-system-mac-linux.tar.gz",
        "the-matrix-agent-system-windows.zip",
    }
    existing = {path.name for path in ASSETS.glob("*") if path.is_file()}
    missing = expected - existing
    if missing:
        problems.append(f"release-assets is missing: {', '.join(sorted(missing))}.")
        return

    notes = (ASSETS / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    if f"## {tag}" not in notes:
        problems.append(f"release notes must use `{tag}` as the heading.")
    if "## The Matrix Agent System " in notes:
        problems.append("release notes heading should not prefix the version with the app name.")
    if f"the_matrix_agent_system-{version}-py3-none-any.whl" not in notes:
        problems.append("release notes do not reference the current wheel filename.")

    checksums = (ASSETS / "SHA256SUMS.txt").read_text(encoding="utf-8")
    expected_checksum_entries = (expected - {"SHA256SUMS.txt"}) | {
        f"the_matrix_agent_system-{version}-py3-none-any.whl",
        f"the_matrix_agent_system-{version}.tar.gz",
    }
    for name in expected_checksum_entries:
        if name not in checksums:
            problems.append(f"SHA256SUMS.txt is missing `{name}`.")
    stale_package = re.search(
        rf"the_matrix_agent_system-(?!{re.escape(version)}\b)\d+\.\d+\.\d+",
        checksums,
    )
    if stale_package:
        problems.append(f"SHA256SUMS.txt includes stale package `{stale_package.group(0)}`.")


def _check_pages(problems: list[str]) -> None:
    html = SITE_INDEX.read_text(encoding="utf-8")
    required = [
        "releases/latest/download/the-matrix-agent-system-windows.zip",
        "releases/latest/download/the-matrix-agent-system-mac-linux.tar.gz",
        "local-first crew of AI agents",
        "any AI model you choose",
        "Choose Your AI",
    ]
    for text in required:
        if text not in html:
            problems.append(f"site/index.html must include `{text}`.")


def _check_windows_installer(problems: list[str]) -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    required = [
        "function New-MatrixIcon",
        "the-matrix.ico",
        "$shortcut.IconLocation",
    ]
    for text in required:
        if text not in installer:
            problems.append(f"install.ps1 must include `{text}`.")


def _check_release_workflow(problems: list[str]) -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    if '--title "${{ github.ref_name }}"' not in workflow:
        problems.append("release workflow title must be exactly the pushed version tag.")
    if "--title \"The Matrix Agent System " in workflow:
        problems.append("release workflow must not prefix the release title with the app name.")


if __name__ == "__main__":
    main()
