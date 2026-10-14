from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from pokemaker.integrations.dynamodb import from_item, is_condition_failed, to_item, to_value
from pokemaker.utils import keys

# Attributes that only the service layer may change, never a card update.
_SYSTEM_ATTRIBUTES = frozenset(
    {
        "PK",
        "SK",
        "cardId",
        "profileId",
        "cardNumber",
        "artworkKey",
        "artworkVersion",
        "artworkSeq",
        "createdAt",
        "version",
        "schemaVersion",
    }
)


class VersionMismatch(Exception):
    """Raised when an update's expected version doesn't match the stored one."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("version mismatch")
        self.current = current


class CardRepository:
    def __init__(self, client: Any, table_name: str, family_id: str) -> None:
        self._client = client
        self._table = table_name
        self._family_id = family_id

    def _key(self, profile_id: str, card_id: str) -> dict[str, Any]:
        return {
            "PK": {"S": keys.cards_pk(self._family_id, profile_id)},
            "SK": {"S": keys.card_sk(card_id)},
        }

    async def list_cards(self, profile_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("query")
        async for page in paginator.paginate(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": keys.cards_pk(self._family_id, profile_id)},
                ":prefix": {"S": "CARD#"},
            },
        ):
            items.extend(from_item(item) for item in page.get("Items", []))
        return items

    async def get_card(self, profile_id: str, card_id: str) -> dict[str, Any] | None:
        response = await self._client.get_item(
            TableName=self._table, Key=self._key(profile_id, card_id), ConsistentRead=True
        )
        item = response.get("Item")
        return from_item(item) if item else None

    async def create_card(self, card: dict[str, Any]) -> None:
        item = {
            "PK": keys.cards_pk(self._family_id, card["profileId"]),
            "SK": keys.card_sk(card["cardId"]),
            "entity": "CARD",
        } | card
        await self._client.put_item(
            TableName=self._table,
            Item=to_item(item),
            ConditionExpression="attribute_not_exists(PK)",
        )

    async def update_fields(
        self, profile_id: str, card_id: str, fields: dict[str, Any], expected_version: int
    ) -> dict[str, Any] | None:
        """Overwrite editable fields if the stored version equals `expected_version`.

        Returns the updated card, None if the card doesn't exist, and raises
        `VersionMismatch` (carrying the current card) if someone saved first.

        This is optimistic concurrency: no locks are held. Two tabs autosaving the
        same card race, exactly one conditional write wins, and the loser learns
        about it instead of silently overwriting newer data.
        """
        names: dict[str, str] = {"#version": "version"}
        values: dict[str, Any] = {":expected": to_value(expected_version), ":one": to_value(1)}
        sets = ["#version = #version + :one"]
        for i, (attr, value) in enumerate(fields.items()):
            if attr in _SYSTEM_ATTRIBUTES:
                raise ValueError(f"{attr} cannot be updated directly")
            names[f"#f{i}"] = attr
            values[f":v{i}"] = to_value(value)
            sets.append(f"#f{i} = :v{i}")
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key=self._key(profile_id, card_id),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="attribute_exists(PK) AND #version = :expected",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not is_condition_failed(exc):
                raise
            current = await self.get_card(profile_id, card_id)
            if current is None:
                return None
            raise VersionMismatch(current) from exc
        return from_item(response["Attributes"])

    async def delete_card(self, profile_id: str, card_id: str) -> dict[str, Any] | None:
        response = await self._client.delete_item(
            TableName=self._table, Key=self._key(profile_id, card_id), ReturnValues="ALL_OLD"
        )
        old = response.get("Attributes")
        return from_item(old) if old else None

    async def allocate_artwork_version(self, profile_id: str, card_id: str) -> int | None:
        """Reserve the next artwork version number (v1, v2, ...) for a card.

        Allocating it atomically means two overlapping generations for the same
        card write to different S3 keys instead of clobbering each other.
        Returns None if the card was deleted.
        """
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key=self._key(profile_id, card_id),
                UpdateExpression="ADD artworkSeq :one",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":one": to_value(1)},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return None
            raise
        return int(response["Attributes"]["artworkSeq"]["N"])

    async def attach_artwork(
        self, profile_id: str, card_id: str, key: str, version: int, updated_at: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Point the card at new artwork unless a newer version is already attached.

        Returns ("attached" | "superseded" | "missing", card).

        Deliberately does NOT bump the card's edit `version`: artwork and text
        fields never overwrite each other, so a generation finishing mid-edit
        shouldn't make the child's next autosave fail with a conflict.
        """
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key=self._key(profile_id, card_id),
                UpdateExpression="SET artworkKey = :key, artworkVersion = :v, updatedAt = :now",
                ConditionExpression=(
                    "attribute_exists(PK) AND "
                    "(attribute_not_exists(artworkVersion) OR artworkVersion < :v)"
                ),
                ExpressionAttributeValues={
                    ":key": to_value(key),
                    ":v": to_value(version),
                    ":now": to_value(updated_at),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not is_condition_failed(exc):
                raise
            current = await self.get_card(profile_id, card_id)
            return ("missing", None) if current is None else ("superseded", current)
        return "attached", from_item(response["Attributes"])
