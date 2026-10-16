"""Edge protection that API Gateway used to provide, now inside the app.

The API runs as one long-lived Uvicorn process behind CloudFront. Two pure-ASGI
middlewares guard it:

* OriginVerifyMiddleware: only CloudFront may reach the server (the security
  group allows CloudFront's address ranges), but those ranges are shared by
  *every* CloudFront distribution. CloudFront adds a secret header to requests
  it forwards from *our* distribution, and anything without it is refused.

* RateLimitMiddleware: a per-client token bucket, so one misbehaving device
  can't flood the API. A family shares one home IP address, so the default is
  generous; artwork costs are bounded separately by the daily quota.
"""

from __future__ import annotations

import hmac
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

ORIGIN_HEADER = b"x-origin-verify"


async def send_error(
    send: Send,
    status: int,
    code: str,
    message: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps({"error": {"code": code, "message": message, "details": {}}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value
    return None


@dataclass
class OriginSecret:
    """Filled in by the lifespan (the secret may come from SSM, which is async)."""

    value: bytes | None = None


class OriginVerifyMiddleware:
    def __init__(self, app: ASGIApp, secret: OriginSecret) -> None:
        self.app = app
        self.secret = secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        expected = self.secret.value
        if scope["type"] != "http" or expected is None:
            await self.app(scope, receive, send)  # not configured: local development
            return
        provided = _header(scope, ORIGIN_HEADER) or b""
        # Constant-time comparison, so response timing doesn't leak the secret.
        if not hmac.compare_digest(provided, expected):
            await send_error(send, 403, "FORBIDDEN", "Requests must come through the website.")
            return
        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """Token bucket per client IP: `per_minute` requests, refilled continuously.

    The buckets live in this process's memory. That is fine here: there is a
    single Uvicorn process, and a restart merely resets the counts. Unlike the
    artwork quota, this is a courtesy limit, not something money depends on.

    No lock is needed: the check-and-update below never awaits, so the event
    loop cannot switch to another request halfway through it.
    """

    MAX_TRACKED_CLIENTS = 10_000

    def __init__(
        self,
        app: ASGIApp,
        per_minute: int,
        *,
        trust_forwarded_for: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60
        self.trust_forwarded_for = trust_forwarded_for
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last refill)

    def client_ip(self, scope: Scope) -> str:
        if self.trust_forwarded_for:
            # CloudFront *appends* the address it saw to X-Forwarded-For. Earlier
            # entries come from the client and can be forged, so use the last.
            forwarded = _header(scope, b"x-forwarded-for")
            if forwarded:
                return forwarded.decode("latin-1").rsplit(",", 1)[-1].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def take(self, ip: str) -> float:
        """Spend one token. Returns 0 if allowed, else seconds until the next token."""
        now = self.clock()
        tokens, last = self.buckets.get(ip, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens >= 1:
            self.buckets[ip] = (tokens - 1, now)
            return 0.0
        self.buckets[ip] = (tokens, now)
        return (1 - tokens) / self.refill_per_second

    def _prune(self) -> None:
        if len(self.buckets) <= self.MAX_TRACKED_CLIENTS:
            return
        now = self.clock()
        full_after = self.capacity / self.refill_per_second
        # A bucket that has had time to refill completely is the same as no bucket.
        self.buckets = {ip: v for ip, v in self.buckets.items() if now - v[1] < full_after}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/api/health" or self.capacity <= 0:
            await self.app(scope, receive, send)
            return
        self._prune()
        wait = self.take(self.client_ip(scope))
        if wait > 0:
            retry_after = str(max(1, math.ceil(wait))).encode()
            await send_error(
                send,
                429,
                "RATE_LIMITED",
                "Whoa, slow down a little!",
                [(b"retry-after", retry_after)],
            )
            return
        await self.app(scope, receive, send)
