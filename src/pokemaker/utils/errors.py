"""Domain errors. Services raise these; `pokemaker.main` turns them into JSON responses.

Keeping HTTP status codes next to the error types (rather than in each route)
keeps route handlers thin and error responses consistent:

    {"error": {"code": "CARD_NOT_FOUND", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any


class PokeMakerError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(PokeMakerError):
    status_code = 404
    code = "NOT_FOUND"


class ProfileNotFoundError(NotFoundError):
    code = "PROFILE_NOT_FOUND"


class CardNotFoundError(NotFoundError):
    code = "CARD_NOT_FOUND"


class GenerationNotFoundError(NotFoundError):
    code = "GENERATION_NOT_FOUND"


class PinRequiredError(PokeMakerError):
    """The profile has a PIN and the request has no valid unlock token for it."""

    status_code = 401
    code = "PIN_REQUIRED"


class WrongPinError(PokeMakerError):
    status_code = 401
    code = "WRONG_PIN"


class ProfileLockedError(PokeMakerError):
    """Too many wrong PINs; unlocking is paused for a while."""

    status_code = 423
    code = "PROFILE_LOCKED"


class VersionConflictError(PokeMakerError):
    """The client edited an out-of-date copy of a card (optimistic concurrency)."""

    status_code = 409
    code = "VERSION_CONFLICT"


class IdempotencyKeyReusedError(PokeMakerError):
    """The same Idempotency-Key was sent for a *different* card."""

    status_code = 409
    code = "IDEMPOTENCY_KEY_REUSED"


class QuotaExceededError(PokeMakerError):
    status_code = 429
    code = "QUOTA_EXCEEDED"


class ArtworkRejectedError(PokeMakerError):
    """The image provider refused the prompt (e.g. safety system). Quota is released."""

    status_code = 422
    code = "ARTWORK_REJECTED"


class ArtworkProviderError(PokeMakerError):
    status_code = 502
    code = "ARTWORK_PROVIDER_ERROR"


class ArtworkTimeoutError(PokeMakerError):
    status_code = 504
    code = "ARTWORK_TIMEOUT"


class ArtworkStorageError(PokeMakerError):
    status_code = 502
    code = "ARTWORK_STORAGE_ERROR"


class ServiceBusyError(PokeMakerError):
    """Contention we could not resolve with a few retries (e.g. transaction conflicts)."""

    status_code = 503
    code = "SERVICE_BUSY"
