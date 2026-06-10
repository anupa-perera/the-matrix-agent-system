from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
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


logger = logging.getLogger(__name__)

MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_CONSECUTIVE_FAILURES = 5
MAX_OPEN_RECURRING_GOALS = 25

_NOTIFY_TERMS = ("notify", "notification", "remind", "reminder", "alert me")
_RECURRENCE_CUES = ("every", "each", "recurring", "hourly", "daily", "weekly", "nightly")
_UNIT_MINUTES = {
    "minute": 1,
    "minutes": 1,
    "min": 1,
    "mins": 1,
    "hour": 60,
    "hours": 60,
    "hr": 60,
    "hrs": 60,
    "day": 24 * 60,
    "days": 24 * 60,
    "week": 7 * 24 * 60,
    "weeks": 7 * 24 * 60,
}
_WORD_INTERVALS = (
    ("every minute", 1),
    ("each minute", 1),
    ("every hour", 60),
    ("each hour", 60),
    ("hourly", 60),
    ("every morning", 24 * 60),
    ("every evening", 24 * 60),
    ("every night", 24 * 60),
    ("every day", 24 * 60),
    ("each day", 24 * 60),
    ("daily", 24 * 60),
    ("nightly", 24 * 60),
    ("every week", 7 * 24 * 60),
    ("each week", 7 * 24 * 60),
    ("weekly", 7 * 24 * 60),
)
_NUMERIC_INTERVAL_PATTERN = re.compile(
    r"\b(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs|day|days|week|weeks)\b"
)


@dataclass(frozen=True)
class ParsedRecurrence:
    kind: OperatorGoalKind
    interval_minutes: int
    action: str


