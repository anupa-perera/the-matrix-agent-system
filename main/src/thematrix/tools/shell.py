from __future__ import annotations

import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class ShellDecision(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    BLOCK = "block"


class ShellReview(BaseModel):
    decision: ShellDecision
    reason: str


class ShellCommandResult(BaseModel):
    command: str
    purpose: str = ""
    decision: ShellDecision
    reason: str
    executed: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class ShellPolicy:
    """Small v1 shell gate. Execution can be added behind this policy later."""

    blocked_fragments = [
        "remove-item -recurse -force",
        "remove-item -force",
        "del /s",
        "erase /s",
        "rmdir /s",
        "rm -rf",
        "format-volume",
        "set-executionpolicy",
        "reg delete",
        "credential",
        "secret",
        "token",
    ]
    approval_fragments = [
        "pip install",
        "npm install",
        "pnpm install",
        "yarn install",
        "curl ",
        "invoke-webrequest",
        "git push",
        "start-process",
        "--fix",
        "ruff format",
        "python -m ruff format",
        "git apply",
        "git commit",
        "git add",
    ]
    allowed_prefixes = [
        "git status",
        "git diff",
        "pytest",
        "python -m pytest",
        "py -m pytest",
        "python --version",
        "python -v",
        "py --version",
        "py -v",
        "ruff check",
        "python -m ruff",
        "dir",
        "ls",
        "get-childitem",
    ]
    command_separators = ["&&", "||", ";", "|", ">", "<", "`"]

    def review(self, command: str) -> ShellReview:
        normalized = " ".join(command.lower().split())
        if any(fragment in normalized for fragment in self.blocked_fragments):
            return ShellReview(
                decision=ShellDecision.BLOCK,
                reason="Command matches a blocked shell pattern.",
            )
        if any(separator in normalized for separator in self.command_separators):
            return ShellReview(
                decision=ShellDecision.APPROVAL_REQUIRED,
                reason="Chained or redirected commands need explicit user approval.",
            )
        if any(fragment in normalized for fragment in self.approval_fragments):
            return ShellReview(
                decision=ShellDecision.APPROVAL_REQUIRED,
                reason="Command needs explicit user approval.",
            )
        if any(normalized.startswith(prefix) for prefix in self.allowed_prefixes):
            return ShellReview(decision=ShellDecision.ALLOW, reason="Command is low-risk.")
        return ShellReview(
            decision=ShellDecision.APPROVAL_REQUIRED,
            reason="Command is not in the low-risk allow list.",
        )


class ShellExecutor:
    """Run shell commands only after ShellPolicy and optional user approval."""

    def __init__(
        self,
        policy: ShellPolicy | None = None,
        approval_callback: Callable[[str, str, str], bool] | None = None,
        cwd: Path | None = None,
        timeout_seconds: int = 30,
        max_output_chars: int = 4000,
    ):
        self.policy = policy or ShellPolicy()
        self.approval_callback = approval_callback
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def run(self, command: str, purpose: str = "") -> ShellCommandResult:
        review = self.policy.review(command)
        if review.decision == ShellDecision.BLOCK:
            return ShellCommandResult(
                command=command,
                purpose=purpose,
                decision=review.decision,
                reason=review.reason,
            )

        if review.decision == ShellDecision.APPROVAL_REQUIRED:
            approved = False
            if self.approval_callback is not None:
                approved = self.approval_callback(command, review.reason, purpose)
            if not approved:
                return ShellCommandResult(
                    command=command,
                    purpose=purpose,
                    decision=review.decision,
                    reason=f"{review.reason} Approval was not granted.",
                )

        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ShellCommandResult(
                command=command,
                purpose=purpose,
                decision=review.decision,
                reason=f"Command timed out after {self.timeout_seconds} seconds.",
                executed=True,
                stdout=self._truncate(exc.stdout or ""),
                stderr=self._truncate(exc.stderr or ""),
            )

        return ShellCommandResult(
            command=command,
            purpose=purpose,
            decision=review.decision,
            reason=review.reason,
            executed=True,
            exit_code=completed.returncode,
            stdout=self._truncate(completed.stdout),
            stderr=self._truncate(completed.stderr),
        )

    def _truncate(self, value: str) -> str:
        if len(value) <= self.max_output_chars:
            return value
        return value[: self.max_output_chars] + "\n[truncated]"
