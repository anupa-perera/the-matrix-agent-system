from thematrix.tools.shell import ShellDecision, ShellExecutor, ShellPolicy


def test_shell_policy_blocks_dangerous_delete() -> None:
    review = ShellPolicy().review("Remove-Item -Recurse -Force C:\\")

    assert review.decision == ShellDecision.BLOCK


def test_shell_policy_allows_git_status() -> None:
    review = ShellPolicy().review("git status -sb")

    assert review.decision == ShellDecision.ALLOW


def test_shell_policy_does_not_auto_allow_chained_commands() -> None:
    review = ShellPolicy().review("git status -sb && del /s secrets.txt")

    assert review.decision == ShellDecision.BLOCK


def test_shell_executor_runs_allowed_read_check_command() -> None:
    result = ShellExecutor().run("python --version", purpose="Check Python availability.")

    assert result.decision == ShellDecision.ALLOW
    assert result.executed
    assert result.exit_code == 0


def test_shell_executor_does_not_run_approval_command_without_approval() -> None:
    result = ShellExecutor(approval_callback=lambda command, reason, purpose: False).run(
        "pip install example-package",
        purpose="Install dependency.",
    )

    assert result.decision == ShellDecision.APPROVAL_REQUIRED
    assert not result.executed


def test_shell_policy_requires_approval_for_mutating_fix_flags() -> None:
    review = ShellPolicy().review("ruff check . --fix")

    assert review.decision == ShellDecision.APPROVAL_REQUIRED
