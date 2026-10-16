"""Seed the family's child profiles and the sample "Layla" card. Idempotent.

    python scripts/seed_profiles.py
    python scripts/seed_profiles.py --name kid1="Captain Cards" --avatar kid1=dragon
    python scripts/seed_profiles.py --name dad="Dad"

Works locally (against DynamoDB Local) and in AWS (with normal AWS credentials
and no endpoint overrides). Display names are shown on screen only; keep them to
nicknames — the app doesn't need children's real names.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import logging
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pokemaker.config import Settings, get_settings
from pokemaker.integrations.dynamodb import dynamodb_client
from pokemaker.models.card import SCHEMA_VERSION, Attack, CardCreate, EnergyType, Stage, card_status
from pokemaker.models.profile import Profile
from pokemaker.repositories.card_repository import CardRepository
from pokemaker.repositories.profile_repository import ProfileRepository
from pokemaker.utils.clock import iso, utc_now

logger = logging.getLogger("seed")

DEFAULT_PROFILES: dict[str, dict[str, str]] = {
    "kid1": {"displayName": "Trainer 1", "avatar": "dragon"},
    "kid2": {"displayName": "Trainer 2", "avatar": "lightning"},
}

LAYLA = CardCreate(
    name="Layla",
    type=EnergyType.COLORLESS,
    hp=90,
    stage=Stage.BASIC,
    description=(
        "A scruffy terrier mix dog with wiry, tousled fur, bright eyes, a happy grin and a "
        "blue collar (a collar, not a harness)."
    ),
    flavor_text="She can jump higher than the couch and loves a good game of tug.",
    attacks=[
        Attack(name="Jump", damage=30, cost=[EnergyType.COLORLESS]),
        Attack(name="Play Bite", damage=60, cost=[EnergyType.COLORLESS, EnergyType.COLORLESS]),
    ],
    weakness=EnergyType.FIGHTING,
    retreat_cost=1,
)


def layla_card_id(family_id: str, profile_id: str) -> str:
    # Deterministic id, so re-running the seed never creates a second Layla.
    return str(uuid5(NAMESPACE_URL, f"pokemaker:{family_id}:{profile_id}:layla"))


async def seed(settings: Settings, overrides: dict[str, dict[str, str]] | None = None) -> None:
    profiles_data = {pid: dict(data) for pid, data in DEFAULT_PROFILES.items()}
    for pid, data in (overrides or {}).items():
        # New profiles (e.g. --name dad="Dad") get a default name and avatar.
        profiles_data.setdefault(pid, {"displayName": pid.capitalize(), "avatar": "star"}).update(
            data
        )
    # Validate everything before writing anything, so a bad id or name can't
    # leave an item in the table that would break GET /api/profiles.
    now = iso(utc_now())
    validated = {
        pid: Profile(profile_id=pid, created_at=now, **_snake(data)).model_dump(by_alias=True)
        for pid, data in profiles_data.items()
    }

    async with dynamodb_client(settings) as ddb:
        profiles = ProfileRepository(ddb, settings.table_name, settings.family_id)
        cards = CardRepository(ddb, settings.table_name, settings.family_id)

        # Each profile is independent, so seed them concurrently.
        async def seed_one(profile_id: str, profile: dict[str, str]) -> None:
            created = await profiles.create_profile_if_absent(profile)
            logger.info("profile %s %s", profile_id, "created" if created else "already exists")
            await seed_layla(profiles, cards, settings.family_id, profile_id)

        await asyncio.gather(*(seed_one(pid, profile) for pid, profile in validated.items()))


async def seed_layla(
    profiles: ProfileRepository, cards: CardRepository, family_id: str, profile_id: str
) -> None:
    card_id = layla_card_id(family_id, profile_id)
    if await cards.get_card(profile_id, card_id):
        logger.info("sample card for %s already exists", profile_id)
        return
    now = iso(utc_now())
    item: dict[str, Any] = LAYLA.model_dump(mode="json", by_alias=True) | {
        "cardId": card_id,
        "profileId": profile_id,
        "cardNumber": await profiles.next_card_number(profile_id),
        "artworkKey": None,
        "artworkVersion": 0,
        "status": card_status(LAYLA),
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "createdAt": now,
        "updatedAt": now,
    }
    await cards.create_card(item)
    logger.info("sample card Layla created for %s", profile_id)


def _snake(data: dict[str, str]) -> dict[str, str]:
    return {"display_name" if k == "displayName" else k: v for k, v in data.items()}


def _pairs(values: list[str], field: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for value in values:
        profile_id, _, text = value.partition("=")
        result.setdefault(profile_id, {})[field] = text
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", action="append", default=[], help="profileId=Display Name")
    parser.add_argument("--avatar", action="append", default=[], help="profileId=avatar")
    args = parser.parse_args()
    overrides = _pairs(args.name, "displayName")
    for pid, data in _pairs(args.avatar, "avatar").items():
        overrides.setdefault(pid, {}).update(data)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(seed(get_settings(), overrides))


if __name__ == "__main__":
    main()
