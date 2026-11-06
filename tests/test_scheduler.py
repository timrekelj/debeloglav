from datetime import UTC, datetime, timedelta

import pytest

from debeloglav.scheduler import cron_interval, next_cron_run


def test_interval_of_a_daily_task():
    assert cron_interval("0 8 * * *") == timedelta(days=1)


def test_interval_of_a_frequent_task():
    assert cron_interval("*/5 * * * *") == timedelta(minutes=5)


@pytest.mark.parametrize("cron", ["not cron", "0 8 * *", "0 8 * * * *", "61 * * * *"])
def test_bad_expressions_are_refused(cron):
    with pytest.raises(ValueError):
        cron_interval(cron)


def test_next_run_is_in_local_time():
    # 8:00 in Ljubljana in winter (UTC+1) is 7:00 UTC.
    after = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    assert next_cron_run("0 8 * * *", after) == datetime(2026, 1, 11, 7, 0, tzinfo=UTC)


def test_next_run_follows_summer_time():
    # 8:00 in Ljubljana in summer (UTC+2) is 6:00 UTC.
    after = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    assert next_cron_run("0 8 * * *", after) == datetime(2026, 7, 11, 6, 0, tzinfo=UTC)


def test_next_run_across_the_clock_change():
    # Clocks go forward on 2026-03-29: the day before is UTC+1, the day itself UTC+2.
    after = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    assert next_cron_run("0 8 * * *", after) == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)
