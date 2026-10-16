from __future__ import annotations

from fastapi import APIRouter

from pokemaker.dependencies import CardServiceDep
from pokemaker.models.profile import Profile

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[Profile])
async def list_profiles(cards: CardServiceDep) -> list[Profile]:
    return await cards.list_profiles()
