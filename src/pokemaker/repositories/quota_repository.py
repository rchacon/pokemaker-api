"""Daily artwork quota and generation-request records.

Both live in the family partition and are always changed together inside
DynamoDB transactions, so they share a repository:

    PK = FAMILY#belchers  SK = QUOTA#ARTWORK#2026-10-13   generationCount, generationLimit, ...
    PK = FAMILY#belchers  SK = GENERATION#<requestId>     status, reservation, ...

Why DynamoDB and not an asyncio.Lock or in-memory counter? Each Lambda instance
(and each Uvicorn worker) has its own memory and event loop; a lock in one is
invisible to the others. DynamoDB conditional writes are evaluated atomically on
the server, so they are correct no matter how many processes race.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from botocore.exceptions import ClientError

from pokemaker.integrations.dynamodb import (
    cancellation_reasons,
    error_code,
    from_item,
    is_condition_failed,
    to_item,
    to_value,
)
from pokemaker.models.artwork import GenerationStatus, ReservationStatus
from pokemaker.utils import keys


class ReserveOutcome(StrEnum):
    RESERVED = "RESERVED"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


class TransactionContention(Exception):
    """DynamoDB cancelled a transaction because another one touched the same items.

    Nothing was written, so the caller may safely retry.
    """


class QuotaRepository:
    def __init__(self, client: Any, table_name: str, family_id: str) -> None:
        self._client = client
        self._table = table_name
        self._pk = keys.family_pk(family_id)

    def _quota_key(self, day: date) -> dict[str, Any]:
        return {"PK": {"S": self._pk}, "SK": {"S": keys.quota_sk(day)}}

    def _generation_key(self, request_id: str) -> dict[str, Any]:
        return {"PK": {"S": self._pk}, "SK": {"S": keys.generation_sk(request_id)}}

    # --- Quota -------------------------------------------------------------------

    async def get_usage(self, day: date) -> int:
        response = await self._client.get_item(
            TableName=self._table, Key=self._quota_key(day), ConsistentRead=True
        )
        item = response.get("Item")
        return int(from_item(item).get("generationCount", 0)) if item else 0

    async def reserve(
        self, record: dict[str, Any], day: date, limit: int, quota_expires_at: int
    ) -> ReserveOutcome:
        """Atomically create the generation record AND take one quota slot.

        One transaction, two conditions:
          1. the request id is new  -> otherwise it's a duplicate (idempotent replay)
          2. today's count < limit  -> otherwise the quota is exhausted
        Either both writes happen or neither does, so a quota slot can never be
        taken without a record that explains it, and vice versa.
        """
        try:
            await self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": to_item(
                                {
                                    "PK": self._pk,
                                    "SK": keys.generation_sk(record["requestId"]),
                                    "entity": "GENERATION",
                                }
                                | record
                            ),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": self._quota_key(day),
                            "UpdateExpression": (
                                "SET generationLimit = :limit, #date = :date, "
                                "expiresAt = if_not_exists(expiresAt, :expires), entity = :entity "
                                "ADD generationCount :one"
                            ),
                            # A missing item means nothing has been used today.
                            "ConditionExpression": (
                                "(attribute_not_exists(generationCount) AND :limit > :zero) "
                                "OR generationCount < :limit"
                            ),
                            "ExpressionAttributeNames": {"#date": "date"},
                            "ExpressionAttributeValues": {
                                ":limit": to_value(limit),
                                ":date": to_value(day.isoformat()),
                                ":expires": to_value(quota_expires_at),
                                ":entity": to_value("QUOTA"),
                                ":one": to_value(1),
                                ":zero": to_value(0),
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if error_code(exc) != "TransactionCanceledException":
                raise
            reasons = cancellation_reasons(exc)
            if reasons and reasons[0] == "ConditionalCheckFailed":
                return ReserveOutcome.DUPLICATE_REQUEST
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                return ReserveOutcome.QUOTA_EXCEEDED
            raise TransactionContention(str(reasons)) from exc
        return ReserveOutcome.RESERVED

    async def release(self, request_id: str, day: date, changes: dict[str, Any]) -> bool:
        """Return a reserved slot to the quota and record why, atomically.

        The condition `reservation = RESERVED` makes this safe to call twice:
        the second call is cancelled instead of giving back a second slot.
        Returns False if the reservation was not (or no longer) RESERVED.
        """
        names, values, sets = _set_clause(changes | {"reservation": ReservationStatus.RELEASED})
        try:
            await self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": self._generation_key(request_id),
                            "UpdateExpression": sets,
                            "ConditionExpression": "#reservation = :reserved",
                            "ExpressionAttributeNames": names | {"#reservation": "reservation"},
                            "ExpressionAttributeValues": values
                            | {":reserved": to_value(ReservationStatus.RESERVED.value)},
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": self._quota_key(day),
                            "UpdateExpression": "ADD generationCount :minus_one",
                            "ConditionExpression": "generationCount > :zero",
                            "ExpressionAttributeValues": {
                                ":minus_one": to_value(-1),
                                ":zero": to_value(0),
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if error_code(exc) != "TransactionCanceledException":
                raise
            reasons = cancellation_reasons(exc)
            if "TransactionConflict" in reasons:
                raise TransactionContention(str(reasons)) from exc
            return False
        return True

    # --- Generation records ------------------------------------------------------

    async def get_generation(self, request_id: str) -> dict[str, Any] | None:
        response = await self._client.get_item(
            TableName=self._table, Key=self._generation_key(request_id), ConsistentRead=True
        )
        item = response.get("Item")
        return from_item(item) if item else None

    async def list_generations(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("query")
        async for page in paginator.paginate(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": {"S": self._pk}, ":prefix": {"S": "GENERATION#"}},
        ):
            items.extend(from_item(item) for item in page.get("Items", []))
        return items

    async def transition(
        self, request_id: str, expected: GenerationStatus, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Move a generation record forward only if it is still in `expected` state.

        The condition turns the state machine into a compare-and-swap: if a
        reconciler (or a duplicate worker) already moved the record, this update
        fails instead of rewinding it. Returns the new record, or None on mismatch.
        """
        names, values, sets = _set_clause(changes)
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key=self._generation_key(request_id),
                UpdateExpression=sets,
                ConditionExpression="#status = :expected",
                ExpressionAttributeNames=names | {"#status": "status"},
                ExpressionAttributeValues=values | {":expected": to_value(expected.value)},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return None
            raise
        return from_item(response["Attributes"])


def _set_clause(changes: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], str]:
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    parts: list[str] = []
    for i, (attr, value) in enumerate(changes.items()):
        if hasattr(value, "value") and isinstance(value.value, str):  # StrEnum
            value = value.value
        names[f"#c{i}"] = attr
        values[f":c{i}"] = to_value(value)
        parts.append(f"#c{i} = :c{i}")
    return names, values, "SET " + ", ".join(parts)
