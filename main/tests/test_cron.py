from __future__ import annotations

from datetime import datetime

import pytest

from thematrix.operator.cron import CronError, next_run, parse_cron


def test_cron_next_run_finds_weekday_morning() -> None:
    # Saturday 2026-06-13 10:00 -> Monday 2026-06-15 09:00
    after = datetime(2026, 6, 13, 10, 0)

    result = next_run("0 9 * * 1-5", after)

    assert result == datetime(2026, 6, 15, 9, 0)
    assert result.weekday() == 0


def test_cron_next_run_same_day_when_time_is_ahead() -> None:
    after = datetime(2026, 6, 11, 8, 15)

    assert next_run("30 8 * * *", after) == datetime(2026, 6, 11, 8, 30)
    assert next_run("0 8 * * *", after) == datetime(2026, 6, 12, 8, 0)


def test_cron_next_run_supports_steps_and_lists() -> None:
    after = datetime(2026, 6, 11, 8, 16)

    assert next_run("*/15 * * * *", after) == datetime(2026, 6, 11, 8, 30)
    assert next_run("0 9,18 * * *", after) == datetime(2026, 6, 11, 9, 0)


def test_cron_sunday_accepts_zero_and_seven() -> None:
    after = datetime(2026, 6, 11, 0, 0)  # Thursday

    sunday = datetime(2026, 6, 14, 9, 0)
    assert next_run("0 9 * * 0", after) == sunday
    assert next_run("0 9 * * 7", after) == sunday


def test_cron_specific_month_and_day() -> None:
    after = datetime(2026, 6, 11, 0, 0)

    assert next_run("0 0 1 7 *", after) == datetime(2026, 7, 1, 0, 0)


def test_cron_rejects_invalid_expressions() -> None:
    for expression in ["0 9 * *", "61 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *"]:
        with pytest.raises(CronError):
            parse_cron(expression)


def test_cron_day_and_weekday_use_or_semantics() -> None:
    # The 13th OR any Friday, whichever comes first after Thursday June 11.
    after = datetime(2026, 6, 11, 0, 0)

    assert next_run("0 0 13 * 5", after) == datetime(2026, 6, 12, 0, 0)  # Friday the 12th
