"""Create the local DynamoDB table and MinIO bucket, then seed data. Idempotent.

    docker compose up -d
    python scripts/init_local.py

In AWS, Terraform creates the table and bucket; only seed_profiles.py is run there.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import ClientError

from scripts.seed_profiles import seed
from src.config import Settings, get_settings
from src.integrations.aws import client_kwargs, get_session
from src.integrations.dynamodb import dynamodb_client, error_code

logger = logging.getLogger("init")


async def create_table(settings: Settings) -> None:
    async with dynamodb_client(settings) as ddb:
        try:
            await ddb.create_table(
                TableName=settings.table_name,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
            )
            logger.info("table %s created", settings.table_name)
        except ClientError as exc:
            if error_code(exc) != "ResourceInUseException":
                raise
            logger.info("table %s already exists", settings.table_name)
        waiter = ddb.get_waiter("table_exists")
        await waiter.wait(TableName=settings.table_name, WaiterConfig={"Delay": 1})
        try:
            await ddb.update_time_to_live(
                TableName=settings.table_name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
            )
        except ClientError as exc:
            # Already enabled, or unsupported by the emulator: TTL is cleanup only.
            logger.info("TTL not changed: %s", error_code(exc))


async def create_bucket(settings: Settings) -> None:
    async with get_session().client(
        "s3", **client_kwargs(settings, settings.s3_endpoint_url)
    ) as s3:
        try:
            await s3.create_bucket(Bucket=settings.artwork_bucket)
            logger.info("bucket %s created", settings.artwork_bucket)
        except ClientError as exc:
            if error_code(exc) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
            logger.info("bucket %s already exists", settings.artwork_bucket)


async def main() -> None:
    settings = get_settings()
    if not (settings.dynamodb_endpoint_url and settings.s3_endpoint_url):
        sys.exit("init_local.py only runs against local endpoints (see .env.example).")
    # The table and the bucket don't depend on each other: create them concurrently.
    await asyncio.gather(create_table(settings), create_bucket(settings))
    # Seeding needs the table, so it runs after.
    await seed(settings)
    logger.info("local environment ready")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
