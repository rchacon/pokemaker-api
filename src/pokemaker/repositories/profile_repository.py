from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from pokemaker.integrations.dynamodb import from_item, is_condition_failed, to_item, to_value
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

    async def next_card_number(self, profile_id: str) -> int:
        """Atomically allocate the next card number for a profile.

        `ADD` is applied atomically by DynamoDB, so two concurrent creates can
        never get the same number, however many processes are running. If the
        card write that follows fails, the number is simply skipped.
        """
        response = await self._client.update_item(
            TableName=self._table,
            Key={"PK": {"S": self._pk}, "SK": {"S": keys.profile_sk(profile_id)}},
            UpdateExpression="ADD cardCounter :one",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":one": {"N": "1"}},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["cardCounter"]["N"])

    # --- PINs ---------------------------------------------------------------------

    def _key(self, profile_id: str) -> dict[str, Any]:
        return {"PK": {"S": self._pk}, "SK": {"S": keys.profile_sk(profile_id)}}

    async def take_pin_attempt(self, profile_id: str, now: int) -> int | None:
        """Atomically count one PIN guess; return the running count, or None if locked.

        Counting *before* checking the PIN means simultaneous guesses each get a
        distinct number (1, 2, 3, ...), so a burst of parallel requests can't
        sneak past the limit the way a read-then-write check could.
        """
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key=self._key(profile_id),
                UpdateExpression="ADD pinAttempts :one",
                ConditionExpression=(
                    "attribute_exists(PK) AND "
                    "(attribute_not_exists(pinLockedUntil) OR pinLockedUntil <= :now)"
                ),
                ExpressionAttributeValues={":one": to_value(1), ":now": to_value(now)},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return None
            raise
        return int(response["Attributes"]["pinAttempts"]["N"])

    async def lock_pin(self, profile_id: str, until: int) -> None:
        await self._client.update_item(
            TableName=self._table,
            Key=self._key(profile_id),
            UpdateExpression="SET pinLockedUntil = :until, pinAttempts = :zero",
            ExpressionAttributeValues={":until": to_value(until), ":zero": to_value(0)},
        )

    async def reset_pin_attempts(self, profile_id: str) -> None:
        await self._client.update_item(
            TableName=self._table,
            Key=self._key(profile_id),
            UpdateExpression="SET pinAttempts = :zero REMOVE pinLockedUntil",
            ExpressionAttributeValues={":zero": to_value(0)},
        )

    async def set_pin(
        self, profile_id: str, pin_hash: str | None, expected_version: int
    ) -> int | None:
        """Set (or with None, remove) a PIN; returns the new pinVersion.

        Conditional on the version the caller was authorized against, so two
        people setting a PIN at the same moment can't both "win" — the second
        gets None and must unlock again. Bumping the version also invalidates
        every unlock token issued for the old PIN.
        """
        values: dict[str, Any] = {
            ":zero": to_value(0),
            ":one": to_value(1),
            ":expected": to_value(expected_version),
        }
        # A profile that never had a PIN has no pinVersion attribute yet.
        version_check = "pinVersion = :expected"
        if expected_version == 0:
            version_check = "attribute_not_exists(pinVersion) OR pinVersion = :expected"
        if pin_hash is None:
            update = "SET pinAttempts = :zero ADD pinVersion :one REMOVE pinLockedUntil, pinHash"
        else:
            update = (
                "SET pinAttempts = :zero, pinHash = :hash ADD pinVersion :one REMOVE pinLockedUntil"
            )
            values[":hash"] = to_value(pin_hash)
        try:
            response = await self._client.update_item(
                TableName=self._table,
                Key=self._key(profile_id),
                UpdateExpression=update,
                ConditionExpression=f"attribute_exists(PK) AND ({version_check})",
                ExpressionAttributeValues=values,
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return None
            raise
        return int(response["Attributes"]["pinVersion"]["N"])

    async def set_role(self, profile_id: str, role: str) -> bool:
        try:
            await self._client.update_item(
                TableName=self._table,
                Key=self._key(profile_id),
                UpdateExpression="SET #role = :role",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#role": "role"},
                ExpressionAttributeValues={":role": to_value(role)},
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return False
            raise
        return True
