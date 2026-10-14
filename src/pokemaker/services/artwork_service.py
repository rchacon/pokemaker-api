"""AI artwork generation: quota reservation, the provider call, storage, and
failure reconciliation.

The core difficulty: a DynamoDB transaction and a paid HTTP call to OpenAI can
never be made atomic together. So instead of pretending, every step is recorded
in a generation record (see `pokemaker.models.artwork` for the state machine) and
each failure path decides explicitly what happens to the reserved quota slot:

    failure                                   quota slot   why
    ----------------------------------------  -----------  ---------------------------
    validation / card missing (before step 1)  never taken  nothing reserved yet
    provider refused the prompt (400)          RELEASED     no image produced
    provider error response (401/429/5xx)      RELEASED     no image delivered
    timeout / connection lost / cancelled      CONSUMED     image may have been billed
    S3 upload or card update failed            CONSUMED     image was billed

Ambiguous outcomes are never retried automatically — a retry could pay twice.
The child can press the button again, which is a new, deliberate request.

Everything happens inside the HTTP request: no detached asyncio tasks or
FastAPI BackgroundTasks, because Lambda may freeze or end the process as soon
as the response is returned. If API Gateway's 30 s timeout cuts the HTTP
response short, the Lambda invocation keeps running to completion and the
client reads the result via GET /api/artwork/requests/{requestId}.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID

from pokemaker.config import Settings
from pokemaker.integrations.artwork_provider import (
    ArtworkProvider,
    GeneratedImage,
    ProviderAmbiguousError,
    ProviderFailedError,
    ProviderRejectedError,
)
from pokemaker.integrations.s3 import ArtworkStorage
from pokemaker.models.artwork import (
    GenerationRecord,
    GenerationResponse,
    GenerationStatus,
    ReservationStatus,
)
from pokemaker.models.card import Card, EnergyType
from pokemaker.repositories.card_repository import CardRepository
from pokemaker.repositories.quota_repository import (
    QuotaRepository,
    ReserveOutcome,
    TransactionContention,
)
from pokemaker.services.card_service import CardService
from pokemaker.services.quota_service import QuotaService
from pokemaker.utils import keys
from pokemaker.utils.clock import Clock, iso, utc_day, utc_now
from pokemaker.utils.errors import (
    ArtworkProviderError,
    ArtworkRejectedError,
    ArtworkStorageError,
    ArtworkTimeoutError,
    CardNotFoundError,
    GenerationNotFoundError,
    IdempotencyKeyReusedError,
    PokeMakerError,
    QuotaExceededError,
    ServiceBusyError,
)

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=5)  # comfortably longer than the Lambda timeout
STORAGE_TIMEOUT_SECONDS = 15
STORAGE_ATTEMPTS = 3

_ERRORS_BY_CODE: dict[str, type[PokeMakerError]] = {
    cls.code: cls
    for cls in (
        ArtworkRejectedError,
        ArtworkProviderError,
        ArtworkTimeoutError,
        ArtworkStorageError,
        CardNotFoundError,
    )
}

TYPE_THEMES: dict[EnergyType, str] = {
    EnergyType.GRASS: "leafy greens, flowers and sunlight",
    EnergyType.FIRE: "warm reds, oranges and glowing embers",
    EnergyType.WATER: "cool blues, splashes and bubbles",
    EnergyType.LIGHTNING: "bright yellows and crackling sparks",
    EnergyType.PSYCHIC: "purples, pinks and soft magical glows",
    EnergyType.FIGHTING: "earthy browns, rocks and dust",
    EnergyType.DARKNESS: "deep night blues, moonlight and shadows",
    EnergyType.METAL: "shiny silver, steel and gears",
    EnergyType.DRAGON: "rich golds and violets with an epic sky",
    EnergyType.COLORLESS: "soft natural colors, meadows and clouds",
}


def build_artwork_prompt(description: str, energy_type: EnergyType) -> str:
    """Wrap the child's description in fixed art direction.

    The description is the only user-controlled part and is length-limited
    upstream. The card's name is deliberately left out: names invite the model
    to paint text, and all text on the card is rendered by the app.

    The art direction aims for hand-painted collectible-card illustration
    rather than a generic 3D/cartoon look. The composition is 3:2 because
    that's both the API's landscape size (1536x1024) and the card's art frame.
    """
    return (
        "Create a premium collectible monster trading card illustration featuring: "
        f"{description.strip()}\n\n"
        "Art direction: Beautifully detailed hand-painted digital fantasy illustration, "
        "inspired by high-end Japanese creature trading card artwork. Expressive character "
        "design, crisp and carefully defined facial features, vibrant but balanced colors, "
        "subtle painterly textures, sophisticated lighting, rich environmental detail, "
        "dynamic composition, and professional illustration quality. Family-friendly and "
        "appealing to young children.\n\n"
        f"Elemental theme: {energy_type.value} — let {TYPE_THEMES[energy_type]} shape the "
        "environment, lighting and color palette.\n\n"
        "The creature should have a distinctive silhouette, natural anatomy appropriate to "
        "its fantasy design, and an appealing personality. Integrate the creature naturally "
        "into a detailed, atmospheric environment.\n\n"
        "Composition: Horizontal 3:2 illustration. Center the creature as the clear focal "
        "point, with enough surrounding environment to establish the scene.\n\n"
        "Avoid: 3D rendering, Pixar-style characters, plastic or glossy toy textures, "
        "generic AI cartoon aesthetics, blurry details, excessive bloom, text, typography, "
        "letters, numbers, logos, borders, card frames, watermarks, and any existing "
        "trademarked character.\n\n"
        "Generate only the illustration, not the complete trading card."
    )


class ArtworkService:
    def __init__(
        self,
        *,
        cards: CardService,
        card_repo: CardRepository,
        quota_repo: QuotaRepository,
        quota: QuotaService,
        storage: ArtworkStorage,
        provider: ArtworkProvider,
        settings: Settings,
        clock: Clock = utc_now,
    ) -> None:
        self._cards = cards
        self._card_repo = card_repo
        self._quota_repo = quota_repo
        self._quota = quota
        self._storage = storage
        self._provider = provider
        self._settings = settings
        self._clock = clock

    # --- Public API --------------------------------------------------------------

    async def generate(
        self,
        profile_id: str,
        card_id: UUID,
        request_id: UUID,
        description: str | None = None,
    ) -> GenerationResponse:
        # 1. Validate everything that doesn't cost anything, before reserving quota.
        card = await self._cards.get_card(profile_id, card_id)
        description = (description if description is not None else card.description).strip()
        if not description:
            raise PokeMakerError("Describe your creature before generating artwork.")
        if len(description) > self._settings.max_description_length:
            raise PokeMakerError(
                f"Creature descriptions can be at most "
                f"{self._settings.max_description_length} characters."
            )
        prompt = build_artwork_prompt(description, card.type)

        # 2. Atomically create the request record and reserve a quota slot.
        now = self._clock()
        record = GenerationRecord(
            request_id=str(request_id),
            family_id=self._settings.family_id,
            profile_id=profile_id,
            card_id=str(card_id),
            status=GenerationStatus.PENDING,
            reservation=ReservationStatus.RESERVED,
            quota_date=utc_day(now).isoformat(),
            prompt_chars=len(prompt),
            model=self._provider_model(),
            created_at=iso(now),
            updated_at=iso(now),
        )
        outcome = await self._reserve(record)
        if outcome is ReserveOutcome.DUPLICATE_REQUEST:
            return await self._replay(request_id, profile_id, card_id)
        if outcome is ReserveOutcome.QUOTA_EXCEEDED:
            quota = await self._quota.get_status()
            raise QuotaExceededError(
                "No artwork generations left today. More arrive at midnight UTC!",
                details={"quota": quota.model_dump(by_alias=True)},
            )

        # 3-6. Call the provider, store the image, attach it to the card.
        return await self._run(record, prompt, description)

    async def get_request(self, request_id: UUID) -> GenerationResponse:
        item = await self._quota_repo.get_generation(str(request_id))
        if item is None:
            raise GenerationNotFoundError("No generation request with that id")
        record = await self._reconcile_if_stale(
            GenerationRecord.model_validate(item, extra="ignore")
        )
        return await self._response(record)

    async def reconcile_stale(self) -> list[GenerationRecord]:
        """Settle every generation record left mid-flight by a crashed process."""
        settled: list[GenerationRecord] = []
        for item in await self._quota_repo.list_generations():
            record = GenerationRecord.model_validate(item, extra="ignore")
            if record.status.is_terminal:
                continue
            updated = await self._reconcile_if_stale(record)
            if updated.status.is_terminal:
                settled.append(updated)
        return settled

    # --- Steps -------------------------------------------------------------------

    async def _reserve(self, record: GenerationRecord) -> ReserveOutcome:
        day = utc_day(self._clock())
        expires = int(
            (self._clock() + timedelta(days=self._settings.quota_record_ttl_days)).timestamp()
        )
        item = record.model_dump(mode="json", by_alias=True, exclude_none=True)
        item["expiresAt"] = int(
            (self._clock() + timedelta(days=self._settings.generation_record_ttl_days)).timestamp()
        )
        # Concurrent reservations touch the same quota item. DynamoDB serializes
        # them; when two transactions collide it cancels one with
        # TransactionConflict *without applying anything*, so retrying with
        # jittered backoff is safe (unlike retrying the OpenAI call).
        for attempt in range(6):
            try:
                return await self._quota_repo.reserve(item, day, self._quota.limit, expires)
            except TransactionContention:
                await asyncio.sleep(random.uniform(0.02, 0.1) * (2**attempt))
        raise ServiceBusyError("Lots of artwork requests at once — please try again.")

    async def _run(
        self, record: GenerationRecord, prompt: str, description: str
    ) -> GenerationResponse:
        rid = record.request_id
        log = {"request_id": rid, "profile_id": record.profile_id, "card_id": record.card_id}

        # PENDING -> CALLING_PROVIDER. From here on we must assume money may be spent.
        try:
            await self._advance(
                record, GenerationStatus.PENDING, status=GenerationStatus.CALLING_PROVIDER
            )
        except asyncio.CancelledError:
            # Cancelled before OpenAI was ever called: the slot can safely go back.
            await asyncio.shield(self._release(record, "CANCELLED", "Request was cancelled"))
            raise

        try:
            # asyncio.timeout() bounds the *whole* provider call, including
            # connection setup, independent of the HTTP client's own timeouts.
            async with asyncio.timeout(self._settings.openai_timeout_seconds):
                image = await self._provider.generate(prompt, seed_text=description)
        except ProviderRejectedError as exc:
            logger.info("artwork rejected by provider", extra=log | {"reason": str(exc)})
            await self._release(record, ArtworkRejectedError.code, str(exc))
            raise ArtworkRejectedError(
                "The artist couldn't draw that one. Try describing your creature differently!"
            ) from None
        except ProviderFailedError as exc:
            logger.warning("artwork provider error", extra=log | {"reason": str(exc)})
            await self._release(record, ArtworkProviderError.code, str(exc))
            raise ArtworkProviderError(
                "The artwork service had a problem. Please try again."
            ) from None
        except (TimeoutError, ProviderAmbiguousError) as exc:
            # Ambiguous: OpenAI may have produced and billed an image we never got.
            # Keep the slot consumed and do NOT retry automatically.
            logger.warning("artwork provider timed out", extra=log | {"reason": repr(exc)})
            await self._fail_consumed(
                record,
                GenerationStatus.CALLING_PROVIDER,
                ArtworkTimeoutError.code,
                "The artwork took too long.",
            )
            raise ArtworkTimeoutError("The artwork took too long. Please try again.") from None
        except asyncio.CancelledError:
            # The request was cancelled (client gone, server shutting down) while
            # OpenAI was working. Same ambiguity as a timeout. Record it — shielded,
            # so a second cancellation can't interrupt the bookkeeping — and then
            # re-raise: swallowing CancelledError would break asyncio's shutdown.
            await asyncio.shield(
                self._fail_consumed(
                    record, GenerationStatus.CALLING_PROVIDER, "CANCELLED", "Request was cancelled"
                )
            )
            raise

        logger.info(
            "artwork generated",
            extra=log
            | {
                "model": image.model,
                "bytes": len(image.data),
                "usage": image.usage,
                "estimated_cost_usd": self._estimated_cost(image),
            },
        )
        try:
            return await self._store(record, image)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail_consumed(
                    record, GenerationStatus.STORING, "CANCELLED", "Request was cancelled"
                )
            )
            raise

    async def _store(self, record: GenerationRecord, image: GeneratedImage) -> GenerationResponse:
        # CALLING_PROVIDER -> STORING. We hold a paid image now, so the slot is consumed.
        updated = await self._advance(
            record,
            GenerationStatus.CALLING_PROVIDER,
            status=GenerationStatus.STORING,
            reservation=ReservationStatus.CONSUMED,
        )
        version = await self._card_repo.allocate_artwork_version(record.profile_id, record.card_id)
        if version is None:
            await self._finish_failed(
                updated, CardNotFoundError.code, "The card was deleted while its artwork was drawn."
            )
        key = keys.artwork_key(
            record.family_id, record.profile_id, record.card_id, version, image.extension
        )
        updated = await self._advance(
            updated, GenerationStatus.STORING, artworkKey=key, artworkVersion=version
        )

        # S3 PUTs to the same key are idempotent, so retrying a failed upload is
        # safe (unlike the provider call). Each attempt gets its own deadline.
        for attempt in range(1, STORAGE_ATTEMPTS + 1):
            try:
                async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
                    await self._storage.put(key, image.data, image.content_type)
                break
            except Exception as exc:
                logger.warning(
                    "artwork upload failed",
                    extra={"request_id": record.request_id, "attempt": attempt, "error": repr(exc)},
                )
                if attempt == STORAGE_ATTEMPTS:
                    await self._finish_failed(
                        updated, ArtworkStorageError.code, "Couldn't save the artwork."
                    )
                await asyncio.sleep(0.2 * attempt)

        outcome, card_item = await self._card_repo.attach_artwork(
            record.profile_id, record.card_id, key, version, iso(self._clock())
        )
        if outcome == "missing":
            await self._safe_delete(key)
            await self._finish_failed(
                updated, CardNotFoundError.code, "The card was deleted while its artwork was drawn."
            )
        # "superseded": a newer generation for this card finished first. Ours is
        # stored (as an older version) but the card keeps showing the newer one.
        final = await self._advance(
            updated,
            GenerationStatus.STORING,
            status=GenerationStatus.SUCCEEDED,
            completedAt=iso(self._clock()),
        )
        card = await self._cards.card_from_item(card_item) if card_item else None
        return GenerationResponse(
            request_id=final.request_id,
            status=final.status,
            reservation=final.reservation,
            card=card,
            quota=await self._quota.get_status(),
        )

    async def _replay(self, request_id: UUID, profile_id: str, card_id: UUID) -> GenerationResponse:
        """Same Idempotency-Key again: report the original request, never re-run it."""
        item = await self._quota_repo.get_generation(str(request_id))
        if item is None:  # vanishingly unlikely: record TTL'd between calls
            raise ServiceBusyError("Please try again.")
        record = GenerationRecord.model_validate(item, extra="ignore")
        if record.profile_id != profile_id or record.card_id != str(card_id):
            raise IdempotencyKeyReusedError("That request id was already used for another card.")
        record = await self._reconcile_if_stale(record)
        if record.status is GenerationStatus.FAILED and record.error:
            error_cls = _ERRORS_BY_CODE.get(record.error.code, ArtworkProviderError)
            raise error_cls(record.error.message, details={"requestId": record.request_id})
        return await self._response(record)

    # --- Failure handling --------------------------------------------------------

    async def _release(self, record: GenerationRecord, code: str, message: str) -> None:
        """Definite failure: give the quota slot back and mark the request FAILED."""
        now = iso(self._clock())
        changes = {
            "status": GenerationStatus.FAILED.value,
            "updatedAt": now,
            "completedAt": now,
            "error": {"code": code, "message": message},
        }
        try:
            released = await self._quota_repo.release(
                record.request_id, date.fromisoformat(record.quota_date), changes
            )
        except TransactionContention:
            await asyncio.sleep(0.05)
            released = await self._quota_repo.release(
                record.request_id, date.fromisoformat(record.quota_date), changes
            )
        if not released:
            logger.warning("reservation was not released", extra={"request_id": record.request_id})

    async def _fail_consumed(
        self, record: GenerationRecord, expected: GenerationStatus, code: str, message: str
    ) -> None:
        now = iso(self._clock())
        result = await self._quota_repo.transition(
            record.request_id,
            expected,
            {
                "status": GenerationStatus.FAILED.value,
                "reservation": ReservationStatus.CONSUMED.value,
                "error": {"code": code, "message": message},
                "updatedAt": now,
                "completedAt": now,
            },
        )
        if result is None:
            logger.warning(
                "generation was not in the expected state",
                extra={"request_id": record.request_id, "expected": expected.value},
            )

    async def _finish_failed(self, record: GenerationRecord, code: str, message: str) -> NoReturn:
        await self._fail_consumed(record, record.status, code, message)
        error_cls = _ERRORS_BY_CODE.get(code, ArtworkStorageError)
        raise error_cls(message)

    async def _reconcile_if_stale(self, record: GenerationRecord) -> GenerationRecord:
        """Settle a record whose process died mid-flight (e.g. Lambda timed out).

        PENDING: the provider was never called, so release the slot.
        CALLING_PROVIDER / STORING: outcome unknown or image paid, so consume it.
        Conditional writes make this safe even if the original process is
        somehow still alive: only one side can win each transition.
        """
        if record.status.is_terminal:
            return record
        age = self._clock() - _parse(record.updated_at)
        if age < STALE_AFTER:
            return record
        logger.warning(
            "reconciling stale generation",
            extra={"request_id": record.request_id, "status": record.status.value},
        )
        if record.status is GenerationStatus.PENDING:
            await self._release(record, "ABANDONED", "The request stopped before drawing started.")
        else:
            await self._fail_consumed(
                record, record.status, "ABANDONED", "The request stopped before it finished."
            )
            if record.artwork_key and record.status is GenerationStatus.STORING:
                await self._safe_delete(record.artwork_key)
        item = await self._quota_repo.get_generation(record.request_id)
        return GenerationRecord.model_validate(item, extra="ignore") if item else record

    # --- Helpers -----------------------------------------------------------------

    async def _advance(
        self, record: GenerationRecord, expected: GenerationStatus, **changes: Any
    ) -> GenerationRecord:
        changes = {
            k: (v.value if isinstance(v, GenerationStatus | ReservationStatus) else v)
            for k, v in changes.items()
        }
        changes["updatedAt"] = iso(self._clock())
        item = await self._quota_repo.transition(record.request_id, expected, changes)
        if item is None:
            # Someone else (a reconciler) already settled this request.
            raise ServiceBusyError("This artwork request was already finished elsewhere.")
        return GenerationRecord.model_validate(item, extra="ignore")

    async def _response(self, record: GenerationRecord) -> GenerationResponse:
        card: Card | None = None
        if record.status is GenerationStatus.SUCCEEDED:
            try:
                card = await self._cards.get_card(record.profile_id, UUID(record.card_id))
            except CardNotFoundError:
                card = None
        return GenerationResponse(
            request_id=record.request_id,
            status=record.status,
            reservation=record.reservation,
            card=card,
            error=record.error,
            quota=await self._quota.get_status(),
        )

    async def _safe_delete(self, key: str) -> None:
        try:
            await self._storage.delete(key)
        except Exception:
            logger.exception("failed to delete orphaned artwork", extra={"key": key})

    def _provider_model(self) -> str:
        return self._settings.openai_image_model if self._provider.name == "openai" else "mock"

    def _estimated_cost(self, image: GeneratedImage) -> float:
        return 0.0 if image.model == "mock" else self._settings.openai_estimated_cost_usd


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
