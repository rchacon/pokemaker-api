from __future__ import annotations

from pydantic import Field

from pokemaker.models.base import ApiModel


class Profile(ApiModel):
    profile_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    display_name: str = Field(max_length=40)
    avatar: str = Field(max_length=32)
    created_at: str
