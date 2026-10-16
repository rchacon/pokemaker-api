from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from pokemaker.integrations.dynamodb import from_item, is_condition_failed, to_item
from pokemaker.utils import keys


class ProfileRepository:
    def __init__(self, client: Any, table_name: str, family_id: str) -> None:
        self._client = client
        self._table = table_name
        self._family_id = family_id
        self._pk = keys.family_pk(family_id)

    async def list_profiles(self) -> list[dict[str, Any]]:
        response = await self._client.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": {"S": self._pk}, ":prefix": {"S": "PROFILE#"}},
        )
        return [from_item(item) for item in response.get("Items", [])]

    async def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        response = await self._client.get_item(
            TableName=self._table,
            Key={"PK": {"S": self._pk}, "SK": {"S": keys.profile_sk(profile_id)}},
        )
        item = response.get("Item")
        return from_item(item) if item else None

    async def create_profile_if_absent(self, profile: dict[str, Any]) -> bool:
        """Insert a profile unless it already exists. Returns True if created."""
        item = {"PK": self._pk, "SK": keys.profile_sk(profile["profileId"]), "entity": "PROFILE"}
        try:
            await self._client.put_item(
                TableName=self._table,
                Item=to_item(item | {"cardCounter": 0} | profile),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return False
            raise
        return True

    async def next_card_number(self, profile_id: str) -> int | None:
        """Atomically allocate the next card number for a profile.

        `ADD` is applied atomically by DynamoDB, so two concurrent creates can
        never get the same number, however many processes are running. If the
        card write that follows fails, the number is simply skipped.

        Returns None if the profile doesn't exist. The condition makes this
        double as the profile check, so creating a card needs no separate
        GetItem round trip.
        """
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key={"PK": {"S": self._pk}, "SK": {"S": keys.profile_sk(profile_id)}},
                UpdateExpression="ADD cardCounter :one",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":one": {"N": "1"}},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return None
            raise
        return int(response["Attributes"]["cardCounter"]["N"])