class TheOperator:
    """Persistent goal owner and simple in-app scheduler."""

    def __init__(
        self,
        store: RuntimeStore,
        notifier: DesktopNotifier | None = None,
        tick_seconds: float = 30.0,
        mission_launcher: Callable[[OperatorGoal], NotificationResult] | None = None,
    ):
        self.store = store
        self.notifier = notifier or DesktopNotifier()
        self.tick_seconds = tick_seconds
        self.mission_launcher = mission_launcher
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

    def attach_mission_launcher(
        self,
        launcher: Callable[[OperatorGoal], NotificationResult],
    ) -> None:
        self.mission_launcher = launcher

    def create_from_request(self, request: str) -> OperatorGoal | None:
        parsed = self.parse_recurring_request(request)
        if parsed is None:
            return None
        try:
            self._ensure_recurring_capacity()
        except ValueError:
            logger.warning(
                "Recurring goal limit reached; running the request as a one-shot mission."
            )
            return None
        activate = self._auto_activate_enabled()
        if parsed.kind == OperatorGoalKind.RECURRING_NOTIFICATION:
            return self.create_recurring_notification_goal(
                original_request=request,
                message=parsed.action,
                interval_minutes=parsed.interval_minutes,
                activate=activate,
            )
        return self.create_recurring_mission_goal(
            original_request=request,
            mission_request=parsed.action,
            interval_minutes=parsed.interval_minutes,
            activate=activate,
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
        self._ensure_recurring_capacity()
        now = datetime.now(UTC)
        safe_interval = max(1, min(interval_minutes, MAX_INTERVAL_MINUTES))
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

    def create_recurring_mission_goal(
        self,
        original_request: str,
        mission_request: str,
        interval_minutes: int,
        activate: bool = True,
    ) -> OperatorGoal:
        self._ensure_recurring_capacity()
        now = datetime.now(UTC)
        safe_interval = max(1, min(interval_minutes, MAX_INTERVAL_MINUTES))
        clean_request = " ".join(mission_request.split())[:600] or original_request
        status = OperatorGoalStatus.ACTIVE if activate else OperatorGoalStatus.PENDING
        goal = OperatorGoal(
            original_request=original_request,
            title=self._title_for(clean_request),
            kind=OperatorGoalKind.RECURRING_MISSION,
            status=status,
            schedule=OperatorSchedule(interval_minutes=safe_interval),
            next_run_at=now + timedelta(minutes=safe_interval) if activate else None,
            capability="mission_run",
            payload={"mission_request": clean_request},
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_operator_goal(goal)
        return goal

    def parse_recurring_request(self, request: str) -> ParsedRecurrence | None:
        lowered = " ".join(request.lower().split())
        if not any(cue in lowered for cue in _RECURRENCE_CUES):
            return None
        interval = self._interval_minutes(lowered)
        if interval is None:
            return None
        if any(term in lowered for term in _NOTIFY_TERMS):
            return ParsedRecurrence(
                kind=OperatorGoalKind.RECURRING_NOTIFICATION,
                interval_minutes=interval,
                action=self._notification_message(request),
            )
        return ParsedRecurrence(
            kind=OperatorGoalKind.RECURRING_MISSION,
            interval_minutes=interval,
            action=self._mission_request(request),
        )

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
        elif goal.kind == OperatorGoalKind.RECURRING_MISSION:
            result = self._launch_recurring_mission(goal)
        else:
            result = NotificationResult(ok=False, message="Unsupported Operator goal kind.")
        return self._record_result(goal, result)

    def note_goal_run(
        self,
        goal_id: str,
        ok: bool,
        message: str,
        details: dict[str, object] | None = None,
    ) -> OperatorGoal:
        """Record a run outcome for a recurring goal without ending the goal."""
        goal = self._require_goal(goal_id)
        now = datetime.now(UTC)
        run = OperatorGoalRun(
            goal_id=goal.goal_id,
            created_at=now,
            status=OperatorRunStatus.SUCCESS if ok else OperatorRunStatus.FAILED,
            message=message,
            details=details or {},
        )
        self.store.record_operator_goal_run(run)
        updates: dict[str, object] = {
            "last_run_at": now,
            "last_result": message,
            "updated_at": now,
            "failure_count": 0 if ok else goal.failure_count + 1,
        }
        if not ok and updates["failure_count"] >= MAX_CONSECUTIVE_FAILURES:
            updates["status"] = OperatorGoalStatus.PAUSED
            updates["next_run_at"] = None
            updates["last_result"] = (
                f"{message} (paused after {MAX_CONSECUTIVE_FAILURES} consecutive failures)"
            )
        updated = goal.model_copy(update=updates)
        self.store.upsert_operator_goal(updated)
        return updated

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

    def update_recurring_goal(
        self,
        goal_id: str,
        *,
        title: str,
        message: str,
        interval_minutes: int,
    ) -> OperatorGoal:
        goal = self._require_goal(goal_id)
        if goal.kind not in {
            OperatorGoalKind.RECURRING_NOTIFICATION,
            OperatorGoalKind.RECURRING_MISSION,
        }:
            raise ValueError("Only recurring goals can be edited here.")
        if interval_minutes < 1 or interval_minutes > MAX_INTERVAL_MINUTES:
            raise ValueError(
                f"Interval must be between 1 and {MAX_INTERVAL_MINUTES} minutes (7 days)."
            )
        clean_message = " ".join(message.split())[:600]
        if not clean_message:
            raise ValueError("Enter the notification message or mission request.")
        clean_title = " ".join(title.split())[:80] or self._title_for(clean_message)
        payload_key = (
            "message"
            if goal.kind == OperatorGoalKind.RECURRING_NOTIFICATION
            else "mission_request"
        )
        now = datetime.now(UTC)
        next_run_at = goal.next_run_at
        if goal.status == OperatorGoalStatus.ACTIVE and goal.schedule is not None:
            next_run_at = now + timedelta(minutes=interval_minutes)
        updated = goal.model_copy(
            update={
                "title": clean_title,
                "schedule": OperatorSchedule(interval_minutes=interval_minutes),
                "next_run_at": next_run_at,
                "payload": {**goal.payload, payload_key: clean_message},
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

    def _launch_recurring_mission(self, goal: OperatorGoal) -> NotificationResult:
        if self.mission_launcher is None:
            return NotificationResult(
                ok=False,
                message=(
                    "Recurring missions need the app runtime. "
                    "Start The Matrix app UI so the Operator can launch them."
                ),
            )
        try:
            return self.mission_launcher(goal)
        except Exception as exc:
            return NotificationResult(
                ok=False,
                message=f"Recurring mission failed to launch: {exc}",
            )

    def _auto_activate_enabled(self) -> bool:
        try:
            return self.store.get_preference("operator_auto_activate") is not False
        except Exception:
            return True

    def _ensure_recurring_capacity(self) -> None:
        open_statuses = {
            OperatorGoalStatus.PENDING,
            OperatorGoalStatus.ACTIVE,
            OperatorGoalStatus.PAUSED,
        }
        recurring_kinds = {
            OperatorGoalKind.RECURRING_NOTIFICATION,
            OperatorGoalKind.RECURRING_MISSION,
        }
        open_recurring = [
            goal
            for goal in self.store.list_operator_goals(limit=200)
            if goal.kind in recurring_kinds and goal.status in open_statuses
        ]
        if len(open_recurring) >= MAX_OPEN_RECURRING_GOALS:
            raise ValueError(
                f"The Operator already tracks {len(open_recurring)} recurring goals. "
                "Cancel or pause existing goals before adding more."
            )

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
        updates: dict[str, object] = {
            "last_run_at": now,
            "last_result": result.message,
            "updated_at": now,
        }
        if result.ok:
            updates["failure_count"] = 0
        else:
            updates["failure_count"] = goal.failure_count + 1
        if not result.ok and updates["failure_count"] >= MAX_CONSECUTIVE_FAILURES:
            updates["status"] = OperatorGoalStatus.PAUSED
            updates["next_run_at"] = None
            updates["last_result"] = (
                f"{result.message} (paused after {MAX_CONSECUTIVE_FAILURES} consecutive failures)"
            )
        elif goal.status == OperatorGoalStatus.ACTIVE and goal.schedule is not None:
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
        consecutive_failures = 0
        while not self._stop.wait(self.tick_seconds):
            try:
                self.run_due_goals()
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Operator scheduler failed while checking due goals; backing off."
                )
                backoff_seconds = min(
                    self.tick_seconds * (2 ** min(consecutive_failures, 5)),
                    300.0,
                )
                self._stop.wait(backoff_seconds)

    def _interval_minutes(self, lowered: str) -> int | None:
        match = _NUMERIC_INTERVAL_PATTERN.search(lowered)
        if match:
            return int(match.group(1)) * _UNIT_MINUTES[match.group(2)]
        for phrase, minutes in _WORD_INTERVALS:
            if phrase in lowered:
                return minutes
        return None

    def _notification_message(self, request: str) -> str:
        compact = " ".join(request.split())
        for prefix in [
            "send me a notification about",
            "send a notification about",
            "notify me about",
            "remind me to",
            "remind me about",
            "alert me about",
            "alert me when",
            "send me",
        ]:
            lowered = compact.lower()
            if prefix in lowered:
                start = lowered.index(prefix) + len(prefix)
                compact = compact[start:].strip(" .")
                break
        return self._strip_schedule_phrases(compact) or request

    def _mission_request(self, request: str) -> str:
        compact = " ".join(request.split())
        lowered = compact.lower()
        for prefix in [
            "set up a recurring task to",
            "setup a recurring task to",
            "create a recurring task to",
            "schedule a recurring task to",
            "set up a recurring task that",
            "create a recurring goal to",
            "schedule a task to",
            "set up a task to",
        ]:
            if lowered.startswith(prefix):
                compact = compact[len(prefix):].strip(" .")
                break
        return self._strip_schedule_phrases(compact) or request

    def _strip_schedule_phrases(self, text: str) -> str:
        cleaned = re.sub(
            r"\b(every|each)\s+\d+\s*(minute|minutes|min|mins|hour|hours|hr|hrs|day|days|week|weeks)\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(every|each)\s+(minute|hour|day|week|morning|evening|night)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(hourly|daily|weekly|nightly|recurring)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return " ".join(cleaned.split()).strip(" .,:-")

    def _title_for(self, message: str) -> str:
        title = message.strip(" .")
        if len(title) <= 64:
            return title or "Recurring notification"
        return f"{title[:61].rstrip()}..."
