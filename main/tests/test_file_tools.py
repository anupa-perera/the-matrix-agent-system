from __future__ import annotations

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
