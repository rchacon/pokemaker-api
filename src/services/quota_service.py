from __future__ import annotations

from datetime import datetime

from src.config import Settings
from src.models.quota import QuotaStatus
from src.repositories.quota_repository import QuotaRepository
from src.utils.clock import Clock, iso, next_utc_midnight, utc_day, utc_now


class QuotaService:
    """Read-side view of the family's daily artwork quota.

    The quota "resets" at midnight UTC simply because each UTC day has its own
    item (QUOTA#ARTWORK#YYYY-MM-DD). Nothing has to run at midnight, and TTL is
    only used to tidy up old items later.
    """

    def __init__(self, repo: QuotaRepository, settings: Settings, clock: Clock = utc_now) -> None:
        self._repo = repo
        self._limit = settings.daily_generation_limit
        self._clock = clock

    @property
    def limit(self) -> int:
        return self._limit

    def now(self) -> datetime:
        return self._clock()

    async def get_status(self) -> QuotaStatus:
        now = self._clock()
        used = await self._repo.get_usage(utc_day(now))
        return QuotaStatus(
            date=utc_day(now).isoformat(),
            used=used,
            limit=self._limit,
            remaining=max(0, self._limit - used),
            resets_at=iso(next_utc_midnight(now)),
        )
