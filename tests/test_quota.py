import asyncio
import uuid
from datetime import date

import httpx

from pokemaker.dependencies import Services
from pokemaker.repositories.quota_repository import ReserveOutcome, TransactionContention


async def _reserve(services: Services, day: date, limit: int = 10) -> ReserveOutcome:
    repo = services.artwork._quota_repo
    for _ in range(20):
        try:
            return await repo.reserve({"requestId": str(uuid.uuid4())}, day, limit, 0)
        except TransactionContention:
            await asyncio.sleep(0.01)
    raise AssertionError("never got through")


async def test_quota_starts_full(client: httpx.AsyncClient):
    quota = (await client.get("/api/artwork/quota")).json()
    assert quota == {
        "date": "2026-10-13",
        "used": 0,
        "limit": 10,
        "remaining": 10,
        "resetsAt": "2026-10-14T00:00:00.000Z",
    }


async def test_concurrent_reservations_never_exceed_the_limit(services: Services):
    # 25 requests race for 10 slots. DynamoDB's conditional transaction is the
    # only thing standing between us and over-spending: no locks, no counters
    # in memory. Exactly 10 must win.
    day = date(2026, 10, 13)
    outcomes = await asyncio.gather(*(_reserve(services, day) for _ in range(25)))
    assert outcomes.count(ReserveOutcome.RESERVED) == 10
    assert outcomes.count(ReserveOutcome.QUOTA_EXCEEDED) == 15
    assert await services.artwork._quota_repo.get_usage(day) == 10


async def test_zero_limit_blocks_everything(services: Services):
    assert await _reserve(services, date(2026, 1, 1), limit=0) == ReserveOutcome.QUOTA_EXCEEDED


async def test_same_request_id_reserves_only_once(services: Services):
    repo = services.artwork._quota_repo
    day = date(2026, 10, 13)
    record = {"requestId": "fixed-id"}
    results = await asyncio.gather(
        *(repo.reserve(record, day, 10, 0) for _ in range(5)), return_exceptions=True
    )
    reserved = [r for r in results if r == ReserveOutcome.RESERVED]
    assert len(reserved) == 1
    assert await repo.get_usage(day) == 1


async def test_quota_resets_at_midnight_utc(client: httpx.AsyncClient, services: Services, clock):
    day = clock().date()
    for _ in range(10):
        assert await _reserve(services, day) == ReserveOutcome.RESERVED
    assert (await client.get("/api/artwork/quota")).json()["remaining"] == 0

    clock.advance(hours=8, minutes=59)  # 23:59 UTC
    assert (await client.get("/api/artwork/quota")).json()["remaining"] == 0

    clock.advance(minutes=1)  # 00:00 UTC next day: a fresh quota item
    quota = (await client.get("/api/artwork/quota")).json()
    assert quota["date"] == "2026-10-14"
    assert quota["remaining"] == 10
