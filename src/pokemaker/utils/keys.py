"""DynamoDB key builders for the single `pokemaker` table.

    Profiles        PK = FAMILY#<family>                 SK = PROFILE#<profileId>
    Cards           PK = FAMILY#<family>#PROFILE#<id>    SK = CARD#<uuid>
    Daily quota     PK = FAMILY#<family>                 SK = QUOTA#ARTWORK#<YYYY-MM-DD>
    Generations     PK = FAMILY#<family>                 SK = GENERATION#<requestId>

The family id is our own identifier, never a Cognito `sub`. When Cognito arrives,
`PK = USER#<sub>, SK = FAMILY#<family>` membership items will map users to
families, so none of the keys above need to change.
"""

from __future__ import annotations

from datetime import date


def family_pk(family_id: str) -> str:
    return f"FAMILY#{family_id}"


def profile_sk(profile_id: str) -> str:
    return f"PROFILE#{profile_id}"


def cards_pk(family_id: str, profile_id: str) -> str:
    return f"FAMILY#{family_id}#PROFILE#{profile_id}"


def card_sk(card_id: str) -> str:
    return f"CARD#{card_id}"


def quota_sk(day: date) -> str:
    return f"QUOTA#ARTWORK#{day.isoformat()}"


def generation_sk(request_id: str) -> str:
    return f"GENERATION#{request_id}"


def artwork_prefix(family_id: str, profile_id: str, card_id: str) -> str:
    return f"families/{family_id}/profiles/{profile_id}/cards/{card_id}/artwork/"


def artwork_key(family_id: str, profile_id: str, card_id: str, version: int, ext: str) -> str:
    return f"{artwork_prefix(family_id, profile_id, card_id)}v{version}.{ext}"
