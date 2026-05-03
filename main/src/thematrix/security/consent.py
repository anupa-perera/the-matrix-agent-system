from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from thematrix.schemas import FileChangeConsent


class ActionKind(StrEnum):
    NORMAL_QUESTION = "normal_question"
    FILE_CHANGE = "file_change"
    SHELL_COMMAND = "shell_command"
    SENSITIVE_MEMORY_WRITE = "sensitive_memory_write"


class ConsentDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    BLOCK = "block"


class ConsentReview(BaseModel):
    decision: ConsentDecision
    reason: str


class ConsentPolicy:
    """Decides when the CLI should interrupt the user for approval."""

    def review(
        self,
        action: ActionKind,
        file_change_consent: FileChangeConsent = FileChangeConsent.ASK_EACH_TIME,
    ) -> ConsentReview:
        if action == ActionKind.NORMAL_QUESTION:
            return ConsentReview(
                decision=ConsentDecision.ALLOW,
                reason="Normal questions do not need extra approval after setup.",
            )
        if action == ActionKind.FILE_CHANGE:
            if file_change_consent == FileChangeConsent.ALLOW_ALWAYS:
                return ConsentReview(
                    decision=ConsentDecision.ALLOW,
                    reason="The user allowed this provider to make file changes.",
                )
            return ConsentReview(
                decision=ConsentDecision.ASK,
                reason="File changes need explicit approval.",
            )
        if action == ActionKind.SHELL_COMMAND:
            return ConsentReview(
                decision=ConsentDecision.ASK,
                reason="Shell commands can affect the local PC and need approval.",
            )
        if action == ActionKind.SENSITIVE_MEMORY_WRITE:
            return ConsentReview(
                decision=ConsentDecision.ASK,
                reason="Sensitive memory writes need user confirmation.",
            )
        return ConsentReview(decision=ConsentDecision.BLOCK, reason="Unknown action kind.")

