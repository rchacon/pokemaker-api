"""Time helpers. Quotas use UTC calendar days.

A `Clock` is injected into services so tests can move time forward (e.g. to
verify that the quota resets at midnight UTC) without sleeping or patching.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_day(dt: datetime) -> date:
    return dt.astimezone(UTC).date()


def next_utc_midnight(dt: datetime) -> datetime:
    return datetime.combine(utc_day(dt) + timedelta(days=1), time.min, tzinfo=UTC)
