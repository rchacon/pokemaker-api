from __future__ import annotations

from fastapi import APIRouter, Response, status

from pokemaker.dependencies import CardServiceDep
from pokemaker.models.card import Card, CardCreate, CardUpdate
from pokemaker.routes.params import CardId, ProfileId

router = APIRouter(prefix="/api/profiles/{profile_id}/cards", tags=["cards"])


@router.get("", response_model=list[Card])
async def list_cards(profile_id: ProfileId, cards: CardServiceDep) -> list[Card]:
    return await cards.list_cards(profile_id)


@router.post("", response_model=Card, status_code=status.HTTP_201_CREATED)
async def create_card(profile_id: ProfileId, body: CardCreate, cards: CardServiceDep) -> Card:
    return await cards.create_card(profile_id, body)


@router.get("/{card_id}", response_model=Card)
async def get_card(profile_id: ProfileId, card_id: CardId, cards: CardServiceDep) -> Card:
    return await cards.get_card(profile_id, card_id)


@router.put("/{card_id}", response_model=Card)
async def update_card(
    profile_id: ProfileId, card_id: CardId, body: CardUpdate, cards: CardServiceDep
) -> Card:
    return await cards.update_card(profile_id, card_id, body)


@router.delete("/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card(profile_id: ProfileId, card_id: CardId, cards: CardServiceDep) -> Response:
    await cards.delete_card(profile_id, card_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{card_id}/duplicate",
    response_model=Card,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_card(profile_id: ProfileId, card_id: CardId, cards: CardServiceDep) -> Card:
    return await cards.duplicate_card(profile_id, card_id)
