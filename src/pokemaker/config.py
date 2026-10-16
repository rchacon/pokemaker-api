"""Application settings, loaded from environment variables (and `.env` locally)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "dev", "prod"] = "local"
    log_level: str = "INFO"

    # --- Family / tenancy -------------------------------------------------------
    # Required, no default: it selects the DynamoDB partition and S3 prefix for
    # every read and write, so a missing value must fail at startup rather than
    # silently using some family's data.
    family_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")

    # --- AWS --------------------------------------------------------------------
    aws_region: str = "us-east-1"
    table_name: str = "pokemaker"
    artwork_bucket: str = "pokemaker-artwork-local"
    # Local endpoints (DynamoDB Local, MinIO). Leave unset in AWS.
    dynamodb_endpoint_url: str | None = None
    s3_endpoint_url: str | None = None
    # Endpoint used when *presigning* URLs. Locally the browser must reach MinIO
    # through the same host name, so this usually equals s3_endpoint_url.
    s3_public_endpoint_url: str | None = None
    # Credentials used only with the local endpoints above, so local development
    # never needs real AWS credentials. MinIO's root user is set to the same pair.
    local_aws_access_key_id: str = "pokemaker"
    local_aws_secret_access_key: SecretStr = SecretStr("pokemaker-secret")
    presigned_url_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    # --- CORS (only needed when the frontend calls the API cross-origin) -------
    cors_allow_origins: list[str] = ["http://localhost:5173"]

    # --- Profile PINs -------------------------------------------------------------
    # Signs unlock tokens. Required, like FAMILY_ID.
    pin_token_secret: SecretStr | None = None
    pin_token_ttl_minutes: int = Field(default=120, ge=5, le=24 * 60)
    pin_max_attempts: int = Field(default=5, ge=1, le=20)
    pin_lockout_minutes: int = Field(default=5, ge=1, le=24 * 60)

    # --- Request limits ---------------------------------------------------------
    max_request_body_bytes: int = Field(default=32 * 1024, ge=1024)

    @property
    def family_pk(self) -> str:
        return f"FAMILY#{self.family_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
