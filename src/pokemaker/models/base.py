"""Shared Pydantic configuration and field types."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

_WHITESPACE = re.compile(r"\s+")


def _clean_text(value: str) -> str:
    """Drop control/format characters and collapse whitespace runs."""
    value = "".join(
        ch for ch in value if ch in "\n " or not unicodedata.category(ch).startswith("C")
    )
    return _WHITESPACE.sub(" ", value).strip()


CleanStr = Annotated[str, AfterValidator(_clean_text)]


class ApiModel(BaseModel):
    """camelCase on the wire and in DynamoDB; snake_case in Python."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_max_length=2000,
    )
