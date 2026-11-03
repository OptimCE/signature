"""Worker process entry point.

Bootstraps logging + tracing, connects to NATS JetStream (which ensures this
service's streams exist), subscribes the durable consumers, and runs until
SIGINT/SIGTERM — then drains in-flight work and disposes resources cleanly.

The seal operations are CPU-bound (pyHanko): they run in worker threads
(``asyncio.to_thread``), so the event loop keeps acking and heartbeating.

Run locally:

    python -m worker.main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import signal
import sys

from core.database.database import crm_engine, local_engine
from core.logging import configure_logging
from core.queue.init import close_nats, get_jetstream, init_nats
from core.realtime import log_realtime_state
from core.tracing import setup_tracer_provider
from ports.bus import NatsBus
from worker import dispatcher
from worker.scheduler import run_scheduler

logger = logging.getLogger(__name__)

# Bounded retry on NATS connect so a slow-to-start broker does not crash the
# container immediately (~10 minutes of exponential backoff, then give up and let
# the orchestrator recreate it).
_NATS_CONNECT_MAX_ATTEMPTS = 10
_NATS_CONNECT_BASE_DELAY_SECONDS = 2
_NATS_CONNECT_MAX_DELAY_SECONDS = 30

_HEARTBEAT_INTERVAL_SECONDS = 15

# Touched every interval while the event loop is alive. The container HEALTHCHECK
# reads this file's mtime to tell a live worker from a hung one.
_HEARTBEAT_PATH = pathlib.Path("/tmp/worker.alive")  # noqa: S108 — dedicated container, non-root user

# How long shutdown waits for in-flight handlers to finish and ack. Stragglers
# redeliver after ack_wait, which the idempotency guards make safe, so this is
# bounded to stay under a typical SIGTERM grace period.
_INFLIGHT_DRAIN_TIMEOUT_SECONDS = 25


async def _connect_nats_with_retry() -> None:
    for attempt in range(1, _NATS_CONNECT_MAX_ATTEMPTS + 1):
        try:
            await init_nats()
            return
        except Exception as exc:
            if attempt == _NATS_CONNECT_MAX_ATTEMPTS:
                logger.error(
                    "NATS connect attempt %d/%d failed: %s; aborting worker startup",
                    attempt,
                    _NATS_CONNECT_MAX_ATTEMPTS,
                    exc,
                )
                raise
            delay = min(
                _NATS_CONNECT_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                _NATS_CONNECT_MAX_DELAY_SECONDS,
            )
            logger.warning(
                "NATS connect attempt %d/%d failed: %s; retrying in %.1fs",
                attempt,
                _NATS_CONNECT_MAX_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def _heartbeat(shutdown_event: asyncio.Event) -> None:
    """Touch the liveness file every interval while the loop is responsive."""
    while not shutdown_event.is_set():
        try:
            await asyncio.to_thread(_HEARTBEAT_PATH.touch)
        except OSError as exc:
            logger.debug("heartbeat touch failed: %s", exc)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue


async def main() -> None:
    configure_logging()
    # Absence of this line means the image predates the realtime feature —
    # see core/realtime/bus.py. Must come after configure_logging().
    log_realtime_state("signature-worker")
    setup_tracer_provider()

    await _connect_nats_with_retry()
    js = get_jetstream()

    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    inflight: set[asyncio.Task] = set()
    subs: list = []
    heartbeat_task: asyncio.Task | None = None
    scheduler_task: asyncio.Task | None = None
    try:
        subs = await dispatcher.subscribe_all(js, inflight=inflight)
        heartbeat_task = asyncio.create_task(_heartbeat(shutdown_event), name="heartbeat")
        # Expiry (hourly) and the requeue of jobs a broker outage lost.
        scheduler_task = asyncio.create_task(
            run_scheduler(shutdown_event, bus=NatsBus(js)), name="sweep-scheduler"
        )

        logger.info("Signature worker ready")
        await shutdown_event.wait()
        logger.info("Shutdown signal received; draining...")
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await scheduler_task

        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

        # Let in-flight handlers finish and ack while NATS is still up.
        if inflight:
            logger.info("Waiting for %d in-flight handler(s) to finish...", len(inflight))
            _done, pending = await asyncio.wait(
                set(inflight), timeout=_INFLIGHT_DRAIN_TIMEOUT_SECONDS
            )
            for task in pending:
                logger.warning("Handler still running after drain timeout; cancelling")
                task.cancel()

        for sub in subs:
            try:
                await sub.drain()
            except Exception:
                logger.exception("Error draining subscription")

        try:
            await close_nats()
        except Exception:
            logger.exception("Error closing NATS connection")

        try:
            await local_engine.dispose()
        except Exception:
            logger.exception("Error disposing local DB engine")

        try:
            await crm_engine.dispose()
        except Exception:
            logger.exception("Error disposing CRM DB engine")

        logger.info("Worker shutdown complete")


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to set ``shutdown_event`` (POSIX + Windows)."""
    loop = asyncio.get_running_loop()

    def _set_event() -> None:
        if not shutdown_event.is_set():
            shutdown_event.set()

    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: _set_event())
        signal.signal(signal.SIGTERM, lambda *_: _set_event())
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_event)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _set_event())


if __name__ == "__main__":
    asyncio.run(main())
