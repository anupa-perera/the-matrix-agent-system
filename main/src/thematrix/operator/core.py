from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

from thematrix.memory import RuntimeStore
from thematrix.schemas import (
    OperatorGoal,
    OperatorGoalKind,
    OperatorGoalRun,
    OperatorGoalStatus,
    OperatorRunStatus,
    OperatorSchedule,
)
from thematrix.tools import DesktopNotifier, NotificationResult


class TheOperator:
    """Persistent goal owner and simple in-app scheduler."""

    def __init__(
        self,
        store: RuntimeStore,
        notifier: DesktopNotifier | None = None,
        tick_seconds: float = 30.0,
    ):
        self.store = store
        self.notifier = notifier or DesktopNotifier()
        self.tick_seconds = tick_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def create_from_request(self, request: str) -> OperatorGoal | None:
        parsed = self.parse_recurring_notification_request(request)
        if parsed is None:
            return None
        interval_minutes, message = parsed
        return self.create_recurring_notification_goal(
            original_request=request,
            message=message,
            interval_minutes=interval_minutes,
            activate=False,
        )

    def create_one_shot_goal(
        self,
        original_request: str,
        title: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> OperatorGoal:
        now = datetime.now(UTC)
        goal = OperatorGoal(
            original_request=original_request,
            title=title or self._title_for(original_request),
            kind=OperatorGoalKind.ONE_SHOT,
            status=OperatorGoalStatus.ACTIVE,
            capability="mission_run",
            payload=payload or {},
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_operator_goal(goal)
        return goal

    def create_recurring_notification_goal(
        self,
        original_request: str,
        message: str,
        interval_minutes: int,
        activate: bool = True,
    ) -> OperatorGoal:
        now = datetime.now(UTC)
        safe_interval = max(1, min(interval_minutes, 24 * 60))
        clean_message = " ".join(message.split())[:240] or "The Matrix reminder is due."
        status = OperatorGoalStatus.ACTIVE if activate else OperatorGoalStatus.PENDING
        goal = OperatorGoal(
            original_request=original_request,
            title=self._title_for(clean_message),
            kind=OperatorGoalKind.RECURRING_NOTIFICATION,
            status=status,
            schedule=OperatorSchedule(interval_minutes=safe_interval),
            next_run_at=now + timedelta(minutes=safe_interval) if activate else None,
            capability="notify_desktop",
            payload={"message": clean_message},
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_operator_goal(goal)
        return goal

    def parse_recurring_notification_request(self, request: str) -> tuple[int, str] | None:
        lowered = request.lower()
        if not any(term in lowered for term in ["notify", "notification", "remind", "reminder"]):
            return None
        if not any(term in lowered for term in ["every", "each", "recurring"]):
            return None
        interval = self._interval_minutes(lowered)
        if interval is None:
            return None
        message = self._notification_message(request)
        return interval, message

    def run_due_goals(self, now: datetime | None = None) -> int:
        due_at = now or datetime.now(UTC)
        count = 0
        for goal in self.store.list_due_operator_goals(due_at, limit=20):
            self.run_goal_now(goal.goal_id)
            count += 1
        return count

    def run_goal_now(self, goal_id: str) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        if goal.status not in {OperatorGoalStatus.ACTIVE, OperatorGoalStatus.PAUSED}:
            raise ValueError(f"Goal is not runnable: {goal.status.value}")
        if goal.kind == OperatorGoalKind.RECURRING_NOTIFICATION:
            result = self.notifier.send("The Matrix", str(goal.payload.get("message", "")))
        else:
            result = NotificationResult(ok=False, message="Unsupported Operator goal kind.")
        return self._record_result(goal, result)

    def pause_goal(self, goal_id: str) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        return self._set_status(goal, OperatorGoalStatus.PAUSED)

    def resume_goal(self, goal_id: str) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        if goal.schedule is not None and goal.next_run_at is None:
            goal.next_run_at = datetime.now(UTC) + timedelta(minutes=goal.schedule.interval_minutes)
        return self._set_status(goal, OperatorGoalStatus.ACTIVE)

    def activate_goal(self, goal_id: str) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        if goal.status != OperatorGoalStatus.PENDING:
            raise ValueError(f"Only pending goals can be activated: {goal.status.value}")
        next_run_at = None
        if goal.schedule is not None:
            next_run_at = datetime.now(UTC) + timedelta(minutes=goal.schedule.interval_minutes)
        updated = goal.model_copy(
            update={
                "status": OperatorGoalStatus.ACTIVE,
                "next_run_at": next_run_at,
                "updated_at": datetime.now(UTC),
            }
        )
        self.store.upsert_operator_goal(updated)
        return updated

    def update_recurring_notification_goal(
        self,
        goal_id: str,
        *,
        title: str,
        message: str,
        interval_minutes: int,
    ) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        if goal.kind != OperatorGoalKind.RECURRING_NOTIFICATION:
            raise ValueError("Only recurring notification goals can be edited here.")
        if interval_minutes < 1 or interval_minutes > 24 * 60:
            raise ValueError("Interval must be between 1 and 1440 minutes.")
        clean_message = " ".join(message.split())[:240]
        if not clean_message:
            raise ValueError("Enter a notification message.")
        clean_title = " ".join(title.split())[:80] or self._title_for(clean_message)
        now = datetime.now(UTC)
        next_run_at = goal.next_run_at
        if goal.status == OperatorGoalStatus.ACTIVE and goal.schedule is not None:
            next_run_at = now + timedelta(minutes=interval_minutes)
        updated = goal.model_copy(
            update={
                "title": clean_title,
                "schedule": OperatorSchedule(interval_minutes=interval_minutes),
                "next_run_at": next_run_at,
                "payload": {**goal.payload, "message": clean_message},
                "updated_at": now,
            }
        )
        self.store.upsert_operator_goal(updated)
        return updated

    def cancel_goal(self, goal_id: str) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        goal.next_run_at = None
        return self._set_status(goal, OperatorGoalStatus.CANCELED)

    def complete_goal(self, goal_id: str, message: str, details: dict[str, object] | None = None) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        now = datetime.now(UTC)
        run = OperatorGoalRun(
            goal_id=goal.goal_id,
            created_at=now,
            status=OperatorRunStatus.SUCCESS,
            message=message,
            details=details or {},
        )
        self.store.record_operator_goal_run(run)
        updated = goal.model_copy(
            update={
                "status": OperatorGoalStatus.COMPLETED,
                "last_run_at": now,
                "last_result": message,
                "updated_at": now,
            }
        )
        self.store.upsert_operator_goal(updated)
        return updated

    def fail_goal(self, goal_id: str, message: str, details: dict[str, object] | None = None) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        now = datetime.now(UTC)
        run = OperatorGoalRun(
            goal_id=goal.goal_id,
            created_at=now,
            status=OperatorRunStatus.FAILED,
            message=message,
            details=details or {},
        )
        self.store.record_operator_goal_run(run)
        updated = goal.model_copy(
            update={
                "status": OperatorGoalStatus.FAILED,
                "last_run_at": now,
                "last_result": message,
                "failure_count": goal.failure_count + 1,
                "updated_at": now,
            }
        )
        self.store.upsert_operator_goal(updated)
        return updated

    def _record_result(self, goal: OperatorGoal, result: NotificationResult) -> OperatorGoal:
        now = datetime.now(UTC)
        status = OperatorRunStatus.SUCCESS if result.ok else OperatorRunStatus.FAILED
        run = OperatorGoalRun(
            goal_id=goal.goal_id,
            created_at=now,
            status=status,
            message=result.message,
            details={"capability": goal.capability},
        )
        self.store.record_operator_goal_run(run)
        updates = {
            "last_run_at": now,
            "last_result": result.message,
            "updated_at": now,
        }
        if result.ok:
            updates["failure_count"] = 0
        else:
            updates["failure_count"] = goal.failure_count + 1
        if goal.status == OperatorGoalStatus.ACTIVE and goal.schedule is not None:
            updates["next_run_at"] = now + timedelta(minutes=goal.schedule.interval_minutes)
        updated = goal.model_copy(update=updates)
        self.store.upsert_operator_goal(updated)
        return updated

    def _set_status(self, goal: OperatorGoal, status: OperatorGoalStatus) -> OperatorGoal:
        updated = goal.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})
        self.store.upsert_operator_goal(updated)
        return updated

    def _require_goal(self, goal_id: str) -> OperatorGoal:
        goal = self.store.get_operator_goal(goal_id)
        if goal is None:
            raise ValueError(f"No Operator goal exists with id: {goal_id}")
        return goal

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.run_due_goals()
            except Exception:
                continue

    def _interval_minutes(self, lowered: str) -> int | None:
        match = re.search(r"\b(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", lowered)
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("hour") or unit.startswith("hr"):
            return amount * 60
        return amount

    def _notification_message(self, request: str) -> str:
        compact = " ".join(request.split())
        for prefix in [
            "send me a notification about",
            "send a notification about",
            "notify me about",
            "remind me to",
            "remind me about",
            "send me",
        ]:
            lowered = compact.lower()
            if prefix in lowered:
                start = lowered.index(prefix) + len(prefix)
                compact = compact[start:].strip(" .")
                break
        compact = re.sub(
            r"\b(every|each)\s+\d+\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b",
            "",
            compact,
            flags=re.IGNORECASE,
        )
        return " ".join(compact.split(" .:-")) or request

    def _title_for(self, message: str) -> str:
        title = message.strip(" .")
        if len(title) <= 64:
            return title or "Recurring notification"
        return f"{title[:61].rstrip()}..."
