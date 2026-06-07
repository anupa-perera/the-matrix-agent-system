from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

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

    blocked_names: ClassVar[tuple[str, ...]] = (
        ".env",
        ".ssh",
        "id_rsa",
        "id_dsa",
        "credentials",
        "secrets",
        "secret",
        "api_key",
        "runtime.sqlite",
    )
    blocked_suffixes: ClassVar[tuple[str, ...]] = (".pem", ".p12", ".pfx")

    def __init__(self, root: Path):
        self.root = root.resolve()

    def review(self, path: str, operation: FileOperation) -> FileReview:
        candidate = self._candidate(path)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            return FileReview(
                decision=FileDecision.BLOCK,
                reason="File path is outside the allowed workspace.",
                resolved_path=str(resolved),
            )
        if self._has_symlink_component(candidate):
            return FileReview(
                decision=FileDecision.BLOCK,
                reason="File path crosses a symlink and cannot be accessed by an agent.",
                resolved_path=str(resolved),
            )
        if self._looks_sensitive(candidate):
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

    def _candidate(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.absolute()

    def _has_symlink_component(self, candidate: Path) -> bool:
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            return False
        current = self.root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                continue
            current = current / part
            if current.is_symlink():
                return True
        return False

    def _looks_sensitive(self, candidate: Path) -> bool:
        try:
            parts = candidate.relative_to(self.root).parts
        except ValueError:
            parts = candidate.parts
        for part in parts:
            name = part.casefold()
            suffixes = tuple(suffix.casefold() for suffix in Path(name).suffixes)
            stem = Path(name).stem.casefold()
            if name in self.blocked_names or stem in self.blocked_names:
                return True
            if any(suffix in self.blocked_suffixes for suffix in suffixes):
                return True
        return False


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
