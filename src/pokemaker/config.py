"""Application settings, loaded from environment variables (and `.env` locally).

Everything that affects cost — image model, size, quality, daily limits — lives here
and is controlled by the server. The frontend can never choose these values.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Allowlists keep a misconfigured environment from silently selecting an
# expensive model, size or quality.
ALLOWED_IMAGE_MODELS = frozenset(
    {"gpt-image-1-mini", "gpt-image-1", "gpt-image-1.5", "gpt-image-2"}
)
ALLOWED_IMAGE_QUALITIES = frozenset({"low", "medium", "high"})
ALLOWED_IMAGE_SIZES = frozenset({"1024x1024", "1536x1024"})


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

    # --- Edge protection (the server sits behind CloudFront) --------------------
    # CloudFront adds this secret header to every request it forwards; the API
    # rejects requests without it. Unset locally. On the server it comes from SSM.
    origin_verify_secret: SecretStr | None = None
    origin_verify_secret_ssm_parameter: str | None = None
    # Requests per minute per client IP (0 disables). A family shares one IP.
    rate_limit_per_minute: int = Field(default=600, ge=0)
    # Only enable behind a proxy that appends the real client IP (CloudFront).
    trust_forwarded_for: bool = False

    # --- CORS (only needed when the frontend calls the API cross-origin) -------
    cors_allow_origins: list[str] = ["http://localhost:5173"]

    # --- Artwork generation -----------------------------------------------------
    artwork_provider: Literal["mock", "openai"] = "mock"
    mock_artwork_delay_seconds: float = Field(default=1.5, ge=0, le=30)

    openai_api_key: SecretStr | None = None
    # On the server the key is read from SSM Parameter Store (SecureString) at startup
    # instead of living in an environment variable or in Terraform state.
    openai_api_key_ssm_parameter: str | None = None
    openai_image_model: str = "gpt-image-1-mini"
    openai_image_quality: str = "medium"
    openai_image_size: str = "1536x1024"
    openai_timeout_seconds: float = Field(default=90, ge=5, le=600)
    # Rough per-image estimate used only for logging. Check the OpenAI pricing page.
    openai_estimated_cost_usd: float = Field(default=0.02, ge=0)

    daily_generation_limit: int = Field(default=10, ge=0, le=500)
    max_description_length: int = Field(default=400, ge=20, le=1000)
    # Generation records are kept this long for auditing/idempotency, then TTL'd.
    generation_record_ttl_days: int = Field(default=30, ge=1)
    # Quota items are TTL'd for cleanup only — correctness never depends on TTL.
    quota_record_ttl_days: int = Field(default=7, ge=1)

    # --- Profile PINs -------------------------------------------------------------
    # Signs unlock tokens. Locally it comes from .env; on the server from SSM.
    pin_token_secret: SecretStr | None = None
    pin_token_secret_ssm_parameter: str | None = None
    pin_token_ttl_minutes: int = Field(default=120, ge=5, le=24 * 60)
    pin_max_attempts: int = Field(default=5, ge=1, le=20)
    pin_lockout_minutes: int = Field(default=5, ge=1, le=24 * 60)

    # --- Request limits ---------------------------------------------------------
    max_request_body_bytes: int = Field(default=32 * 1024, ge=1024)

    @field_validator("openai_image_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if value not in ALLOWED_IMAGE_MODELS:
            raise ValueError(f"OPENAI_IMAGE_MODEL must be one of {sorted(ALLOWED_IMAGE_MODELS)}")
        return value

    @field_validator("openai_image_quality")
    @classmethod
    def _check_quality(cls, value: str) -> str:
        if value not in ALLOWED_IMAGE_QUALITIES:
            raise ValueError(
                f"OPENAI_IMAGE_QUALITY must be one of {sorted(ALLOWED_IMAGE_QUALITIES)}"
            )
        return value

    @field_validator("openai_image_size")
    @classmethod
    def _check_size(cls, value: str) -> str:
        if value not in ALLOWED_IMAGE_SIZES:
            raise ValueError(f"OPENAI_IMAGE_SIZE must be one of {sorted(ALLOWED_IMAGE_SIZES)}")
        return value

    @property
    def family_pk(self) -> str:
        return f"FAMILY#{self.family_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
