from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from thematrix.schemas import FileChangeConsent


class FileDecision(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    BLOCK = "block"


class FileOperation(StrEnum):
    READ = "read"
    WRITE = "write"


class FileReview(BaseModel):
    decision: FileDecision
    reason: str
    resolved_path: str


class FileToolResult(BaseModel):
    operation: FileOperation
    path: str
    purpose: str = ""
    decision: FileDecision
    reason: str
    executed: bool = False
    content: str = ""
    bytes_written: int = 0


class FilePolicy:
    """Guard file reads and writes to the current workspace boundary."""

    blocked_path_fragments = [
        ".env",
        ".pem",
        ".p12",
        ".pfx",
        ".ssh",
        "id_rsa",
        "id_dsa",
        "credentials",
        "secrets",
        "secret",
        "api_key",
        "runtime.sqlite",
    ]

    def __init__(self, root: Path):
        self.root = root.resolve()

    def review(self, path: str, operation: FileOperation) -> FileReview:
        resolved = self._resolve(path)
        if not resolved.is_relative_to(self.root):
            return FileReview(
                decision=FileDecision.BLOCK,
                reason="File path is outside the allowed workspace.",
                resolved_path=str(resolved),
            )
        lowered = str(resolved).lower()
        if any(fragment in lowered for fragment in self.blocked_path_fragments):
            return FileReview(
                decision=FileDecision.BLOCK,
                reason="File path looks sensitive and cannot be accessed by an agent.",
                resolved_path=str(resolved),
            )
        if operation == FileOperation.READ:
            return FileReview(
                decision=FileDecision.ALLOW,
                reason="File read is inside the allowed workspace.",
                resolved_path=str(resolved),
            )
        return FileReview(
            decision=FileDecision.APPROVAL_REQUIRED,
            reason="File writes change the local workspace and need approval.",
            resolved_path=str(resolved),
        )

    def _resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()


class FileExecutor:
    """Read and write files only after FilePolicy and consent checks."""

    def __init__(
        self,
        root: Path,
        approval_callback: Callable[[str, str, str], bool] | None = None,
        file_change_consent: FileChangeConsent = FileChangeConsent.ASK_EACH_TIME,
        max_read_chars: int = 8000,
    ):
        self.policy = FilePolicy(root)
        self.approval_callback = approval_callback
        self.file_change_consent = file_change_consent
        self.max_read_chars = max_read_chars

    def read(self, path: str, purpose: str = "") -> FileToolResult:
        review = self.policy.review(path, FileOperation.READ)
        if review.decision != FileDecision.ALLOW:
            return FileToolResult(
                operation=FileOperation.READ,
                path=review.resolved_path,
                purpose=purpose,
                decision=review.decision,
                reason=review.reason,
            )
        try:
            content = Path(review.resolved_path).read_text(encoding="utf-8")
        except Exception as exc:
            return FileToolResult(
                operation=FileOperation.READ,
                path=review.resolved_path,
                purpose=purpose,
                decision=review.decision,
                reason=f"File read failed: {exc}",
            )
        return FileToolResult(
            operation=FileOperation.READ,
            path=review.resolved_path,
            purpose=purpose,
            decision=review.decision,
            reason=review.reason,
            executed=True,
            content=self._truncate(content),
        )

    def write(self, path: str, content: str, purpose: str = "") -> FileToolResult:
        review = self.policy.review(path, FileOperation.WRITE)
        if review.decision == FileDecision.BLOCK:
            return FileToolResult(
                operation=FileOperation.WRITE,
                path=review.resolved_path,
                purpose=purpose,
                decision=review.decision,
                reason=review.reason,
            )

        approved = self.file_change_consent == FileChangeConsent.ALLOW_ALWAYS
        if not approved and self.approval_callback is not None:
            approved = self.approval_callback(review.resolved_path, review.reason, purpose)
        if not approved:
            return FileToolResult(
                operation=FileOperation.WRITE,
                path=review.resolved_path,
                purpose=purpose,
                decision=review.decision,
                reason=f"{review.reason} Approval was not granted.",
            )

        target = Path(review.resolved_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return FileToolResult(
            operation=FileOperation.WRITE,
            path=review.resolved_path,
            purpose=purpose,
            decision=review.decision,
            reason=review.reason,
            executed=True,
            bytes_written=len(content.encode("utf-8")),
        )

    def _truncate(self, value: str) -> str:
        if len(value) <= self.max_read_chars:
            return value
        return value[: self.max_read_chars] + "\n[truncated]"
