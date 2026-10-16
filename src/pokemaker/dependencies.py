"""Shared resources and FastAPI dependencies.

`build_services()` opens every long-lived resource (so far, the DynamoDB client)
inside one AsyncExitStack, so they are all closed in reverse order on shutdown,
even if startup fails halfway through. The FastAPI lifespan in `pokemaker.main`
enters it once; tests enter it directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from pokemaker.config import Settings
from pokemaker.integrations.dynamodb import dynamodb_client


@dataclass(frozen=True)
class Services:
    dynamodb: object


@asynccontextmanager
async def build_services(settings: Settings) -> AsyncIterator[Services]:
    async with AsyncExitStack() as stack:
        ddb = await stack.enter_async_context(dynamodb_client(settings))
        yield Services(dynamodb=ddb)


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]
