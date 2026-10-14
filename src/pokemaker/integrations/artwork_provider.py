"""The artwork-provider interface shared by the OpenAI and mock implementations.

Providers translate their own failures into three outcomes, because the quota
policy depends on *what we know* about whether a paid image was produced:

* `ProviderRejectedError` — the provider refused the prompt (e.g. safety system).
  Definitely no image; the quota reservation is released.
* `ProviderFailedError` — the provider answered with an error response.
  No image was delivered; the reservation is released.
* `ProviderAmbiguousError` — timeout or dropped connection after the request may
  have been sent. The provider may have generated (and billed) an image we never
  received. The reservation is consumed and we do NOT retry automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    content_type: str
    extension: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class ProviderRejectedError(Exception):
    pass


class ProviderFailedError(Exception):
    pass


class ProviderAmbiguousError(Exception):
    pass


class ArtworkProvider(Protocol):
    name: str

    async def generate(self, prompt: str, *, seed_text: str = "") -> GeneratedImage: ...
