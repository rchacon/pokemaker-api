"""Card models.

Text limits are deliberately tight: they keep cards readable when printed at
2.5 x 3.5 inches, and they bound what reaches the image prompt and DynamoDB.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from src.models.base import ApiModel, CleanStr

SCHEMA_VERSION = 1


class EnergyType(StrEnum):
    GRASS = "grass"
    FIRE = "fire"
    WATER = "water"
    LIGHTNING = "lightning"
    PSYCHIC = "psychic"
    FIGHTING = "fighting"
    DARKNESS = "darkness"
    METAL = "metal"
    DRAGON = "dragon"
    COLORLESS = "colorless"


class Stage(StrEnum):
    BASIC = "basic"
    STAGE1 = "stage1"
    STAGE2 = "stage2"


CardStatus = Literal["draft", "complete"]


Text24 = Annotated[CleanStr, StringConstraints(max_length=24)]
Text120 = Annotated[CleanStr, StringConstraints(max_length=120)]
Text160 = Annotated[CleanStr, StringConstraints(max_length=160)]
Text400 = Annotated[CleanStr, StringConstraints(max_length=400)]


class Attack(ApiModel):
    name: Text24 = ""
    damage: int = Field(default=10, ge=0, le=300)
    cost: list[EnergyType] = Field(default_factory=list, max_length=4)
    effect: Text120 = ""


class CardFields(ApiModel):
    """Everything a child can edit. Shared by create and update requests."""

    name: Text24 = ""
    type: EnergyType = EnergyType.COLORLESS
    hp: int = Field(default=60, ge=10, le=500)
    stage: Stage = Stage.BASIC
    evolves_from: Text24 | None = None
    evolves_from_card_id: UUID | None = None
    description: Text400 = ""
    flavor_text: Text160 = ""
    attacks: list[Attack] = Field(default_factory=list, max_length=2)
    weakness: EnergyType | None = None
    resistance: EnergyType | None = None
    retreat_cost: int = Field(default=1, ge=0, le=4)


class CardCreate(CardFields):
    pass


class CardUpdate(CardFields):
    # The version the client last saw. The update succeeds only if the stored
    # card still has this version (optimistic concurrency).
    version: int = Field(ge=1)


class Card(CardFields):
    card_id: UUID
    profile_id: str
    card_number: int
    artwork_key: str | None = None
    artwork_version: int = 0
    artwork_url: str | None = None  # short-lived presigned URL, never stored
    status: CardStatus = "draft"
    schema_version: int = SCHEMA_VERSION
    version: int = 1
    created_at: str
    updated_at: str


def card_status(fields: CardFields) -> CardStatus:
    """A card is complete once it has a name and at least one named attack."""
    has_attack = any(attack.name for attack in fields.attacks)
    return "complete" if fields.name and has_attack else "draft"
