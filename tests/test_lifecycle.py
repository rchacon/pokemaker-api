"""Resource cleanup, the mock provider, and the Lambda entry point."""

import asyncio
import json
import time

import pytest
from mangum import Mangum

from scripts.init_local import create_table
from src.config import Settings
from src.dependencies import build_services
from src.integrations.mock_artwork import MockArtworkProvider, render_placeholder
from src.main import create_app


async def test_build_services_closes_aws_clients(settings: Settings, services):
    async with build_services(settings) as svc:
        await svc.dynamodb.list_tables()
        # aiobotocore keeps one aiohttp ClientSession (connection pool) per client.
        pools = list(svc.dynamodb._endpoint.http_session._sessions.values())
        assert pools and not any(pool.closed for pool in pools)
    # Leaving the context closed the connection pool.
    assert all(pool.closed for pool in pools)


async def test_mock_provider_is_deterministic_and_free():
    provider = MockArtworkProvider(delay_seconds=0)
    first = await provider.generate("prompt, theme: fire — x", seed_text="a fox")
    second = await provider.generate("prompt, theme: fire — x", seed_text="a fox")
    other = await provider.generate("prompt, theme: fire — x", seed_text="a frog")
    assert first.data == second.data != other.data
    assert first.data.startswith(b"\x89PNG")
    assert first.model == "mock"


async def test_mock_provider_honours_timeouts():
    provider = MockArtworkProvider(delay_seconds=10)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await provider.generate("x")
    assert time.monotonic() - started < 1


async def test_rendering_runs_off_the_event_loop():
    # While a thread renders the placeholder, the loop keeps running other tasks.
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    await asyncio.to_thread(render_placeholder, "busy")
    task.cancel()
    assert ticks > 5


def _http_api_event(path: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "api.example.com"},
        "requestContext": {
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test",
            },
            "stage": "$default",
            "requestId": "r1",
        },
        "isBase64Encoded": False,
    }


def test_lambda_handler_runs_lifespan_per_invocation(settings: Settings):
    asyncio.run(create_table(settings))
    opened = 0

    def factory(s: Settings):
        nonlocal opened
        opened += 1
        return build_services(s)

    handler = Mangum(create_app(settings, services_factory=factory), lifespan="auto")
    for _ in range(2):
        response = handler(_http_api_event("/api/artwork/quota"), None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["limit"] == 10
    assert opened == 2
