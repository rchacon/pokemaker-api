"""Private artwork storage in S3 (MinIO locally)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from botocore.config import Config

from src.config import Settings
from src.integrations.aws import BOTO_CONFIG, client_kwargs, get_session

logger = logging.getLogger(__name__)

# MinIO needs path-style URLs (http://host:9000/bucket/key); AWS prefers
# virtual-hosted ones (https://bucket.s3.amazonaws.com/key).
_AWS_S3_CONFIG = BOTO_CONFIG.merge(Config(signature_version="s3v4"))
_LOCAL_S3_CONFIG = _AWS_S3_CONFIG.merge(Config(s3={"addressing_style": "path"}))


def _s3_kwargs(settings: Settings, endpoint_url: str | None) -> dict[str, Any]:
    config = _LOCAL_S3_CONFIG if endpoint_url else _AWS_S3_CONFIG
    return client_kwargs(settings, endpoint_url) | {"config": config}


class ArtworkStorage:
    def __init__(self, client: Any, presign_client: Any, bucket: str, url_ttl: int) -> None:
        self._client = client
        self._presign_client = presign_client
        self._bucket = bucket
        self._url_ttl = url_ttl

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="private, max-age=31536000, immutable",  # keys are versioned
        )

    async def copy(self, source_key: str, dest_key: str) -> None:
        await self._client.copy_object(
            Bucket=self._bucket,
            Key=dest_key,
            CopySource={"Bucket": self._bucket, "Key": source_key},
        )

    async def delete(self, key: str) -> None:
        await self._client.delete_object(Bucket=self._bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under `prefix` (all artwork versions of a card)."""
        deleted = 0
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                await self._client.delete_objects(
                    Bucket=self._bucket, Delete={"Objects": objects, "Quiet": True}
                )
                deleted += len(objects)
        return deleted

    async def exists(self, key: str) -> bool:
        try:
            await self._client.head_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    async def ping(self) -> None:
        await self._client.head_bucket(Bucket=self._bucket)

    async def presign(self, key: str) -> str:
        # Presigning is local computation (an HMAC), not a network call. It is
        # async in aiobotocore only because credential resolution may be.
        return await self._presign_client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=self._url_ttl
        )

    async def presign_many(self, keys: list[str | None]) -> list[str | None]:
        async def one(key: str | None) -> str | None:
            return await self.presign(key) if key else None

        return list(await asyncio.gather(*(one(k) for k in keys)))


@asynccontextmanager
async def artwork_storage(settings: Settings) -> AsyncIterator[ArtworkStorage]:
    session = get_session()
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            session.client("s3", **_s3_kwargs(settings, settings.s3_endpoint_url))
        )
        presign_client = client
        public = settings.s3_public_endpoint_url
        if public and public != settings.s3_endpoint_url:
            # Locally the API may reach MinIO by a different host name than the
            # browser does; signatures cover the host, so presign with the public one.
            presign_client = await stack.enter_async_context(
                session.client("s3", **_s3_kwargs(settings, public))
            )
        yield ArtworkStorage(
            client, presign_client, settings.artwork_bucket, settings.presigned_url_ttl_seconds
        )
