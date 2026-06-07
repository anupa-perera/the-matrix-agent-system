from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

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
    """Small shell gate for agent-requested commands."""

    shell_control_markers: ClassVar[tuple[str, ...]] = (
        "$(",
        "${",
        "`",
        ";",
        "|",
        "&",
        "<",
        ">",
        "\n",
        "\r",
    )
    allowed_programs: ClassVar[tuple[str, ...]] = (
        "git",
        "pytest",
        "python",
        "py",
        "ruff",
        "ls",
    )
    install_programs: ClassVar[tuple[str, ...]] = ("pip", "npm", "pnpm", "yarn")
    destructive_programs: ClassVar[tuple[str, ...]] = (
        "del",
        "erase",
        "format-volume",
        "rmdir",
        "rm",
        "remove-item",
        "set-executionpolicy",
    )
    network_programs: ClassVar[tuple[str, ...]] = ("curl", "invoke-webrequest")
    approval_git_subcommands: ClassVar[tuple[str, ...]] = (
        "add",
        "apply",
        "commit",
        "push",
    )

    def review(self, command: str) -> ShellReview:
        lowered = command.casefold()
        if self._is_raw_blocked(lowered):
            return ShellReview(
                decision=ShellDecision.BLOCK,
                reason="Command matches a blocked destructive shell pattern.",
            )
        args = self.tokenize(command)
        if args is None:
            return ShellReview(
                decision=ShellDecision.APPROVAL_REQUIRED,
                reason="Command could not be parsed safely.",
            )

        if self._is_blocked(args):
            return ShellReview(
                decision=ShellDecision.BLOCK,
                reason="Command matches a blocked destructive shell pattern.",
            )
        if any(marker in command for marker in self.shell_control_markers):
            return ShellReview(
                decision=ShellDecision.APPROVAL_REQUIRED,
                reason="Shell control syntax needs explicit user approval.",
            )
        if self._needs_approval(args, lowered):
            return ShellReview(
                decision=ShellDecision.APPROVAL_REQUIRED,
                reason="Command needs explicit user approval.",
            )
        if self._is_low_risk(args):
            return ShellReview(decision=ShellDecision.ALLOW, reason="Command is low-risk.")
        return ShellReview(
            decision=ShellDecision.APPROVAL_REQUIRED,
            reason="Command is not in the low-risk allow list.",
        )

    def tokenize(self, command: str) -> list[str] | None:
        try:
            return shlex.split(command)
        except ValueError:
            return None

    def _program(self, args: list[str]) -> str:
        return args[0].casefold() if args else ""

    def _is_raw_blocked(self, lowered: str) -> bool:
        if "remove-item" in lowered and ("-recurse" in lowered or "-force" in lowered):
            return True
        blocked_fragments = (
            "del /s",
            "erase /s",
            "rmdir /s",
            "rm -rf",
            "rm -fr",
            "format-volume",
            "set-executionpolicy",
            "reg delete",
        )
        return any(fragment in lowered for fragment in blocked_fragments)

    def _is_blocked(self, args: list[str]) -> bool:
        if not args:
            return True
        program = self._program(args)
        lowered_args = [arg.casefold() for arg in args]
        if program in self.destructive_programs:
            return True
        if any(arg in self.destructive_programs for arg in lowered_args):
            return True
        if program == "reg" and len(lowered_args) > 1 and lowered_args[1] == "delete":
            return True
        if any(
            arg == "reg" and index + 1 < len(lowered_args) and lowered_args[index + 1] == "delete"
            for index, arg in enumerate(lowered_args)
        ):
            return True
        if program == "git" and len(lowered_args) > 1 and lowered_args[1] == "clean":
            return True
        return False

    def _needs_approval(self, args: list[str], lowered: str) -> bool:
        program = self._program(args)
        lowered_args = [arg.casefold() for arg in args]
        if program in self.install_programs or program in self.network_programs:
            return True
        if program == "start-process":
            return True
        if "--fix" in lowered_args:
            return True
        if program == "git" and len(lowered_args) > 1:
            return lowered_args[1] in self.approval_git_subcommands
        if program == "ruff" and len(lowered_args) > 1:
            return lowered_args[1] == "format"
        if program in {"python", "py"} and len(lowered_args) > 3:
            return lowered_args[1:3] == ["-m", "ruff"] and lowered_args[3] == "format"
        if "pip install" in lowered or "npm install" in lowered:
            return True
        return False

    def _is_low_risk(self, args: list[str]) -> bool:
        if not args:
            return False
        program = self._program(args)
        lowered_args = [arg.casefold() for arg in args]
        if program not in self.allowed_programs:
            return False
        if program == "git":
            return len(lowered_args) > 1 and lowered_args[1] in {"status", "diff"}
        if program == "pytest":
            return True
        if program in {"python", "py"}:
            if len(lowered_args) == 2 and lowered_args[1] in {"--version", "-v"}:
                return True
            return len(lowered_args) >= 3 and lowered_args[1:3] in (
                ["-m", "pytest"],
                ["-m", "ruff"],
            )
        if program == "ruff":
            return len(lowered_args) > 1 and lowered_args[1] == "check"
        return program == "ls"


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
        args = self.policy.tokenize(command)
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

        if args is None:
            return ShellCommandResult(
                command=command,
                purpose=purpose,
                decision=review.decision,
                reason="Command could not be parsed safely.",
            )

        try:
            completed = subprocess.run(
                args,
                cwd=self.cwd,
                shell=False,
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
        except OSError as exc:
            return ShellCommandResult(
                command=command,
                purpose=purpose,
                decision=review.decision,
                reason=f"Command could not start: {exc}",
                executed=False,
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
