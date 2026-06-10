"""Minimal 5-field cron evaluator: minute hour day-of-month month day-of-week.

Supports `*`, numbers, ranges (`1-5`), lists (`1,3,5`), and steps (`*/15`, `1-5/2`).
Day-of-week uses cron numbering: 0 or 7 = Sunday through 6 = Saturday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class CronError(ValueError):
    """Raised when a cron expression is invalid or unsatisfiable."""


_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_SEARCH_LIMIT_DAYS = 366 * 4 + 1


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool


def parse_cron(expression: str) -> CronSchedule:
    fields = expression.split()
    if len(fields) != 5:
        raise CronError(
            "Cron expressions need 5 fields: minute hour day-of-month month day-of-week."
        )
    parsed = [
        _parse_field(field, name, low, high)
        for field, name, (low, high) in zip(fields, _FIELD_NAMES, _FIELD_BOUNDS)
    ]
    return CronSchedule(
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=frozenset(value % 7 for value in parsed[4]),
        day_restricted=fields[2].strip() != "*",
        weekday_restricted=fields[4].strip() != "*",
    )


def next_run(expression: str | CronSchedule, after: datetime) -> datetime:
    """Return the first matching wall-clock time strictly after `after`."""
    schedule = parse_cron(expression) if isinstance(expression, str) else expression
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = candidate + timedelta(days=_SEARCH_LIMIT_DAYS)
    while candidate <= limit:
        if candidate.month not in schedule.months:
            candidate = _start_of_next_month(candidate)
            continue
        if not _day_matches(schedule, candidate):
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour not in schedule.hours:
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
        if candidate.minute not in schedule.minutes:
            candidate += timedelta(minutes=1)
            continue
        return candidate
    raise CronError("No matching time found for this cron expression.")


def _parse_field(field: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = _to_int(step_text, name)
            if step < 1:
                raise CronError(f"Cron {name} step must be 1 or more.")
        if part in {"*", ""}:
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start = _to_int(start_text, name)
            end = _to_int(end_text, name)
        else:
            start = end = _to_int(part, name)
        if start < low or end > high or start > end:
            raise CronError(f"Cron {name} value must be between {low} and {high}.")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"Cron {name} field selects no values.")
    return frozenset(values)


def _to_int(text: str, name: str) -> int:
    try:
        return int(text.strip())
    except ValueError as exc:
        raise CronError(f"Cron {name} field has a non-numeric value: {text!r}.") from exc


def _day_matches(schedule: CronSchedule, candidate: datetime) -> bool:
    # Standard cron semantics: when both day fields are restricted, either may match.
    day_ok = candidate.day in schedule.days
    weekday_ok = (candidate.weekday() + 1) % 7 in schedule.weekdays
    if schedule.day_restricted and schedule.weekday_restricted:
        return day_ok or weekday_ok
    if schedule.day_restricted:
        return day_ok
    if schedule.weekday_restricted:
        return weekday_ok
    return True


def _start_of_next_month(candidate: datetime) -> datetime:
    if candidate.month == 12:
        return candidate.replace(year=candidate.year + 1, month=1, day=1, hour=0, minute=0)
    return candidate.replace(month=candidate.month + 1, day=1, hour=0, minute=0)
