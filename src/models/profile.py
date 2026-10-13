from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.models.base import ApiModel

ProfileRole = Literal["parent", "player"]
Pin = Field(pattern=r"^[0-9]{4}$", description="Exactly four digits")


class Profile(ApiModel):
    profile_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    display_name: str = Field(max_length=40)
    avatar: str = Field(max_length=32)
    # Parents' PINs unlock every profile in the family.
    role: ProfileRole = "player"
    # Whether changes to this profile need its PIN. The hash itself never leaves the server.
    has_pin: bool = False
    created_at: str


class UnlockRequest(ApiModel):
    pin: str = Pin


class SetPinRequest(ApiModel):
    pin: str = Pin


class UnlockResponse(ApiModel):
    profile_id: str
    token: str
    expires_at: str
    # True when a parent's PIN (not the profile's own) unlocked it.
    by_parent: bool = False
