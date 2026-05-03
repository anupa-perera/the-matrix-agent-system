from thematrix.tools.shell import ShellDecision, ShellPolicy


def test_shell_policy_blocks_dangerous_delete() -> None:
    review = ShellPolicy().review("Remove-Item -Recurse -Force C:\\")

    assert review.decision == ShellDecision.BLOCK


def test_shell_policy_allows_git_status() -> None:
    review = ShellPolicy().review("git status -sb")

    assert review.decision == ShellDecision.ALLOW

