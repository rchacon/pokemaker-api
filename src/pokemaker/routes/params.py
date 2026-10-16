from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Path

ProfileId = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,32}$", description="Child profile id")]
CardId = Annotated[UUID, Path(description="Card UUID")]
