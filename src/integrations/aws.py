"""Shared aioboto3 session and botocore configuration."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import aioboto3
from botocore.config import Config

from src.config import Settings

# Tight timeouts: every DynamoDB/S3 call here should take milliseconds. botocore's
# "standard" retry mode retries throttling and transient errors with backoff.
BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
)


@lru_cache
def get_session() -> aioboto3.Session:
    # Creating a session is cheap, but it owns botocore's loader cache (the parsed
    # service models). Reusing one session keeps warm Lambda invocations fast.
    # This is a cache, not application state: nothing depends on it for correctness.
    return aioboto3.Session()


def client_kwargs(settings: Settings, endpoint_url: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"region_name": settings.aws_region, "config": BOTO_CONFIG}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
        kwargs["aws_access_key_id"] = settings.local_aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.local_aws_secret_access_key.get_secret_value()
    return kwargs
