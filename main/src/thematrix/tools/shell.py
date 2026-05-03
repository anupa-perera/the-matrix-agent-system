from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ShellDecision(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    BLOCK = "block"


class ShellReview(BaseModel):
    decision: ShellDecision
    reason: str


class ShellPolicy:
    """Small v1 shell gate. Execution can be added behind this policy later."""

    blocked_fragments = [
        "remove-item -recurse -force",
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
    ]
    allowed_prefixes = [
        "git status",
        "git diff",
        "pytest",
        "python -m pytest",
        "py -m pytest",
        "ruff check",
        "python -m ruff",
        "dir",
        "ls",
        "get-childitem",
    ]

    def review(self, command: str) -> ShellReview:
        normalized = " ".join(command.lower().split())
        if any(fragment in normalized for fragment in self.blocked_fragments):
            return ShellReview(
                decision=ShellDecision.BLOCK,
                reason="Command matches a blocked shell pattern.",
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

