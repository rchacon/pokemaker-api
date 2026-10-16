"""Origin verification and rate limiting (what API Gateway used to do)."""

import httpx
import pytest
from pydantic import SecretStr

from pokemaker.config import Settings
from pokemaker.main import create_app
from pokemaker.utils.edge import RateLimitMiddleware


@pytest.fixture
async def guarded_client(settings: Settings, services):
    # model_copy skips validation, so pass the SecretStr the setting expects.
    secret = SecretStr("cloudfront-only-secret")
    settings = settings.model_copy(update={"origin_verify_secret": secret})
    app = create_app(settings, services_factory=lambda s: _existing(services))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


class _existing:
    """Hand the already-open test services to the app's lifespan."""

    def __init__(self, services):
        self.services = services

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *exc):
        return None


async def test_requests_without_the_cloudfront_header_are_refused(guarded_client):
    for headers in ({}, {"X-Origin-Verify": "wrong"}):
        response = await guarded_client.get("/api/profiles", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_requests_with_the_cloudfront_header_pass(guarded_client):
    response = await guarded_client.get(
        "/api/profiles", headers={"X-Origin-Verify": "cloudfront-only-secret"}
    )
    assert response.status_code == 200


async def test_no_secret_configured_means_open(client: httpx.AsyncClient):
    assert (await client.get("/api/profiles")).status_code == 200  # local development


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _limited(per_minute: int, clock: FakeTime, **kwargs) -> httpx.AsyncClient:
    middleware = RateLimitMiddleware(_ok_app, per_minute, clock=clock, **kwargs)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://test")


async def test_rate_limit_allows_a_burst_then_refills():
    clock = FakeTime()
    async with _limited(3, clock) as client:
        codes = [(await client.get("/api/profiles")).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        limited = await client.get("/api/profiles")
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert limited.headers["retry-after"] == "20"  # 3/min = one token every 20 s
        clock.now += 20
        assert (await client.get("/api/profiles")).status_code == 200


async def test_health_checks_are_never_limited():
    async with _limited(1, FakeTime()) as client:
        assert [(await client.get("/api/health")).status_code for _ in range(5)] == [200] * 5


async def test_clients_are_limited_separately_using_the_cloudfront_address():
    clock = FakeTime()
    async with _limited(1, clock, trust_forwarded_for=True) as client:
        # CloudFront appends the viewer's address; anything earlier is client-supplied.
        a = {"X-Forwarded-For": "6.6.6.6, 1.1.1.1"}
        b = {"X-Forwarded-For": "6.6.6.6, 2.2.2.2"}
        assert (await client.get("/x", headers=a)).status_code == 200
        assert (await client.get("/x", headers=a)).status_code == 429
        assert (await client.get("/x", headers=b)).status_code == 200


async def test_forged_forwarded_for_is_ignored_unless_trusted():
    async with _limited(1, FakeTime()) as client:
        assert (await client.get("/x", headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 200
        assert (await client.get("/x", headers={"X-Forwarded-For": "2.2.2.2"})).status_code == 429


async def test_zero_disables_rate_limiting():
    async with _limited(0, FakeTime()) as client:
        assert [(await client.get("/x")).status_code for _ in range(3)] == [200] * 3
