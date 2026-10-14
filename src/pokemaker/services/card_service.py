from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID, uuid4

from pokemaker.config import Settings
from pokemaker.integrations.s3 import ArtworkStorage
from pokemaker.models.card import (
    SCHEMA_VERSION,
    Card,
    CardCreate,
    CardFields,
    CardUpdate,
    card_status,
)
from pokemaker.models.profile import Profile
from pokemaker.repositories.card_repository import CardRepository, VersionMismatch
from pokemaker.repositories.profile_repository import ProfileRepository
from pokemaker.utils import keys
from pokemaker.utils.clock import Clock, iso, utc_now
from pokemaker.utils.errors import CardNotFoundError, ProfileNotFoundError, VersionConflictError

logger = logging.getLogger(__name__)


class CardService:
    def __init__(
        self,
        profiles: ProfileRepository,
        cards: CardRepository,
        storage: ArtworkStorage,
        settings: Settings,
        clock: Clock = utc_now,
    ) -> None:
        self._profiles = profiles
        self._cards = cards
        self._storage = storage
        self._family_id = settings.family_id
        self._clock = clock

    # --- Profiles ----------------------------------------------------------------

    async def list_profiles(self) -> list[Profile]:
        items = await self._profiles.list_profiles()
        profiles = (
            Profile.model_validate(i | {"hasPin": bool(i.get("pinHash"))}, extra="ignore")
            for i in items
        )
        return sorted(profiles, key=lambda p: p.profile_id)

    async def require_profile(self, profile_id: str) -> dict[str, Any]:
        """Ensure the profile exists in the configured family.

        This is a data-integrity check, not authorization: without login,
        anyone who can reach the API can name any profile.
        """
        profile = await self._profiles.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"No profile named {profile_id!r}")
        return profile

    # --- Cards -------------------------------------------------------------------

    async def list_cards(self, profile_id: str) -> list[Card]:
        # The profile check and the card query don't depend on each other, so run
        # them concurrently: total latency ≈ the slower call, not the sum.
        # TaskGroup cancels the sibling if one fails (e.g. profile not found) and
        # re-raises the error (wrapped in an ExceptionGroup, unwrapped below).
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.require_profile(profile_id))
                cards_task = tg.create_task(self._cards.list_cards(profile_id))
        except* ProfileNotFoundError as group:
            raise group.exceptions[0] from None
        items = sorted(cards_task.result(), key=lambda c: c.get("cardNumber", 0))
        return await self._to_cards(items)

    async def get_card(self, profile_id: str, card_id: UUID) -> Card:
        item = await self._get_item(profile_id, card_id)
        return (await self._to_cards([item]))[0]

    async def create_card(self, profile_id: str, data: CardCreate) -> Card:
        await self.require_profile(profile_id)
        item = await self._new_card_item(profile_id, data)
        await self._cards.create_card(item)
        return (await self._to_cards([item]))[0]

    async def update_card(self, profile_id: str, card_id: UUID, data: CardUpdate) -> Card:
        fields = _fields_for_storage(data)
        fields["status"] = card_status(data)
        fields["updatedAt"] = iso(self._clock())
        try:
            item = await self._cards.update_fields(profile_id, str(card_id), fields, data.version)
        except VersionMismatch as exc:
            current = (await self._to_cards([exc.current]))[0]
            raise VersionConflictError(
                "This card was changed somewhere else since you opened it.",
                details={"current": current.model_dump(mode="json", by_alias=True)},
            ) from None
        if item is None:
            raise CardNotFoundError("Card not found")
        return (await self._to_cards([item]))[0]

    async def duplicate_card(self, profile_id: str, card_id: UUID) -> Card:
        source = await self._get_item(profile_id, card_id)
        fields = CardFields.model_validate(source, extra="ignore")
        if fields.name:
            fields.name = f"{fields.name[:19]} Copy"
        item = await self._new_card_item(profile_id, fields)
        if source.get("artworkKey"):
            # Copy the artwork server-side (S3 → S3, no download) so the copy is
            # independent: deleting either card later won't break the other.
            ext = source["artworkKey"].rsplit(".", 1)[-1]
            new_key = keys.artwork_key(self._family_id, profile_id, item["cardId"], 1, ext)
            await self._storage.copy(source["artworkKey"], new_key)
            item |= {"artworkKey": new_key, "artworkVersion": 1, "artworkSeq": 1}
        await self._cards.create_card(item)
        return (await self._to_cards([item]))[0]

    async def delete_card(self, profile_id: str, card_id: UUID) -> None:
        # Order matters here, so these steps are sequential, not concurrent:
        # DynamoDB is the source of truth, so delete the card first. If artwork
        # cleanup then fails we leave orphaned objects (harmless, private) rather
        # than a card pointing at missing artwork.
        old = await self._cards.delete_card(profile_id, str(card_id))
        if old is None:
            raise CardNotFoundError("Card not found")
        prefix = keys.artwork_prefix(self._family_id, profile_id, str(card_id))
        try:
            await self._storage.delete_prefix(prefix)
        except Exception:
            logger.exception("artwork cleanup failed", extra={"prefix": prefix})

    # --- Helpers -----------------------------------------------------------------

    async def _get_item(self, profile_id: str, card_id: UUID) -> dict[str, Any]:
        item = await self._cards.get_card(profile_id, str(card_id))
        if item is None:
            await self.require_profile(profile_id)  # better error if the profile is wrong
            raise CardNotFoundError("Card not found")
        return item

    async def _new_card_item(self, profile_id: str, data: CardFields) -> dict[str, Any]:
        number = await self._profiles.next_card_number(profile_id)
        now = iso(self._clock())
        return _fields_for_storage(data) | {
            "cardId": str(uuid4()),
            "profileId": profile_id,
            "cardNumber": number,
            "artworkKey": None,
            "artworkVersion": 0,
            "status": card_status(data),
            "schemaVersion": SCHEMA_VERSION,
            "version": 1,
            "createdAt": now,
            "updatedAt": now,
        }

    async def _to_cards(self, items: list[dict[str, Any]]) -> list[Card]:
        urls = await self._storage.presign_many([i.get("artworkKey") for i in items])
        return [
            Card.model_validate(item | {"artworkUrl": url}, extra="ignore")
            for item, url in zip(items, urls, strict=True)
        ]

    async def card_from_item(self, item: dict[str, Any]) -> Card:
        return (await self._to_cards([item]))[0]


def _fields_for_storage(data: CardFields) -> dict[str, Any]:
    fields = data.model_dump(mode="json", by_alias=True, include=set(CardFields.model_fields))
    return fields
