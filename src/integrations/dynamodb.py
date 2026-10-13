"""Thin async helpers around the DynamoDB low-level client.

We use the client (not the `Table` resource) because conditional writes and
transactions are the heart of this app and the client API exposes them directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.config import Settings
from src.integrations.aws import client_kwargs, get_session

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


@asynccontextmanager
async def dynamodb_client(settings: Settings) -> AsyncIterator[Any]:
    """Yield an aiobotocore DynamoDB client and close its connection pool on exit."""
    async with get_session().client(
        "dynamodb", **client_kwargs(settings, settings.dynamodb_endpoint_url)
    ) as client:
        yield client


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    return {k: _serializer.serialize(v) for k, v in data.items()}


def to_value(value: Any) -> dict[str, Any]:
    return _serializer.serialize(value)


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _plain(_deserializer.deserialize(v)) for k, v in item.items()}


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def is_condition_failed(exc: ClientError) -> bool:
    return error_code(exc) == "ConditionalCheckFailedException"


def cancellation_reasons(exc: ClientError) -> list[str]:
    """Reason code per item of a cancelled transaction, e.g. ["None", "ConditionalCheckFailed"]."""
    reasons = exc.response.get("CancellationReasons") or []
    if reasons:
        return [r.get("Code", "None") for r in reasons]
    # Some emulators only report the reasons in the message:
    #   "... cancelled, please refer cancellation reasons for specific reasons
    #    [None, ConditionalCheckFailed]"
    message = exc.response.get("Error", {}).get("Message", "")
    if "[" in message and message.endswith("]"):
        return [part.strip() for part in message[message.rindex("[") + 1 : -1].split(",")]
    return []
