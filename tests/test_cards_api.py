import asyncio

import httpx
import pytest
from pydantic import ValidationError

from scripts.seed_profiles import layla_card_id, seed


async def test_lists_seeded_profiles(client: httpx.AsyncClient):
    response = await client.get("/api/profiles")
    assert response.status_code == 200
    assert [p["profileId"] for p in response.json()] == ["kid1", "kid2"]


async def test_seeded_sample_card_is_layla(client: httpx.AsyncClient, settings):
    response = await client.get(
        f"/api/profiles/kid1/cards/{layla_card_id(settings.family_id, 'kid1')}"
    )
    card = response.json()
    assert card["name"] == "Layla"
    assert card["type"] == "colorless"
    assert card["hp"] == 90
    assert [(a["name"], a["damage"]) for a in card["attacks"]] == [("Jump", 30), ("Play Bite", 60)]
    assert "blue collar" in card["description"]
    assert card["artworkUrl"] is None


async def test_create_assigns_number_and_defaults(client: httpx.AsyncClient, card: dict):
    assert card["cardNumber"] == 2  # Layla is #1
    assert card["version"] == 1
    assert card["status"] == "complete"
    assert card["retreatCost"] == 1


async def test_card_numbers_are_unique_under_concurrency(client: httpx.AsyncClient):
    responses = await asyncio.gather(
        *(client.post("/api/profiles/kid2/cards", json={"name": f"C{i}"}) for i in range(8))
    )
    numbers = sorted(r.json()["cardNumber"] for r in responses)
    assert numbers == list(range(2, 10))


async def test_collections_are_separate_per_profile(client: httpx.AsyncClient, card: dict):
    kid1 = {c["cardId"] for c in (await client.get("/api/profiles/kid1/cards")).json()}
    kid2 = {c["cardId"] for c in (await client.get("/api/profiles/kid2/cards")).json()}
    assert card["cardId"] in kid1
    assert card["cardId"] not in kid2
    response = await client.get(f"/api/profiles/kid2/cards/{card['cardId']}")
    assert response.status_code == 404


async def test_unknown_profile_is_404(client: httpx.AsyncClient):
    for request in (
        client.get("/api/profiles/stranger/cards"),
        client.post("/api/profiles/stranger/cards", json={}),
    ):
        response = await request
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROFILE_NOT_FOUND"


async def test_invalid_profile_id_format_is_422(client: httpx.AsyncClient):
    response = await client.get("/api/profiles/BAD%20ID/cards")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_update_and_optimistic_concurrency(client: httpx.AsyncClient, card: dict):
    url = f"/api/profiles/kid1/cards/{card['cardId']}"
    first = await client.put(url, json={"name": "Sparkpup EX", "hp": 120, "version": 1})
    assert first.status_code == 200
    assert first.json()["version"] == 2
    assert first.json()["hp"] == 120

    # A second tab still holding version 1 must not silently overwrite version 2.
    stale = await client.put(url, json={"name": "Old name", "version": 1})
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"]["current"]["name"] == "Sparkpup EX"
    assert (await client.get(url)).json()["name"] == "Sparkpup EX"


async def test_concurrent_updates_with_same_version_only_one_wins(
    client: httpx.AsyncClient, card: dict
):
    url = f"/api/profiles/kid1/cards/{card['cardId']}"
    responses = await asyncio.gather(
        *(client.put(url, json={"name": f"Tab {i}", "version": 1}) for i in range(5))
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]


async def test_update_missing_card_is_404(client: httpx.AsyncClient):
    url = "/api/profiles/kid1/cards/00000000-0000-4000-8000-000000000000"
    response = await client.put(url, json={"version": 1})
    assert response.status_code == 404


async def test_delete_card(client: httpx.AsyncClient, card: dict):
    url = f"/api/profiles/kid1/cards/{card['cardId']}"
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404
    assert (await client.delete(url)).status_code == 404


async def test_duplicate_copies_fields_but_gets_new_identity(client: httpx.AsyncClient, card: dict):
    response = await client.post(f"/api/profiles/kid1/cards/{card['cardId']}/duplicate")
    assert response.status_code == 201
    copy = response.json()
    assert copy["cardId"] != card["cardId"]
    assert copy["cardNumber"] == card["cardNumber"] + 1
    assert copy["name"] == "Sparkpup Copy"
    assert copy["attacks"] == card["attacks"]


async def test_oversized_body_is_rejected(client: httpx.AsyncClient):
    response = await client.post(
        "/api/profiles/kid1/cards",
        content=b'{"name": "' + b"x" * 40_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_seed_adds_a_profile_with_default_avatar(client: httpx.AsyncClient, settings):
    # e.g. python scripts/seed_profiles.py --name dad="Dad"
    await seed(settings, {"dad": {"displayName": "Dad"}})
    profiles = {p["profileId"]: p for p in (await client.get("/api/profiles")).json()}
    assert set(profiles) == {"dad", "kid1", "kid2"}
    assert profiles["dad"]["displayName"] == "Dad"
    assert profiles["dad"]["avatar"] == "star"
    cards = (await client.get("/api/profiles/dad/cards")).json()
    assert [c["name"] for c in cards] == ["Layla"]


async def test_seed_rejects_invalid_profile_ids_before_writing(client: httpx.AsyncClient, settings):
    with pytest.raises(ValidationError):
        await seed(settings, {"Dad": {"displayName": "Dad"}})
    assert len((await client.get("/api/profiles")).json()) == 2


async def test_create_card_needs_no_separate_profile_lookup(
    client: httpx.AsyncClient, services, monkeypatch
):
    # Allocating the card number already fails for a missing profile, so
    # creating a card skips the extra GetItem round trip.
    async def no_lookup(profile_id):
        raise AssertionError("create_card should not call require_profile")

    monkeypatch.setattr(services.cards, "require_profile", no_lookup)
    assert (
        await client.post("/api/profiles/kid1/cards", json={"name": "Quick"})
    ).status_code == 201
    missing = await client.post("/api/profiles/stranger/cards", json={})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PROFILE_NOT_FOUND"
