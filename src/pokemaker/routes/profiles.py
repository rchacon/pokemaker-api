from __future__ import annotations

from fastapi import APIRouter

from pokemaker.dependencies import BearerToken, CardServiceDep, PinServiceDep
from pokemaker.models.profile import Profile, SetPinRequest, UnlockRequest, UnlockResponse
from pokemaker.routes.params import ProfileId

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[Profile])
async def list_profiles(cards: CardServiceDep) -> list[Profile]:
    return await cards.list_profiles()


@router.post("/{profile_id}/unlock", response_model=UnlockResponse)
async def unlock_profile(
    profile_id: ProfileId, body: UnlockRequest, pins: PinServiceDep
) -> UnlockResponse:
    """Exchange the profile's PIN (or a parent's PIN) for a short-lived unlock token."""
    return await pins.unlock(profile_id, body.pin)


@router.put("/{profile_id}/pin", response_model=UnlockResponse)
async def set_profile_pin(
    profile_id: ProfileId, body: SetPinRequest, pins: PinServiceDep, token: BearerToken
) -> UnlockResponse:
    """Set the first PIN (open), or change one (needs the profile unlocked)."""
    return await pins.set_pin(profile_id, body.pin, token)
