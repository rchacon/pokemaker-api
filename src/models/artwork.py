"""Artwork generation request/record models and the generation state machine.

    PENDING ──► CALLING_PROVIDER ──► STORING ──► SUCCEEDED
       │               │                 │
       ▼               ▼                 ▼
     FAILED          FAILED            FAILED
  (RELEASED)   (RELEASED or CONSUMED)  (CONSUMED)

`status` says where the request is. `reservation` says what happened to the
quota slot reserved for it:

    RESERVED  slot held while the request is in flight
    CONSUMED  provider was (or may have been) charged — slot stays used
    RELEASED  provider definitely did not produce a billable image — slot returned
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from src.models.base import ApiModel, CleanStr
from src.models.card import Card
from src.models.quota import QuotaStatus


class GenerationStatus(StrEnum):
    PENDING = "PENDING"
    CALLING_PROVIDER = "CALLING_PROVIDER"
    STORING = "STORING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (GenerationStatus.SUCCEEDED, GenerationStatus.FAILED)


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class ArtworkGenerateRequest(ApiModel):
    # Optional: lets the editor send the description the child is looking at,
    # even if autosave hasn't persisted it yet. Falls back to the saved card.
    description: CleanStr | None = Field(default=None, max_length=1000)


class GenerationError(ApiModel):
    code: str
    message: str


class GenerationRecord(ApiModel):
    request_id: str
    family_id: str
    profile_id: str
    card_id: str
    status: GenerationStatus
    reservation: ReservationStatus
    quota_date: str
    prompt_chars: int = 0
    model: str | None = None
    artwork_key: str | None = None
    artwork_version: int | None = None
    error: GenerationError | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class GenerationResponse(ApiModel):
    request_id: str
    status: GenerationStatus
    reservation: ReservationStatus
    card: Card | None = None
    error: GenerationError | None = None
    quota: QuotaStatus
