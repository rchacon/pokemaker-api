"""FastAPI application entry point.

Locally:   uvicorn pokemaker.main:app --reload
In Lambda: handler = pokemaker.main.handler  (Mangum adapts API Gateway events to ASGI)

Mangum runs the lifespan (startup + shutdown) around *every* Lambda invocation.
That's deliberate: async clients are bound to the event loop that created them,
and nothing in memory may be relied on between invocations anyway. Expensive,
loop-independent things (botocore's parsed service models, the OpenAI key from
SSM) are cached at module level, so warm invocations stay fast.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from pokemaker.config import Settings, get_settings
from pokemaker.dependencies import Services, ServicesDep, build_services
from pokemaker.routes import artwork, cards, profiles
from pokemaker.utils.errors import PokeMakerError
from pokemaker.utils.logging import configure_logging
from pokemaker.utils.middleware import BodySizeLimitMiddleware

logger = logging.getLogger(__name__)

ServicesFactory = Callable[[Settings], AbstractAsyncContextManager[Services]]


def create_app(
    settings: Settings | None = None, services_factory: ServicesFactory = build_services
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with services_factory(settings) as services:
            app.state.services = services
            yield
        # Leaving the `async with` closed every client opened in build_services.

    app = FastAPI(
        title="PokeMaker API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", "Idempotency-Key", "Authorization"],
        )

    @app.exception_handler(PokeMakerError)
    async def domain_error(_: Request, exc: PokeMakerError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return _error(422, "VALIDATION_ERROR", "Some fields need fixing.", {"problems": problems})

    @app.get("/api/health", tags=["health"])
    async def health(services: ServicesDep) -> JSONResponse:
        # Two independent I/O checks, so run them concurrently: the endpoint
        # takes as long as the slower check rather than the sum of both.
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(
                    services.dynamodb.describe_table(TableName=settings.table_name),  # type: ignore[attr-defined]
                    services.storage.ping(),
                )
        except Exception as exc:
            logger.warning("health check failed", extra={"error": repr(exc)})
            return JSONResponse({"status": "degraded"}, status_code=503)
        return JSONResponse({"status": "ok"})

    app.include_router(profiles.router)
    app.include_router(cards.router)
    app.include_router(artwork.router)
    return app


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


app = create_app()
handler = Mangum(app, lifespan="auto")
