"""Shared resources and FastAPI dependencies.

`build_services()` opens every long-lived resource (DynamoDB and S3 clients,
the OpenAI HTTP connection pool) inside one AsyncExitStack, so they are all
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
from pokemaker.integrations.artwork_provider import ArtworkProvider
from pokemaker.integrations.aws import client_kwargs, get_session
from pokemaker.integrations.dynamodb import dynamodb_client
from pokemaker.integrations.mock_artwork import MockArtworkProvider
from pokemaker.integrations.openai import openai_provider
from pokemaker.integrations.s3 import ArtworkStorage, artwork_storage
from pokemaker.repositories.card_repository import CardRepository
from pokemaker.repositories.profile_repository import ProfileRepository
from pokemaker.repositories.quota_repository import QuotaRepository
from pokemaker.services.artwork_service import ArtworkService
from pokemaker.services.card_service import CardService
from pokemaker.services.pin_service import PinService
from pokemaker.services.quota_service import QuotaService
from pokemaker.utils.clock import Clock, utc_now

logger = logging.getLogger(__name__)

# SSM secrets cached across warm Lambda invocations so we don't call SSM on
# every request. Purely a cache: a cold start just fetches them again.
_ssm_cache: dict[str, str] = {}


@dataclass(frozen=True)
class Services:
    cards: CardService
    artwork: ArtworkService
    pins: PinService
    quota: QuotaService
    storage: ArtworkStorage
    profiles: ProfileRepository
    dynamodb: object


@asynccontextmanager
async def build_services(
    settings: Settings,
    *,
    clock: Clock = utc_now,
    provider: ArtworkProvider | None = None,
) -> AsyncIterator[Services]:
    async with AsyncExitStack() as stack:
        ddb = await stack.enter_async_context(dynamodb_client(settings))
        storage = await stack.enter_async_context(artwork_storage(settings))
        if provider is None:
            provider = await _create_provider(settings, stack)

        family, table = settings.family_id, settings.table_name
        profile_repo = ProfileRepository(ddb, table, family)
        card_repo = CardRepository(ddb, table, family)
        quota_repo = QuotaRepository(ddb, table, family)
        cards = CardService(profile_repo, card_repo, storage, settings, clock)
        pins = PinService(profile_repo, settings, await _pin_token_secret(settings), clock)
        quota = QuotaService(quota_repo, settings, clock)
        artwork = ArtworkService(
            cards=cards,
            card_repo=card_repo,
            quota_repo=quota_repo,
            quota=quota,
            storage=storage,
            provider=provider,
            settings=settings,
            clock=clock,
        )
        yield Services(
            cards=cards,
            artwork=artwork,
            pins=pins,
            quota=quota,
            storage=storage,
            profiles=profile_repo,
            dynamodb=ddb,
        )


async def _create_provider(settings: Settings, stack: AsyncExitStack) -> ArtworkProvider:
    if settings.artwork_provider == "mock":
        return MockArtworkProvider(settings.mock_artwork_delay_seconds)
    api_key = await _openai_api_key(settings)
    return await stack.enter_async_context(openai_provider(settings, api_key))


async def _openai_api_key(settings: Settings) -> str:
    if settings.openai_api_key:
        return settings.openai_api_key.get_secret_value()
    name = settings.openai_api_key_ssm_parameter
    if not name:
        raise RuntimeError(
            "ARTWORK_PROVIDER=openai needs OPENAI_API_KEY or OPENAI_API_KEY_SSM_PARAMETER"
        )
    return await _ssm_secret(settings, name)


async def _pin_token_secret(settings: Settings) -> bytes:
    if settings.pin_token_secret:
        value = settings.pin_token_secret.get_secret_value()
    elif settings.pin_token_secret_ssm_parameter:
        value = await _ssm_secret(settings, settings.pin_token_secret_ssm_parameter)
    else:
        raise RuntimeError("Set PIN_TOKEN_SECRET (or PIN_TOKEN_SECRET_SSM_PARAMETER)")
    if len(value) < 32:
        raise RuntimeError("PIN_TOKEN_SECRET must be at least 32 characters")
    return value.encode()


async def _ssm_secret(settings: Settings, name: str) -> str:
    if name not in _ssm_cache:
        async with get_session().client("ssm", **client_kwargs(settings, None)) as ssm:
            response = await ssm.get_parameter(Name=name, WithDecryption=True)
        _ssm_cache[name] = response["Parameter"]["Value"]
    return _ssm_cache[name]


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_card_service(request: Request) -> CardService:
    return get_services(request).cards


def get_artwork_service(request: Request) -> ArtworkService:
    return get_services(request).artwork


def get_quota_service(request: Request) -> QuotaService:
    return get_services(request).quota


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
ArtworkServiceDep = Annotated[ArtworkService, Depends(get_artwork_service)]
QuotaServiceDep = Annotated[QuotaService, Depends(get_quota_service)]
ServicesDep = Annotated[Services, Depends(get_services)]
