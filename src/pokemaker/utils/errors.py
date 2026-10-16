"""Domain errors. Services raise these; `pokemaker.main` turns them into JSON responses.

Keeping HTTP status codes next to the error types (rather than in each route)
keeps route handlers thin and error responses consistent:

    {"error": {"code": "CARD_NOT_FOUND", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any


class PokeMakerError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
