import asyncio
import uuid
from datetime import date

import httpx
import pytest

from pokemaker.dependencies import Services
from pokemaker.models.artwork import GenerationStatus, ReservationStatus
from pokemaker.utils.errors import ArtworkStorageError
from tests.conftest import FakeProvider


def _url(card: dict) -> str:
    return f"/api/profiles/{card['profileId']}/cards/{card['cardId']}/artwork"


async def _generate(client: httpx.AsyncClient, card: dict, key: str | None = None, **body):
    headers = {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post(_url(card), headers=headers, json=body or None)


async def _record(services: Services, key: str) -> dict:
    return await services.artwork._quota_repo.get_generation(key)


async def _used(services: Services) -> int:
    return await services.artwork._quota_repo.get_usage(date(2026, 10, 13))


async def test_generate_stores_artwork_and_uses_one_slot(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider, card: dict
):
    response = await _generate(client, card)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["reservation"] == "CONSUMED"
    assert body["quota"]["remaining"] == 9

    updated = body["card"]
    key = f"families/belchers/profiles/kid1/cards/{card['cardId']}/artwork/v1.png"
    assert updated["artworkKey"] == key
    assert updated["artworkVersion"] == 1
    assert updated["artworkUrl"].startswith("http")
    # Artwork doesn't bump the edit version, so in-flight autosaves don't conflict.
    assert updated["version"] == card["version"]
    assert await services.storage.exists(key)

    # The prompt asks for art only and includes the child's description.
    prompt = provider.prompts[0]
    assert "lightning-bolt ears" in prompt
    assert "Avoid:" in prompt and "text, typography" in prompt
    assert "Generate only the illustration" in prompt
    assert "theme: lightning" in prompt  # the card's type steers the palette
    assert "Sparkpup" not in prompt


async def test_regenerate_creates_new_version_and_keeps_other_fields(
    client: httpx.AsyncClient, card: dict
):
    await _generate(client, card)
    second = (await _generate(client, card)).json()
    assert second["card"]["artworkVersion"] == 2
    assert second["card"]["artworkKey"].endswith("/v2.png")
    assert second["card"]["name"] == card["name"]
    assert second["quota"]["remaining"] == 8


async def test_description_override_is_used(
    client: httpx.AsyncClient, provider: FakeProvider, card: dict
):
    await _generate(client, card, description="a purple turtle with a rocket shell")
    assert "purple turtle" in provider.prompts[0]


async def test_replay_does_not_generate_or_charge_twice(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider, card: dict
):
    key = str(uuid.uuid4())
    first = await _generate(client, card, key)
    replay = await _generate(client, card, key)
    assert first.status_code == replay.status_code == 200
    assert replay.json()["status"] == "SUCCEEDED"
    assert replay.json()["card"]["artworkVersion"] == 1
    assert provider.calls == 1
    assert await _used(services) == 1


async def test_concurrent_duplicates_call_the_provider_once(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider, card: dict
):
    provider.delay = 0.3
    key = str(uuid.uuid4())
    responses = await asyncio.gather(*(_generate(client, card, key) for _ in range(4)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 202, 202, 202]  # one did the work, the rest saw it in flight
    assert provider.calls == 1
    assert await _used(services) == 1
    status = await client.get(f"/api/artwork/requests/{key}")
    assert status.json()["status"] == "SUCCEEDED"


async def test_key_reused_for_another_card_is_rejected(client: httpx.AsyncClient, card: dict):
    key = str(uuid.uuid4())
    await _generate(client, card, key)
    other = (await client.post("/api/profiles/kid1/cards", json={"description": "a cat"})).json()
    response = await _generate(client, other, key)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_missing_idempotency_key_is_422(client: httpx.AsyncClient, card: dict):
    response = await client.post(_url(card))
    assert response.status_code == 422


async def test_validation_failures_do_not_use_quota(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider
):
    blank = (await client.post("/api/profiles/kid1/cards", json={"name": "Blank"})).json()
    response = await _generate(client, blank)
    assert response.status_code == 400
    missing = {"profileId": "kid1", "cardId": str(uuid.uuid4())}
    assert (await _generate(client, missing)).status_code == 404
    too_long = await _generate(client, blank, description="x" * 500)
    assert too_long.status_code == 400
    assert provider.calls == 0
    assert await _used(services) == 0


async def test_quota_exhausted_returns_429(
    client: httpx.AsyncClient, services: Services, card: dict
):
    for _ in range(10):
        assert (await _generate(client, card)).status_code == 200
    response = await _generate(client, card)
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "QUOTA_EXCEEDED"
    assert error["details"]["quota"]["remaining"] == 0


@pytest.mark.parametrize(
    ("mode", "status_code", "code"),
    [("rejected", 422, "ARTWORK_REJECTED"), ("failed", 502, "ARTWORK_PROVIDER_ERROR")],
)
async def test_definite_provider_failures_release_the_slot(
    client: httpx.AsyncClient,
    services: Services,
    provider: FakeProvider,
    card: dict,
    mode: str,
    status_code: int,
    code: str,
):
    provider.mode = mode
    key = str(uuid.uuid4())
    response = await _generate(client, card, key)
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    record = await _record(services, key)
    assert record["status"] == GenerationStatus.FAILED
    assert record["reservation"] == ReservationStatus.RELEASED
    assert await _used(services) == 0

    # Replaying a failed request reports the same failure without retrying.
    replay = await _generate(client, card, key)
    assert replay.status_code == status_code
    assert provider.calls == 1


async def test_ambiguous_failure_consumes_the_slot_and_is_not_retried(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider, card: dict
):
    provider.mode = "ambiguous"
    key = str(uuid.uuid4())
    response = await _generate(client, card, key)
    assert response.status_code == 504
    record = await _record(services, key)
    assert record["status"] == GenerationStatus.FAILED
    assert record["reservation"] == ReservationStatus.CONSUMED
    assert await _used(services) == 1
    assert provider.calls == 1


async def test_timeout_consumes_the_slot(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider, card: dict, settings
):
    services.artwork._settings = settings.model_copy(update={"openai_timeout_seconds": 0.2})
    provider.mode = "hang"
    key = str(uuid.uuid4())
    response = await _generate(client, card, key)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "ARTWORK_TIMEOUT"
    record = await _record(services, key)
    assert record["reservation"] == ReservationStatus.CONSUMED
    assert record["error"]["code"] == "ARTWORK_TIMEOUT"


async def test_cancellation_during_provider_call_is_recorded(
    services: Services, provider: FakeProvider, card: dict
):
    provider.mode = "hang"
    key = uuid.uuid4()
    task = asyncio.create_task(services.artwork.generate("kid1", uuid.UUID(card["cardId"]), key))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # CancelledError must propagate, not be swallowed
    record = await _record(services, str(key))
    assert record["status"] == GenerationStatus.FAILED
    assert record["reservation"] == ReservationStatus.CONSUMED
    assert record["error"]["code"] == "CANCELLED"


async def test_storage_failure_is_retried_then_recorded(
    client: httpx.AsyncClient, services: Services, card: dict, monkeypatch
):
    attempts = 0

    async def broken_put(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("S3 unavailable")

    monkeypatch.setattr(services.storage, "put", broken_put)
    key = str(uuid.uuid4())
    response = await _generate(client, card, key)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == ArtworkStorageError.code
    assert attempts == 3
    record = await _record(services, key)
    assert record["reservation"] == ReservationStatus.CONSUMED  # the image was paid for
    card_now = (await client.get(f"/api/profiles/kid1/cards/{card['cardId']}")).json()
    assert card_now["artworkKey"] is None


async def test_card_deleted_mid_generation(
    client: httpx.AsyncClient, services: Services, provider: FakeProvider, card: dict
):
    provider.delay = 0.3
    key = str(uuid.uuid4())
    generation = asyncio.create_task(_generate(client, card, key))
    await provider.started.wait()
    await client.delete(f"/api/profiles/kid1/cards/{card['cardId']}")
    response = await generation
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CARD_NOT_FOUND"
    record = await _record(services, key)
    assert record["reservation"] == ReservationStatus.CONSUMED


async def test_older_generation_does_not_overwrite_newer_artwork(services: Services, card: dict):
    repo = services.artwork._card_repo
    outcome, _ = await repo.attach_artwork("kid1", card["cardId"], "k/v2.png", 2, "now")
    assert outcome == "attached"
    outcome, current = await repo.attach_artwork("kid1", card["cardId"], "k/v1.png", 1, "now")
    assert outcome == "superseded"
    assert current["artworkKey"] == "k/v2.png"


async def test_stale_pending_request_is_released_on_read(
    client: httpx.AsyncClient, services: Services, clock, card: dict
):
    # Simulate a Lambda that reserved quota and then died before calling OpenAI.
    key = str(uuid.uuid4())
    record = {
        "requestId": key,
        "familyId": "belchers",
        "profileId": "kid1",
        "cardId": card["cardId"],
        "status": "PENDING",
        "reservation": "RESERVED",
        "quotaDate": "2026-10-13",
        "createdAt": "2026-10-13T15:00:00.000Z",
        "updatedAt": "2026-10-13T15:00:00.000Z",
    }
    await services.artwork._quota_repo.reserve(record, date(2026, 10, 13), 10, 0)
    assert await _used(services) == 1

    assert (await client.get(f"/api/artwork/requests/{key}")).json()["status"] == "PENDING"
    clock.advance(minutes=10)
    response = (await client.get(f"/api/artwork/requests/{key}")).json()
    assert response["status"] == "FAILED"
    assert response["reservation"] == "RELEASED"
    assert await _used(services) == 0


async def test_reconcile_settles_stuck_provider_calls(services: Services, clock, card: dict):
    key = str(uuid.uuid4())
    record = {
        "requestId": key,
        "familyId": "belchers",
        "profileId": "kid1",
        "cardId": card["cardId"],
        "status": "CALLING_PROVIDER",
        "reservation": "RESERVED",
        "quotaDate": "2026-10-13",
        "createdAt": "2026-10-13T15:00:00.000Z",
        "updatedAt": "2026-10-13T15:00:00.000Z",
    }
    await services.artwork._quota_repo.reserve(record, date(2026, 10, 13), 10, 0)
    clock.advance(minutes=10)
    settled = await services.artwork.reconcile_stale()
    assert [r.request_id for r in settled] == [key]
    assert settled[0].reservation == ReservationStatus.CONSUMED
    assert await _used(services) == 1


async def test_unknown_request_is_404(client: httpx.AsyncClient, services: Services):
    response = await client.get(f"/api/artwork/requests/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_deleting_a_card_removes_its_artwork(
    client: httpx.AsyncClient, services: Services, card: dict
):
    key = (await _generate(client, card)).json()["card"]["artworkKey"]
    assert await services.storage.exists(key)
    await client.delete(f"/api/profiles/kid1/cards/{card['cardId']}")
    assert not await services.storage.exists(key)


async def test_duplicate_copies_artwork_independently(
    client: httpx.AsyncClient, services: Services, card: dict
):
    await _generate(client, card)
    copy = (await client.post(f"/api/profiles/kid1/cards/{card['cardId']}/duplicate")).json()
    assert copy["artworkKey"].startswith(f"families/belchers/profiles/kid1/cards/{copy['cardId']}/")
    await client.delete(f"/api/profiles/kid1/cards/{card['cardId']}")
    assert await services.storage.exists(copy["artworkKey"])
