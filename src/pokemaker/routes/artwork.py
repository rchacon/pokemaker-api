from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from pokemaker.dependencies import ArtworkServiceDep, QuotaServiceDep, RequireUnlocked
from pokemaker.models.artwork import ArtworkGenerateRequest, GenerationResponse
from pokemaker.models.quota import QuotaStatus
from pokemaker.routes.params import CardId, IdempotencyKey, ProfileId

router = APIRouter(tags=["artwork"])


@router.post(
    "/api/profiles/{profile_id}/cards/{card_id}/artwork",
    response_model=GenerationResponse,
    responses={202: {"model": GenerationResponse, "description": "Still generating"}},
    dependencies=[RequireUnlocked],
)
async def generate_artwork(
    profile_id: ProfileId,
    card_id: CardId,
    idempotency_key: IdempotencyKey,
    response: Response,
    artwork: ArtworkServiceDep,
    body: ArtworkGenerateRequest | None = None,
) -> GenerationResponse:
    result = await artwork.generate(
        profile_id, card_id, idempotency_key, body.description if body else None
    )
    if not result.status.is_terminal:
        # A replay of a request that is still running elsewhere.
        response.status_code = status.HTTP_202_ACCEPTED
    return result


@router.get("/api/artwork/requests/{request_id}", response_model=GenerationResponse)
async def get_generation(request_id: UUID, artwork: ArtworkServiceDep) -> GenerationResponse:
    return await artwork.get_request(request_id)


@router.get("/api/artwork/quota", response_model=QuotaStatus)
async def get_quota(quota: QuotaServiceDep) -> QuotaStatus:
    return await quota.get_status()
