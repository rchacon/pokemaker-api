"""Remove a profile's PIN — the way out when a parent forgets their own PIN.

    python scripts/reset_pin.py dad

(A forgotten kid's PIN doesn't need this: unlock their profile with a parent's
PIN in the app and set a new one.) Removing the PIN also signs out every device
that had the profile unlocked. Works locally or, with AWS credentials, in AWS.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokemaker.config import get_settings
from pokemaker.integrations.dynamodb import dynamodb_client
from pokemaker.repositories.profile_repository import ProfileRepository


async def reset(profile_id: str) -> None:
    settings = get_settings()
    async with dynamodb_client(settings) as ddb:
        profiles = ProfileRepository(ddb, settings.table_name, settings.family_id)
        profile = await profiles.get_profile(profile_id)
        if profile is None:
            sys.exit(f"No profile {profile_id!r} in family {settings.family_id!r}")
        version = await profiles.set_pin(profile_id, None, int(profile.get("pinVersion", 0)))
    print(f"PIN removed for {profile_id}" if version else "Profile changed meanwhile; run again")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile_id")
    asyncio.run(reset(parser.parse_args().profile_id))
