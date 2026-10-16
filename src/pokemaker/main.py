"""FastAPI application entry point.

    uvicorn pokemaker.main:app --reload

The app runs as one long-lived process, so the lifespan below runs once at
startup: every client is created once and its connection pool is shared by all
requests, and the event loop interleaves many requests that are mostly waiting
on the network. That's what asyncio is good at.
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

from pokemaker.config import Settings, get_settings
from pokemaker.dependencies import Services, ServicesDep, build_services
from pokemaker.utils.errors import PokeMakerError
from pokemaker.utils.logging import configure_logging

logger = logging.getLogger(__name__)

ServicesFactory = Callable[[Settings], AbstractAsyncContextManager[Services]]


def create_app(
    settings: Settings | None = None, services_factory: ServicesFactory = build_services
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Runs once per process: before the first request and after the last.
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
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type"],
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
        try:
            async with asyncio.timeout(5):
                await services.dynamodb.describe_table(TableName=settings.table_name)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("health check failed", extra={"error": repr(exc)})
            return JSONResponse({"status": "degraded"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


app = create_app()
