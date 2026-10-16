"""Test fixtures.

AWS is emulated by moto's in-process server, so the suite needs no Docker and
no credentials. Each test gets its own table, so tests are isolated and can
never touch real data. Set TEST_AWS_ENDPOINT_URL to run the same suite against
another emulator instead (e.g. http://localhost:8001 for DynamoDB Local).
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from moto.server import ThreadedMotoServer

# pokemaker.main builds the module-level `app` (for Uvicorn) on import,
# which reads settings. Tests construct their own Settings, but the import
# still needs the required variables when there's no .env (e.g. in CI).
os.environ.setdefault("FAMILY_ID", "belchers")

from pokemaker.config import Settings
from pokemaker.dependencies import Services, build_services
from pokemaker.main import create_app
from scripts.init_local import create_table


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def aws_endpoint() -> Iterator[str]:
    endpoint = os.environ.get("TEST_AWS_ENDPOINT_URL")
    if endpoint:
        yield endpoint
        return
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
def settings(aws_endpoint: str) -> Settings:
    suffix = uuid.uuid4().hex[:10]
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        family_id="belchers",
        log_level="WARNING",
        table_name=f"test-{suffix}",
        dynamodb_endpoint_url=aws_endpoint,
        cors_allow_origins=[],
    )


@pytest.fixture
async def services(settings: Settings) -> AsyncIterator[Services]:
    await create_table(settings)
    async with build_services(settings) as svc:
        yield svc


@pytest.fixture
async def client(settings: Settings, services: Services) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    # ASGITransport doesn't run the lifespan; install the already-open services.
    app.state.services = services
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
