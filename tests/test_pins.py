"""Profile PINs: unlocking, protecting changes, parent master PIN, lockout."""

import asyncio

import httpx
import pytest

from pokemaker.utils.security import TokenClaims, hash_pin, make_token, read_token, verify_pin
from scripts.seed_profiles import seed

CARD = {"name": "Pinpup", "description": "a puppy guarding a treasure chest"}


async def _set_pin(client: httpx.AsyncClient, profile_id: str, pin: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.put(f"/api/profiles/{profile_id}/pin", json={"pin": pin}, headers=headers)


async def _unlock(client: httpx.AsyncClient, profile_id: str, pin: str):
    return await client.post(f"/api/profiles/{profile_id}/unlock", json={"pin": pin})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def dad(client: httpx.AsyncClient, settings) -> str:
    """A parent profile with PIN 9999; returns its token."""
    await seed(settings, {"dad": {"displayName": "Dad", "role": "parent"}})
    response = await _set_pin(client, "dad", "9999")
    assert response.status_code == 200, response.text
    return response.json()["token"]


# --- Crypto helpers -------------------------------------------------------------


def test_pin_hashes_are_salted_and_verifiable():
    first, second = hash_pin("1234"), hash_pin("1234")
    assert first != second  # random salt
    assert "1234" not in first
    assert verify_pin("1234", first)
    assert not verify_pin("1235", first)
    assert not verify_pin("1234", "garbage")


def test_tokens_detect_tampering():
    secret = b"x" * 32
    token = make_token(secret, TokenClaims("belchers", "kid1", 1, 2_000_000_000))
    assert read_token(secret, token) == TokenClaims("belchers", "kid1", 1, 2_000_000_000)
    assert read_token(b"y" * 32, token) is None
    forged = make_token(b"y" * 32, TokenClaims("belchers", "kid2", 1, 2_000_000_000))
    assert (
        read_token(
            secret, token.split(".")[0] + "." + forged.split(".")[1] + "." + token.split(".")[2]
        )
        is None
    )
    assert read_token(secret, "not-a-token") is None


# --- Behavior -----------------------------------------------------------------


async def test_profiles_without_a_pin_stay_open(client: httpx.AsyncClient):
    profiles = (await client.get("/api/profiles")).json()
    assert all(p["hasPin"] is False for p in profiles)
    assert all("pinHash" not in p for p in profiles)
    assert (await client.post("/api/profiles/kid1/cards", json=CARD)).status_code == 201


async def test_pin_protects_changes_but_not_viewing(client: httpx.AsyncClient):
    card = (await client.post("/api/profiles/kid1/cards", json=CARD)).json()
    assert (await _set_pin(client, "kid1", "1234")).status_code == 200
    assert next(p for p in (await client.get("/api/profiles")).json() if p["profileId"] == "kid1")[
        "hasPin"
    ]

    # Anyone can still look.
    assert (await client.get("/api/profiles/kid1/cards")).status_code == 200
    assert (await client.get(f"/api/profiles/kid1/cards/{card['cardId']}")).status_code == 200

    # But every kind of change needs the PIN.
    url = f"/api/profiles/kid1/cards/{card['cardId']}"
    attempts = [
        client.post("/api/profiles/kid1/cards", json=CARD),
        client.put(url, json={**CARD, "version": 1}),
        client.delete(url),
        client.post(f"{url}/duplicate"),
        client.post(
            f"{url}/artwork", headers={"Idempotency-Key": "8b0f2c4e-0000-4000-8000-000000000001"}
        ),
        _set_pin(client, "kid1", "0000"),
    ]
    for response in await asyncio.gather(*attempts):
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "PIN_REQUIRED"

    token = (await _unlock(client, "kid1", "1234")).json()["token"]
    response = await client.put(
        url, json={**CARD, "name": "Mine!", "version": 1}, headers=_auth(token)
    )
    assert response.status_code == 200


async def test_one_kid_cannot_use_their_token_on_another(client: httpx.AsyncClient):
    await _set_pin(client, "kid1", "1111")
    kid2_token = (await _set_pin(client, "kid2", "2222")).json()["token"]
    response = await client.post("/api/profiles/kid1/cards", json=CARD, headers=_auth(kid2_token))
    assert response.status_code == 401


async def test_wrong_pin_counts_down_then_locks(client: httpx.AsyncClient, clock):
    await _set_pin(client, "kid1", "1234")
    for left in (4, 3, 2, 1):
        response = await _unlock(client, "kid1", "0000")
        assert response.status_code == 401
        assert response.json()["error"] == {
            "code": "WRONG_PIN",
            "message": "That's not the right PIN.",
            "details": {"attemptsLeft": left},
        }
    response = await _unlock(client, "kid1", "0000")
    assert response.status_code == 423
    assert response.json()["error"]["details"]["retryAfterSeconds"] == 300

    # Even the right PIN waits out the lockout...
    assert (await _unlock(client, "kid1", "1234")).status_code == 423
    clock.advance(minutes=5)
    # ...then works, and resets the counter.
    assert (await _unlock(client, "kid1", "1234")).status_code == 200
    assert (await _unlock(client, "kid1", "0000")).json()["error"]["details"]["attemptsLeft"] == 4


async def test_simultaneous_guesses_cannot_beat_the_limit(client: httpx.AsyncClient):
    await _set_pin(client, "kid1", "1234")
    # 20 guesses at once. The attempt counter is an atomic DynamoDB ADD, so at
    # most 5 are ever checked against the PIN; the rest are refused as locked.
    guesses = await asyncio.gather(*(_unlock(client, "kid1", f"{i:04d}") for i in range(20)))
    codes = [r.status_code for r in guesses]
    assert codes.count(401) <= 5
    assert codes.count(423) >= 15


async def test_parent_pin_unlocks_any_profile_and_resets_a_forgotten_pin(
    client: httpx.AsyncClient, dad: str
):
    await _set_pin(client, "kid1", "1234")
    unlocked = (await _unlock(client, "kid1", "9999")).json()
    assert unlocked["byParent"] is True
    # Dad resets the forgotten PIN; the kid's old PIN stops working.
    assert (await _set_pin(client, "kid1", "4321", unlocked["token"])).status_code == 200
    assert (await _unlock(client, "kid1", "1234")).status_code == 401
    assert (await _unlock(client, "kid1", "4321")).json()["byParent"] is False


async def test_kid_pin_does_not_unlock_the_parent(client: httpx.AsyncClient, dad: str):
    await _set_pin(client, "kid1", "1234")
    assert (await _unlock(client, "dad", "1234")).status_code == 401


async def test_changing_a_pin_signs_out_old_tokens(client: httpx.AsyncClient):
    first = (await _set_pin(client, "kid1", "1234")).json()["token"]
    second = (await _set_pin(client, "kid1", "5678", first)).json()["token"]
    assert (
        await client.post("/api/profiles/kid1/cards", json=CARD, headers=_auth(first))
    ).status_code == 401
    assert (
        await client.post("/api/profiles/kid1/cards", json=CARD, headers=_auth(second))
    ).status_code == 201


async def test_tokens_expire(client: httpx.AsyncClient, clock):
    token = (await _set_pin(client, "kid1", "1234")).json()["token"]
    clock.advance(minutes=121)
    response = await client.post("/api/profiles/kid1/cards", json=CARD, headers=_auth(token))
    assert response.status_code == 401


async def test_first_pin_race_has_one_winner(client: httpx.AsyncClient):
    # Two siblings try to claim an unprotected profile at the same instant.
    results = await asyncio.gather(
        _set_pin(client, "kid2", "1111"), _set_pin(client, "kid2", "2222")
    )
    assert sorted(r.status_code for r in results) == [200, 401]


@pytest.mark.parametrize("pin", ["123", "12345", "abcd", "12 4"])
async def test_pins_are_exactly_four_digits(client: httpx.AsyncClient, pin: str):
    assert (await _set_pin(client, "kid1", pin)).status_code == 422


async def test_seed_can_promote_an_existing_profile_to_parent(client: httpx.AsyncClient, settings):
    await seed(settings, {"kid1": {"role": "parent"}})
    roles = {p["profileId"]: p["role"] for p in (await client.get("/api/profiles")).json()}
    assert roles == {"kid1": "parent", "kid2": "player"}
