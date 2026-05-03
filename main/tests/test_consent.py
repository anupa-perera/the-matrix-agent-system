from thematrix.schemas import FileChangeConsent
from thematrix.security import ActionKind, ConsentDecision, ConsentPolicy


def test_normal_questions_do_not_require_extra_consent() -> None:
    review = ConsentPolicy().review(ActionKind.NORMAL_QUESTION)

    assert review.decision == ConsentDecision.ALLOW


def test_file_changes_ask_by_default() -> None:
    review = ConsentPolicy().review(ActionKind.FILE_CHANGE)

    assert review.decision == ConsentDecision.ASK


def test_file_changes_can_be_allowed_always() -> None:
    review = ConsentPolicy().review(
        ActionKind.FILE_CHANGE,
        file_change_consent=FileChangeConsent.ALLOW_ALWAYS,
    )

    assert review.decision == ConsentDecision.ALLOW

