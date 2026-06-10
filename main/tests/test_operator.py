from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import sleep

from thematrix.memory import RuntimeStore
from thematrix.operator import TheOperator
from thematrix.schemas import OperatorGoal, OperatorGoalKind, OperatorGoalStatus
from thematrix.tools import NotificationResult


class FakeNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, message: str) -> NotificationResult:
        self.sent.append((title, message))
        if self.ok:
            return NotificationResult(ok=True, message=f"sent: {message}")
        return NotificationResult(ok=False, message=f"failed: {message}")


def test_operator_creates_active_recurring_notification_goal_from_request(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    goal = operator.create_from_request("Send me a notification about drinking water every 5 minutes")

    assert goal is not None
    assert goal.kind == OperatorGoalKind.RECURRING_NOTIFICATION
    assert goal.status == OperatorGoalStatus.ACTIVE
    assert goal.schedule is not None
    assert goal.schedule.interval_minutes == 5
    assert goal.capability == "notify_desktop"
    assert goal.payload["message"] == "drinking water"
    assert goal.next_run_at is not None
    assert store.get_operator_goal(goal.goal_id) is not None


def test_operator_creates_recurring_mission_goal_from_request(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    goal = operator.create_from_request("Organize my downloads folder every hour")

    assert goal is not None
    assert goal.kind == OperatorGoalKind.RECURRING_MISSION
    assert goal.status == OperatorGoalStatus.ACTIVE
    assert goal.capability == "mission_run"
    assert goal.schedule is not None
    assert goal.schedule.interval_minutes == 60
    assert goal.payload["mission_request"] == "Organize my downloads folder"
    assert goal.next_run_at is not None


def test_operator_parses_word_intervals_and_prefixes(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    daily = operator.parse_recurring_request("Set up a recurring task to back up my notes daily")
    assert daily is not None
    assert daily.kind == OperatorGoalKind.RECURRING_MISSION
    assert daily.interval_minutes == 24 * 60
    assert daily.action == "back up my notes"

    morning = operator.parse_recurring_request("Remind me every morning to review my plan")
    assert morning is not None
    assert morning.kind == OperatorGoalKind.RECURRING_NOTIFICATION
    assert morning.cron == "0 9 * * *"

    assert operator.parse_recurring_request("Review every file in this folder") is None
    assert operator.parse_recurring_request("Build a planning agent") is None


def test_operator_parses_time_of_day_requests_into_cron(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    weekday = operator.parse_recurring_request("Check my email every weekday at 9am")
    assert weekday is not None
    assert weekday.kind == OperatorGoalKind.RECURRING_MISSION
    assert weekday.cron == "0 9 * * 1-5"
    assert weekday.action == "Check my email"

    evening = operator.parse_recurring_request("Remind me to stretch daily at 8:30pm")
    assert evening is not None
    assert evening.kind == OperatorGoalKind.RECURRING_NOTIFICATION
    assert evening.cron == "30 20 * * *"

    monday = operator.parse_recurring_request("Summarize my notes every monday")
    assert monday is not None
    assert monday.cron == "0 9 * * 1"


def test_operator_creates_cron_goal_with_future_next_run(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    goal = operator.create_from_request("Back up my vault every weekday at 9am")

    assert goal is not None
    assert goal.kind == OperatorGoalKind.RECURRING_MISSION
    assert goal.status == OperatorGoalStatus.ACTIVE
    assert goal.schedule is not None
    assert goal.schedule.cron == "0 9 * * 1-5"
    assert goal.next_run_at is not None
    assert goal.next_run_at > datetime.now(UTC)
    local_next = goal.next_run_at.astimezone()
    assert (local_next.hour, local_next.minute) == (9, 0)
    assert local_next.weekday() < 5


def test_operator_cron_goal_reschedules_on_cron_after_run(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    notifier = FakeNotifier()
    operator = TheOperator(store, notifier=notifier)
    goal = operator.create_recurring_notification_goal(
        original_request="remind me daily at 9am",
        message="stand up",
        interval_minutes=0,
        cron="0 9 * * *",
    )
    due = goal.model_copy(update={"next_run_at": datetime.now(UTC) - timedelta(seconds=1)})
    store.upsert_operator_goal(due)

    assert operator.run_due_goals() == 1

    updated = store.get_operator_goal(goal.goal_id)
    assert updated is not None
    assert updated.next_run_at is not None
    local_next = updated.next_run_at.astimezone()
    assert (local_next.hour, local_next.minute) == (9, 0)
    assert updated.next_run_at > datetime.now(UTC)


def test_operator_rejects_invalid_cron_on_creation(tmp_path) -> None:
    import pytest

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())

    with pytest.raises(ValueError):
        operator.create_recurring_notification_goal(
            original_request="bad cron",
            message="stand up",
            interval_minutes=0,
            cron="61 * * * *",
        )


def test_operator_auto_activation_respects_preference(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    store.set_preference("operator_auto_activate", False)
    operator = TheOperator(store, notifier=FakeNotifier())

    goal = operator.create_from_request("Remind me about posture every 10 minutes")

    assert goal is not None
    assert goal.status == OperatorGoalStatus.PENDING
    assert goal.next_run_at is None


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


def test_operator_runs_recurring_mission_through_launcher(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    launched: list[OperatorGoal] = []

    def launcher(goal: OperatorGoal) -> NotificationResult:
        launched.append(goal)
        return NotificationResult(ok=True, message="Recurring mission started.")

    operator = TheOperator(store, notifier=FakeNotifier(), mission_launcher=launcher)
    goal = operator.create_recurring_mission_goal(
        original_request="Check disk space every 30 minutes",
        mission_request="Check disk space",
        interval_minutes=30,
    )
    due = goal.model_copy(update={"next_run_at": datetime.now(UTC) - timedelta(seconds=1)})
    store.upsert_operator_goal(due)

    assert operator.run_due_goals() == 1

    assert len(launched) == 1
    assert launched[0].payload["mission_request"] == "Check disk space"
    updated = store.get_operator_goal(goal.goal_id)
    assert updated is not None
    assert updated.status == OperatorGoalStatus.ACTIVE
    assert updated.last_result == "Recurring mission started."
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(UTC)


def test_operator_recurring_mission_without_launcher_fails_softly(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_mission_goal(
        original_request="Check disk space every 30 minutes",
        mission_request="Check disk space",
        interval_minutes=30,
    )

    updated = operator.run_goal_now(goal.goal_id)

    assert updated.failure_count == 1
    assert "app runtime" in updated.last_result
    assert updated.status == OperatorGoalStatus.ACTIVE


def test_operator_pauses_goal_after_repeated_failures(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier(ok=False))
    goal = operator.create_recurring_notification_goal(
        original_request="notify me every 5 minutes",
        message="stand up",
        interval_minutes=5,
    )
    seeded = goal.model_copy(update={"failure_count": 4})
    store.upsert_operator_goal(seeded)

    updated = operator.run_goal_now(goal.goal_id)

    assert updated.failure_count == 5
    assert updated.status == OperatorGoalStatus.PAUSED
    assert updated.next_run_at is None
    assert "paused after 5 consecutive failures" in updated.last_result


def test_operator_note_goal_run_keeps_recurring_goal_active(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_mission_goal(
        original_request="Check disk space every 30 minutes",
        mission_request="Check disk space",
        interval_minutes=30,
    )

    updated = operator.note_goal_run(goal.goal_id, ok=True, message="Recurring mission completed.")

    assert updated.status == OperatorGoalStatus.ACTIVE
    assert updated.last_result == "Recurring mission completed."
    assert updated.failure_count == 0
    runs = store.list_operator_goal_runs(goal.goal_id)
    assert len(runs) == 1


def test_operator_activates_pending_recurring_goal(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_notification_goal(
        original_request="Remind me about posture every 10 minutes",
        message="posture",
        interval_minutes=10,
        activate=False,
    )

    assert goal.status == OperatorGoalStatus.PENDING
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


def test_operator_updates_recurring_notification_goal(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_notification_goal(
        original_request="notify me every 5 minutes",
        message="stretch",
        interval_minutes=5,
    )

    updated = operator.update_recurring_goal(
        goal.goal_id,
        title="Movement reminder",
        message="Stand and stretch",
        interval_minutes=20,
    )

    assert updated.title == "Movement reminder"
    assert updated.payload["message"] == "Stand and stretch"
    assert updated.schedule is not None
    assert updated.schedule.interval_minutes == 20
    assert updated.status == OperatorGoalStatus.ACTIVE
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(UTC)
    stored = store.get_operator_goal(goal.goal_id)
    assert stored is not None
    assert stored.payload["message"] == "Stand and stretch"


def test_operator_updates_recurring_mission_goal(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_mission_goal(
        original_request="Check disk space every 30 minutes",
        mission_request="Check disk space",
        interval_minutes=30,
    )

    updated = operator.update_recurring_goal(
        goal.goal_id,
        title="Disk check",
        message="Check disk space and clean temp files",
        interval_minutes=60,
    )

    assert updated.payload["mission_request"] == "Check disk space and clean temp files"
    assert updated.schedule is not None
    assert updated.schedule.interval_minutes == 60


def test_operator_update_keeps_pending_goal_pending(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    goal = operator.create_recurring_notification_goal(
        original_request="Remind me about posture every 10 minutes",
        message="posture",
        interval_minutes=10,
        activate=False,
    )

    updated = operator.update_recurring_goal(
        goal.goal_id,
        title="Posture reminder",
        message="Check posture",
        interval_minutes=15,
    )

    assert updated.status == OperatorGoalStatus.PENDING
    assert updated.next_run_at is None
    assert updated.schedule is not None
    assert updated.schedule.interval_minutes == 15


def test_operator_caps_open_recurring_goals(tmp_path) -> None:
    import pytest

    from thematrix.operator.core import MAX_OPEN_RECURRING_GOALS

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    operator = TheOperator(store, notifier=FakeNotifier())
    for index in range(MAX_OPEN_RECURRING_GOALS):
        operator.create_recurring_notification_goal(
            original_request=f"notify me every 5 minutes #{index}",
            message=f"reminder {index}",
            interval_minutes=5,
        )

    with pytest.raises(ValueError, match="recurring goals"):
        operator.create_recurring_mission_goal(
            original_request="One goal too many every hour",
            mission_request="One goal too many",
            interval_minutes=60,
        )

    # Intake degrades to a one-shot mission instead of erroring out.
    assert operator.create_from_request("Organize my notes every hour") is None


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


def test_operator_scheduler_logs_store_failures(tmp_path, caplog) -> None:
    class FailingStore(RuntimeStore):
        def list_due_operator_goals(self, now, limit: int = 20):
            raise RuntimeError("store unavailable")

    caplog.set_level("ERROR")
    operator = TheOperator(
        FailingStore(tmp_path / "runtime.sqlite"),
        notifier=FakeNotifier(),
        tick_seconds=0.01,
    )

    operator.start()
    sleep(0.05)
    operator.stop()

    assert "Operator scheduler failed" in caplog.text
    assert "store unavailable" in caplog.text
