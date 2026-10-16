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
    # Required, no default: it selects the DynamoDB partition for every read and
    # write, so a missing value must fail at startup rather than
    # silently using some family's data.
    family_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")

    # --- AWS --------------------------------------------------------------------
    aws_region: str = "us-east-1"
    table_name: str = "pokemaker"
    # Local endpoint (DynamoDB Local). Leave unset in AWS.
    dynamodb_endpoint_url: str | None = None
    # Credentials used only with the local endpoint above, so local development
    # never needs real AWS credentials.
    local_aws_access_key_id: str = "pokemaker"
    local_aws_secret_access_key: SecretStr = SecretStr("pokemaker-secret")

    # --- CORS (only needed when the frontend calls the API cross-origin) -------
    cors_allow_origins: list[str] = ["http://localhost:5173"]

    @property
    def family_pk(self) -> str:
        return f"FAMILY#{self.family_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
