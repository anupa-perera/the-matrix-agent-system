from __future__ import annotations

import pytest

from thematrix.schemas import FileChangeConsent
from thematrix.tools import FileDecision, FileExecutor, FileOperation, FilePolicy


def test_file_policy_allows_workspace_reads(tmp_path) -> None:
    review = FilePolicy(tmp_path).review("README.md", FileOperation.READ)

    assert review.decision == FileDecision.ALLOW


def test_file_policy_blocks_path_escape(tmp_path) -> None:
    review = FilePolicy(tmp_path).review("../outside.txt", FileOperation.READ)

    assert review.decision == FileDecision.BLOCK


def test_file_policy_blocks_sensitive_paths(tmp_path) -> None:
    review = FilePolicy(tmp_path).review(".env", FileOperation.READ)

    assert review.decision == FileDecision.BLOCK


def test_file_policy_does_not_block_secret_as_plain_substring(tmp_path) -> None:
    review = FilePolicy(tmp_path).review("docs/secret_notes.md", FileOperation.READ)

    assert review.decision == FileDecision.ALLOW


def test_file_policy_blocks_sensitive_components_and_suffixes(tmp_path) -> None:
    policy = FilePolicy(tmp_path)

    assert policy.review("docs/secret.md", FileOperation.READ).decision == FileDecision.BLOCK
    assert policy.review("certs/local.pem", FileOperation.READ).decision == FileDecision.BLOCK
    assert policy.review("config/.env.local", FileOperation.READ).decision == FileDecision.BLOCK


def test_file_policy_blocks_symlinked_components(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    review = FilePolicy(tmp_path).review("linked/notes.md", FileOperation.READ)

    assert review.decision == FileDecision.BLOCK


def test_file_executor_reads_safe_file(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")

    result = FileExecutor(tmp_path).read("notes.md", purpose="Read notes.")

    assert result.executed
    assert result.content == "hello"


def test_file_executor_does_not_write_without_approval(tmp_path) -> None:
    result = FileExecutor(
        tmp_path,
        approval_callback=lambda path, reason, purpose: False,
    ).write("notes.md", "hello", purpose="Create notes.")

    assert result.decision == FileDecision.APPROVAL_REQUIRED
    assert not result.executed
    assert not (tmp_path / "notes.md").exists()


def test_file_executor_writes_when_consent_allows(tmp_path) -> None:
    result = FileExecutor(
        tmp_path,
        file_change_consent=FileChangeConsent.ALLOW_ALWAYS,
    ).write("notes.md", "hello", purpose="Create notes.")

    assert result.executed
    assert result.bytes_written == 5
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hello"
