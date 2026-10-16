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

from fastapi import Depends, Request

from pokemaker.config import Settings
from pokemaker.integrations.dynamodb import dynamodb_client
from pokemaker.integrations.s3 import ArtworkStorage, artwork_storage
from pokemaker.repositories.card_repository import CardRepository
from pokemaker.repositories.profile_repository import ProfileRepository
from pokemaker.services.card_service import CardService
from pokemaker.utils.clock import Clock, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Services:
    cards: CardService
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
        yield Services(
            cards=cards,
            storage=storage,
            profiles=profile_repo,
            dynamodb=ddb,
        )


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_card_service(request: Request) -> CardService:
    return get_services(request).cards


CardServiceDep = Annotated[CardService, Depends(get_card_service)]
ServicesDep = Annotated[Services, Depends(get_services)]
