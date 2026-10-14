"""The OpenAI integration, tested against a fake HTTP transport — no network, no charges."""

import base64
import json

import httpx2
import pytest
from openai import AsyncOpenAI

from pokemaker.config import Settings
from pokemaker.integrations.artwork_provider import (
    ProviderAmbiguousError,
    ProviderFailedError,
    ProviderRejectedError,
)
from pokemaker.integrations.openai import OpenAIArtworkProvider, openai_provider

IMAGE = b"RIFF....WEBPVP8 fake"


def _provider(handler) -> tuple[OpenAIArtworkProvider, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    client = AsyncOpenAI(api_key="sk-test", http_client=http, max_retries=0)
    settings = Settings(_env_file=None, family_id="belchers", openai_image_quality="low")  # type: ignore[call-arg]
    return OpenAIArtworkProvider(client, settings), seen


def _ok(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "created": 1,
            "data": [{"b64_json": base64.b64encode(IMAGE).decode()}],
            "usage": {"input_tokens": 50, "output_tokens": 400, "total_tokens": 450},
        },
    )


async def test_success_decodes_image_and_sends_server_controlled_params():
    provider, seen = _provider(_ok)
    image = await provider.generate("a friendly dragon")
    assert image.data == IMAGE
    assert image.content_type == "image/webp"
    assert image.usage["output_tokens"] == 400
    body = json.loads(seen[0].content)
    assert body["model"] == "gpt-image-1-mini"
    assert body["quality"] == "low"
    assert body["size"] == "1536x1024"
    assert body["n"] == 1
    assert body["prompt"] == "a friendly dragon"


async def test_bad_request_is_a_rejection():
    provider, _ = _provider(
        lambda r: httpx2.Response(
            400, json={"error": {"message": "safety system", "type": "invalid_request_error"}}
        )
    )
    with pytest.raises(ProviderRejectedError, match="safety system"):
        await provider.generate("x")


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_error_responses_fail_without_retrying(status: int):
    provider, seen = _provider(
        lambda r: httpx2.Response(status, json={"error": {"message": "nope"}})
    )
    with pytest.raises(ProviderFailedError):
        await provider.generate("x")
    assert len(seen) == 1  # max_retries=0: never pay twice


async def test_timeout_is_ambiguous():
    def hang(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    provider, seen = _provider(hang)
    with pytest.raises(ProviderAmbiguousError):
        await provider.generate("x")
    assert len(seen) == 1


async def test_missing_image_data_is_a_failure():
    provider, _ = _provider(lambda r: httpx2.Response(200, json={"created": 1, "data": []}))
    with pytest.raises(ProviderFailedError):
        await provider.generate("x")


async def test_context_manager_closes_http_client():
    settings = Settings(_env_file=None, family_id="belchers")  # type: ignore[call-arg]
    async with openai_provider(settings, "sk-test") as provider:
        http = provider._client._client
        assert not http.is_closed
        assert provider._client.max_retries == 0
    assert http.is_closed
