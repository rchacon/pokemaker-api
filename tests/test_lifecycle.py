"""Resource cleanup, the mock provider, and the app lifespan."""

import asyncio
import json
import time

import httpx
import pytest

from pokemaker.config import Settings
from pokemaker.dependencies import build_services
from pokemaker.integrations.mock_artwork import MockArtworkProvider, render_placeholder
from pokemaker.main import create_app
from scripts.init_local import create_table


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


async def test_lifespan_opens_resources_once_for_many_requests(settings: Settings):
    # A long-running server builds its clients once and shares them across
    # requests (unlike Lambda, where every invocation started from scratch).
    await create_table(settings)
    opened = 0

    def factory(s: Settings):
        nonlocal opened
        opened += 1
        return build_services(s)

    app = create_app(settings, services_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*(client.get("/api/artwork/quota") for _ in range(5)))
    assert [r.status_code for r in responses] == [200] * 5
    assert json.loads(responses[0].text)["limit"] == 10
    assert opened == 1
