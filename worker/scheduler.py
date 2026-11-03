"""The worker's periodic passes: expiry every SWEEP_INTERVAL_SECONDS, requeue every two minutes.

Both are idempotent and lock each request row they touch, so a second replica
running them at the same time does no harm.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from ports.bus import Bus
from service.sweep import expire_due, requeue_stuck

logger = logging.getLogger(__name__)

REQUEUE_INTERVAL_SECONDS = 120


async def _every(
    seconds: int, shutdown: asyncio.Event, name: str, run: Callable[[], Awaitable[None]]
) -> None:
    while not shutdown.is_set():
        try:
            await run()
        except Exception:
            logger.exception("%s pass failed; next one in %ds", name, seconds)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=seconds)


async def run_scheduler(shutdown: asyncio.Event, *, bus: Bus) -> None:
    if not settings.SWEEP_ENABLED:
        logger.info("the expiry and requeue passes are off (SWEEP_ENABLED=false)")
        return

    async def expire() -> None:
        async with AsyncSessionLocalFactory() as local, AsyncSessionCRMFactory() as crm:
            expired = await expire_due(local=local, crm=crm, bus=bus)
            if expired:
                logger.info("expired %d request(s)", len(expired))

    async def requeue() -> None:
        async with AsyncSessionLocalFactory() as local:
            await requeue_stuck(local=local, bus=bus)

    await asyncio.gather(
        _every(settings.SWEEP_INTERVAL_SECONDS, shutdown, "expiry", expire),
        _every(REQUEUE_INTERVAL_SECONDS, shutdown, "requeue", requeue),
    )
