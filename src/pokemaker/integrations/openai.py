"""OpenAI Images API integration (async).

Why async: an image generation call spends 10-60 s waiting on the network. With
`AsyncOpenAI` that wait doesn't block the event loop, so a local Uvicorn worker
keeps serving autosaves and gallery loads while a generation is in flight.
The server runs one long-lived process, so this is where asyncio pays off:
while one child's artwork is being drawn, the same event loop keeps serving
everyone else's requests.

Note: openai-python 3.x is built on `httpx2`, the successor of `httpx`, so the
HTTP client we hand it is an `httpx2.AsyncClient`.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import openai
from openai import AsyncOpenAI

from pokemaker.config import Settings
from pokemaker.integrations.artwork_provider import (
    GeneratedImage,
    ProviderAmbiguousError,
    ProviderFailedError,
    ProviderRejectedError,
)

logger = logging.getLogger(__name__)


class OpenAIArtworkProvider:
    name = "openai"

    def __init__(self, client: AsyncOpenAI, settings: Settings) -> None:
        self._client = client
        self._model = settings.openai_image_model
        self._quality = settings.openai_image_quality
        self._size = settings.openai_image_size

    async def generate(self, prompt: str, *, seed_text: str = "") -> GeneratedImage:
        try:
            response = await self._client.images.generate(
                model=self._model,
                prompt=prompt,
                n=1,
                size=self._size,
                quality=self._quality,  # type: ignore[arg-type]
                output_format="webp",
                output_compression=85,
                background="opaque",
                moderation="auto",
            )
        except openai.BadRequestError as exc:
            # 400s include safety-system rejections. Nothing was generated.
            raise ProviderRejectedError(_message(exc)) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            # APITimeoutError subclasses APIConnectionError. Either way the request
            # may have reached OpenAI, so we can't know whether it was billed.
            raise ProviderAmbiguousError(str(exc)) from exc
        except openai.APIStatusError as exc:
            # Any other HTTP error response (401, 429, 5xx): OpenAI answered and
            # delivered no image.
            raise ProviderFailedError(f"{exc.status_code}: {_message(exc)}") from exc

        if not response.data or not response.data[0].b64_json:
            raise ProviderFailedError("OpenAI returned no image data")
        usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
        flat_usage = {k: v for k, v in usage.items() if isinstance(v, int)}
        return GeneratedImage(
            data=base64.b64decode(response.data[0].b64_json),
            content_type="image/webp",
            extension="webp",
            model=self._model,
            usage=flat_usage,
        )


def _message(exc: openai.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    return str(body.get("message") or exc.message)


@asynccontextmanager
async def openai_provider(settings: Settings, api_key: str) -> AsyncIterator[OpenAIArtworkProvider]:
    timeout = httpx2.Timeout(settings.openai_timeout_seconds, connect=10.0)
    async with (
        httpx2.AsyncClient(timeout=timeout, limits=httpx2.Limits(max_connections=10)) as http,
        AsyncOpenAI(
            api_key=api_key,
            http_client=http,
            timeout=timeout,
            # Never let the SDK retry: a retry after a timeout could pay for a
            # second image. Retry decisions belong to our generation state machine.
            max_retries=0,
        ) as client,
    ):
        yield OpenAIArtworkProvider(client, settings)
