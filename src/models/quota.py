from __future__ import annotations

from src.models.base import ApiModel


class QuotaStatus(ApiModel):
    date: str  # UTC day, YYYY-MM-DD
    used: int
    limit: int
    remaining: int
    resets_at: str  # next UTC midnight, ISO 8601
