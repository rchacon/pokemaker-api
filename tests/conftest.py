"""Test fixtures.

AWS is emulated by moto's in-process server, so the suite needs no Docker and
no credentials. Each test gets its own table and bucket, so tests are isolated
and can never touch real data. Set TEST_AWS_ENDPOINT_URL to run the same suite
against another emulator instead (e.g. http://localhost:8001 for DynamoDB Local
— the S3 parts then need TEST_S3_ENDPOINT_URL=http://localhost:9000 too).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
from scripts.init_local import create_bucket, create_table
from scripts.seed_profiles import seed


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def aws_endpoints() -> Iterator[tuple[str, str]]:
    ddb = os.environ.get("TEST_AWS_ENDPOINT_URL")
    if ddb:
        yield ddb, os.environ.get("TEST_S3_ENDPOINT_URL", ddb)
        return
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
def settings(aws_endpoints: tuple[str, str]) -> Settings:
    suffix = uuid.uuid4().hex[:10]
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        family_id="belchers",
        log_level="WARNING",
        table_name=f"test-{suffix}",
        artwork_bucket=f"test-artwork-{suffix}",
        dynamodb_endpoint_url=aws_endpoints[0],
        s3_endpoint_url=aws_endpoints[1],
        s3_public_endpoint_url=aws_endpoints[1],
        cors_allow_origins=[],
    )


@dataclass
class FakeClock:
    """Controllable time, so tests can cross midnight UTC instantly."""

    now: datetime = field(default_factory=lambda: datetime(2026, 10, 13, 15, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def services(settings: Settings, clock: FakeClock) -> AsyncIterator[Services]:
    await asyncio.gather(create_table(settings), create_bucket(settings))
    await seed(settings)
    async with build_services(settings, clock=clock) as svc:
        yield svc


@pytest.fixture
async def client(settings: Settings, services: Services) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    # ASGITransport doesn't run the lifespan; install the already-open services.
    app.state.services = services
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def card(client: httpx.AsyncClient) -> dict:
    response = await client.post(
        "/api/profiles/kid1/cards",
        json={
            "name": "Sparkpup",
            "type": "lightning",
            "hp": 70,
            "description": "a small yellow puppy with lightning-bolt ears",
            "attacks": [{"name": "Zap", "damage": 20, "cost": ["lightning"]}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
