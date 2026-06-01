from __future__ import annotations

from datetime import UTC, datetime, timedelta

from thematrix.memory import RuntimeStore
from thematrix.operator import TheOperator
from thematrix.schemas import OperatorGoalStatus
from thematrix.tools import NotificationResult


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, message: str) -> NotificationResult:
        self.sent.append((title, message))
        return NotificationResult(ok=True, message=f"sent: {message}")


def test_operator_creates_recurring_notification_goal_from_request(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    goal = operator.create_from_request("Send me a notification about drinking water every 5 minutes")

    assert goal is not None
    assert goal.status == OperatorGoalStatus.PENDING
    assert goal.schedule is not None
    assert goal.schedule.interval_minutes == 5
    assert goal.capability == "notify_desktop"
    assert goal.payload["message"] == "drinking water"
    assert goal.next_run_at is None
    assert store.get_operator_goal(goal.goal_id) is not None


def test_operator_runs_due_goal_and_reschedules(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    notifier = FakeNotifier()
    operator = TheOperator(store, notifier=notifier)
    goal = operator.create_recurring_notification_goal(
        original_request="notify me every 5 minutes",
        message="stand up",
        interval_minutes=5,
    )
    due = goal.model_copy(update={"next_run_at": datetime.now(UTC) - timedelta(seconds=1)})
    store.upsert_operator_goal(due)

    assert operator.run_due_goals() == 1

    updated = store.get_operator_goal(goal.goal_id)
    assert updated is not None
    assert updated.last_result == "sent: stand up"
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(UTC)
    assert notifier.sent == [("The Matrix", "stand up")]
    assert store.list_operator_goal_runs(goal.goal_id)[0].message == "sent: stand up"


def test_operator_activates_pending_recurring_goal(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_from_request("Remind me about posture every 10 minutes")

    assert goal is not None
    activated = operator.activate_goal(goal.goal_id)

    assert activated.status == OperatorGoalStatus.ACTIVE
    assert activated.next_run_at is not None
    assert activated.next_run_at > datetime.now(UTC)


def test_operator_pause_resume_cancel_controls(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_notification_goal(
        original_request="notify me every 5 minutes",
        message="stretch",
        interval_minutes=5,
    )

    paused = operator.pause_goal(goal.goal_id)
    assert paused.status == OperatorGoalStatus.PAUSED

    resumed = operator.resume_goal(goal.goal_id)
    assert resumed.status == OperatorGoalStatus.ACTIVE

    canceled = operator.cancel_goal(goal.goal_id)
    assert canceled.status == OperatorGoalStatus.CANCELED
    assert canceled.next_run_at is None


def test_operator_tracks_one_shot_goal_completion_and_failure(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    goal = operator.create_one_shot_goal("Build a helper")
    completed = operator.complete_goal(goal.goal_id, "Mission completed.", {"run_id": "run-1"})
    failed_goal = operator.create_one_shot_goal("Fail later")
    failed = operator.fail_goal(failed_goal.goal_id, "Mission failed.")

    assert completed.status == OperatorGoalStatus.COMPLETED
    assert completed.last_result == "Mission completed."
    assert failed.status == OperatorGoalStatus.FAILED
    assert failed.failure_count == 1
    assert len(store.list_operator_goal_runs(goal.goal_id)) == 1
