from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Header, Path

ProfileId = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,32}$", description="Child profile id")]
CardId = Annotated[UUID, Path(description="Card UUID")]
IdempotencyKey = Annotated[
    UUID,
    Header(
        alias="Idempotency-Key",
        description="Client-generated UUID. Re-sending it returns the original "
        "request's result instead of generating (and paying for) another image.",
    ),
]
