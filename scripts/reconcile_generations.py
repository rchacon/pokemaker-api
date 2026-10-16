"""Settle artwork generations left mid-flight by a crashed or timed-out process.

    python scripts/reconcile_generations.py

The API already reconciles a stale request whenever someone reads it, so this is
an occasional sweep, e.g. after an outage. Safe to run at any time: every change
it makes is a conditional write that loses gracefully to a live request.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokemaker.config import get_settings
from pokemaker.dependencies import build_services


async def main() -> None:
    async with build_services(get_settings()) as services:
        settled = await services.artwork.reconcile_stale()
    for record in settled:
        print(f"{record.request_id}: {record.status} / {record.reservation}")
    print(f"settled {len(settled)} stale generation(s)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
