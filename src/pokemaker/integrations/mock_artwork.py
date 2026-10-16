"""A free, deterministic stand-in for OpenAI.

It renders a friendly placeholder creature as a PNG: the same description and
type always produce the same picture, so tests and local development are
repeatable. It sleeps briefly to mimic a slow network call, which makes it
useful for exercising timeouts and cancellation locally.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zlib

from pokemaker.integrations.artwork_provider import GeneratedImage

WIDTH, HEIGHT = 384, 256

TYPE_COLORS: dict[str, tuple[int, int, int]] = {
    "grass": (88, 176, 72),
    "fire": (232, 96, 48),
    "water": (56, 136, 224),
    "lightning": (240, 200, 40),
    "psychic": (176, 88, 200),
    "fighting": (184, 104, 56),
    "darkness": (64, 72, 96),
    "metal": (140, 150, 160),
    "dragon": (112, 88, 200),
    "colorless": (200, 196, 184),
}


class MockArtworkProvider:
    name = "mock"

    def __init__(self, delay_seconds: float = 1.5, energy_type: str = "colorless") -> None:
        self._delay = delay_seconds
        self._default_type = energy_type

    async def generate(self, prompt: str, *, seed_text: str = "") -> GeneratedImage:
        # Simulated network latency. Because this is an `await`, a cancelled
        # request or an `asyncio.timeout()` interrupts it right here.
        await asyncio.sleep(self._delay)
        energy_type = next((t for t in TYPE_COLORS if f"theme: {t}" in prompt), self._default_type)
        # Rasterizing ~100k pixels in pure Python is CPU work (~0.1 s). Running it
        # in a worker thread keeps the event loop responsive. It is *not* parallel
        # speed-up: the GIL still serializes Python bytecode.
        data = await asyncio.to_thread(render_placeholder, seed_text or prompt, energy_type)
        return GeneratedImage(data=data, content_type="image/png", extension="png", model="mock")


def render_placeholder(seed_text: str, energy_type: str = "colorless") -> bytes:
    digest = hashlib.sha256(seed_text.encode()).digest()
    base = TYPE_COLORS.get(energy_type, TYPE_COLORS["colorless"])
    body = tuple(min(255, c + 40 + digest[i] % 40) for i, c in enumerate(base))
    cx = WIDTH // 2 + (digest[3] % 60) - 30
    cy = HEIGHT // 2 + 20
    radius = 70 + digest[4] % 30
    eye_dx = radius // 3
    eye_y = cy - radius // 4
    eye_r = 10 + digest[5] % 6

    rows = bytearray()
    for y in range(HEIGHT):
        rows.append(0)  # PNG filter type: none
        shade = y / HEIGHT
        bg = tuple(int(c * (1.15 - 0.45 * shade)) for c in base)
        for x in range(WIDTH):
            dx, dy = x - cx, y - cy
            pixel = bg
            if dx * dx + dy * dy <= radius * radius:
                pixel = body
                for ex in (cx - eye_dx, cx + eye_dx):
                    ddx, ddy = x - ex, y - eye_y
                    d2 = ddx * ddx + ddy * ddy
                    if d2 <= eye_r * eye_r:
                        pixel = (30, 30, 40) if d2 <= (eye_r // 2) ** 2 else (255, 255, 255)
            elif (dx / radius) ** 2 + ((y - cy - radius) / 12) ** 2 <= 1:  # ground shadow
                pixel = tuple(int(c * 0.75) for c in bg)
            rows.extend(min(255, max(0, c)) for c in pixel)
    return _png(WIDTH, HEIGHT, bytes(rows))


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
