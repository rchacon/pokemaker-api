"""Profile PINs: unlocking, setting, and checking unlock tokens.

This keeps siblings out of each other's collections. It is not real security:
there are no user accounts, and a 4-digit PIN only resists guessing because
the API limits attempts. Anyone with the API's address can still read cards.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pokemaker.config import Settings
from pokemaker.models.profile import UnlockResponse
from pokemaker.repositories.profile_repository import ProfileRepository
from pokemaker.utils.clock import Clock, iso, utc_now
from pokemaker.utils.errors import (
    PinRequiredError,
    ProfileLockedError,
    ProfileNotFoundError,
    WrongPinError,
)
from pokemaker.utils.security import TokenClaims, hash_pin, make_token, read_token, verify_pin

logger = logging.getLogger(__name__)


class PinService:
    def __init__(
        self, profiles: ProfileRepository, settings: Settings, secret: bytes, clock: Clock = utc_now
    ) -> None:
        self._profiles = profiles
        self._family_id = settings.family_id
        self._secret = secret
        self._ttl = timedelta(minutes=settings.pin_token_ttl_minutes)
        self._max_attempts = settings.pin_max_attempts
        self._lockout = timedelta(minutes=settings.pin_lockout_minutes)
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock().timestamp())

    async def _profile(self, profile_id: str) -> dict[str, Any]:
        profile = await self._profiles.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"No profile named {profile_id!r}")
        return profile

    async def authorize(self, profile_id: str, token: str | None) -> dict[str, Any]:
        """Allow a change to `profile_id`, or raise PinRequiredError.

        Profiles without a PIN are open. Otherwise the token must be correctly
        signed, unexpired, for this family and profile, and issued for the
        profile's current PIN (pinVersion) — so changing a PIN logs out every
        device that had unlocked it.
        """
        profile = await self._profile(profile_id)
        if not profile.get("pinHash"):
            return profile
        claims = read_token(self._secret, token) if token else None
        if (
            claims is None
            or claims.family_id != self._family_id
            or claims.profile_id != profile_id
            or claims.pin_version != int(profile.get("pinVersion", 0))
            or claims.expires_at <= self._now()
        ):
            raise PinRequiredError("Enter the PIN to change these cards.")
        return profile

    async def unlock(self, profile_id: str, pin: str) -> UnlockResponse:
        profile = await self._profile(profile_id)
        if not profile.get("pinHash"):
            return self._issue(profile, by_parent=False)  # nothing to unlock

        now = self._now()
        attempt = await self._profiles.take_pin_attempt(profile_id, now)
        if attempt is None or attempt > self._max_attempts:
            await self._raise_locked(profile_id, now)

        # The profile's own PIN, plus every parent's PIN (parents unlock any profile).
        parents = [
            p
            for p in await self._profiles.list_profiles()
            if p.get("role") == "parent" and p.get("pinHash") and p["profileId"] != profile_id
        ]
        hashes = [profile["pinHash"], *(p["pinHash"] for p in parents)]
        # scrypt is CPU work that releases the GIL: checking the candidates in
        # worker threads keeps the event loop free and runs them in parallel.
        matches = await asyncio.gather(*(asyncio.to_thread(verify_pin, pin, h) for h in hashes))

        if any(matches):
            await self._profiles.reset_pin_attempts(profile_id)
            by_parent = not matches[0]
            logger.info(
                "profile unlocked", extra={"profile_id": profile_id, "by_parent": by_parent}
            )
            return self._issue(profile, by_parent=by_parent)

        logger.info("wrong pin", extra={"profile_id": profile_id, "attempt": attempt})
        if attempt >= self._max_attempts:
            await self._profiles.lock_pin(profile_id, now + int(self._lockout.total_seconds()))
            await self._raise_locked(profile_id, now)
        raise WrongPinError(
            "That's not the right PIN.",
            details={"attemptsLeft": self._max_attempts - attempt},
        )

    async def set_pin(self, profile_id: str, pin: str | None, token: str | None) -> UnlockResponse:
        """Set, change or (with None) remove a profile's PIN.

        Setting the first PIN needs no token; changing one needs the profile
        unlocked — by its own PIN, or by a parent's PIN to reset a forgotten one.
        """
        profile = await self.authorize(profile_id, token)
        pin_hash = await asyncio.to_thread(hash_pin, pin) if pin is not None else None
        version = await self._profiles.set_pin(
            profile_id, pin_hash, expected_version=int(profile.get("pinVersion", 0))
        )
        if version is None:
            raise PinRequiredError("The PIN was just changed somewhere else. Unlock and try again.")
        return self._issue(profile | {"pinVersion": version}, by_parent=False)

    def _issue(self, profile: dict[str, Any], *, by_parent: bool) -> UnlockResponse:
        expires = self._clock() + self._ttl
        token = make_token(
            self._secret,
            TokenClaims(
                family_id=self._family_id,
                profile_id=profile["profileId"],
                pin_version=int(profile.get("pinVersion", 0)),
                expires_at=int(expires.timestamp()),
            ),
        )
        return UnlockResponse(
            profile_id=profile["profileId"],
            token=token,
            expires_at=iso(expires),
            by_parent=by_parent,
        )

    async def _raise_locked(self, profile_id: str, now: int) -> None:
        profile = await self._profile(profile_id)
        until = int(profile.get("pinLockedUntil", now))
        raise ProfileLockedError(
            "Too many wrong PINs. Take a break and try again soon!",
            details={
                "retryAfterSeconds": max(1, until - now),
                "lockedUntil": iso(datetime.fromtimestamp(until, UTC)),
            },
        )
