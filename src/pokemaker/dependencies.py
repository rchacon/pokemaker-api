"""Shared resources and FastAPI dependencies.

`build_services()` opens every long-lived resource (the DynamoDB and S3
clients) inside one AsyncExitStack, so they are all
closed in reverse order on shutdown, even if startup fails halfway through.
The FastAPI lifespan in `pokemaker.main` enters it once; tests enter it directly.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from pokemaker.config import Settings
from pokemaker.integrations.dynamodb import dynamodb_client
from pokemaker.integrations.s3 import ArtworkStorage, artwork_storage
from pokemaker.repositories.card_repository import CardRepository
from pokemaker.repositories.profile_repository import ProfileRepository
from pokemaker.services.card_service import CardService
from pokemaker.services.pin_service import PinService
from pokemaker.utils.clock import Clock, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Services:
    cards: CardService
    pins: PinService
    storage: ArtworkStorage
    profiles: ProfileRepository
    dynamodb: object


@asynccontextmanager
async def build_services(
    settings: Settings,
    *,
    clock: Clock = utc_now,
) -> AsyncIterator[Services]:
    async with AsyncExitStack() as stack:
        ddb = await stack.enter_async_context(dynamodb_client(settings))
        storage = await stack.enter_async_context(artwork_storage(settings))

        family, table = settings.family_id, settings.table_name
        profile_repo = ProfileRepository(ddb, table, family)
        card_repo = CardRepository(ddb, table, family)
        cards = CardService(profile_repo, card_repo, storage, settings, clock)
        pins = PinService(profile_repo, settings, await _pin_token_secret(settings), clock)
        yield Services(
            cards=cards,
            pins=pins,
            storage=storage,
            profiles=profile_repo,
            dynamodb=ddb,
        )


async def _pin_token_secret(settings: Settings) -> bytes:
    if not settings.pin_token_secret:
        raise RuntimeError("Set PIN_TOKEN_SECRET")
    value = settings.pin_token_secret.get_secret_value()
    if len(value) < 32:
        raise RuntimeError("PIN_TOKEN_SECRET must be at least 32 characters")
    return value.encode()


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_card_service(request: Request) -> CardService:
    return get_services(request).cards


def get_pin_service(request: Request) -> PinService:
    return get_services(request).pins


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def require_unlocked(
    profile_id: str,
    request: Request,
    token: Annotated[str | None, Depends(bearer_token)],
) -> None:
    """Route dependency: changes to a PIN-protected profile need its unlock token."""
    await get_services(request).pins.authorize(profile_id, token)


CardServiceDep = Annotated[CardService, Depends(get_card_service)]
PinServiceDep = Annotated[PinService, Depends(get_pin_service)]
BearerToken = Annotated[str | None, Depends(bearer_token)]
RequireUnlocked = Depends(require_unlocked)
ServicesDep = Annotated[Services, Depends(get_services)]
