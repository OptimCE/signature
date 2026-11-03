"""Fire-and-forget realtime publisher.

One lazily-created ``redis.asyncio`` client per process. Every failure mode —
no configuration, unreachable broker, hung broker, malformed envelope — is a
silent no-op, because the *only* thing lost is UI freshness: crm-backend's
clients refetch authoritative state on every reconnect, and every poller in the
SPA keeps running (slower) as a durability backstop.
"""

import asyncio
import json
import logging
from collections.abc import Mapping

import redis.asyncio as redis

from core.config import settings

from .channels import Audience
from .envelope import build_envelope

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None

#: Publishing must never add latency to a request or a worker tick. A wedged
#: broker is bounded here rather than by the socket, because a *connected but
#: hung* Redis would otherwise await forever.
_PUBLISH_TIMEOUT_SECONDS = 1.0


def _redacted_url() -> str:
    """The DSN with its password removed. NEVER log the raw value.

    ``REALTIME_REDIS_URL`` is composed from ``REDIS_PASSWORD`` in
    docker-compose, so it carries a live secret in userinfo position.
    """
    url = settings.REALTIME_REDIS_URL
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    return f"{scheme}://***@{rest.rpartition('@')[2]}"


def log_realtime_state(component: str) -> None:
    """Announce this process's realtime publishing state, once, at startup.

    *** CALL THIS AFTER configure_logging(). NEVER at module import time. ***

    This module is imported long before logging is configured: ``worker/main.py``
    imports the dispatcher, which reaches ``worker/persistence.py``, which imports
    this file — all in the import block at the top — while ``configure_logging()``
    runs inside ``async def main()``. And ``core/logging.py`` opens with
    ``root.handlers.clear()``. So at import time the root logger has no handlers,
    Python falls back to ``logging.lastResort`` at WARNING, and an INFO line here
    is dropped with no trace whatsoever — reproducing the exact silence this
    function exists to break. Do not "simplify" it into the module body.

    Why it exists: four worker containers once ran images built before this
    package existed. ``core/realtime/`` was absent and so were the emit call
    sites, so three topics were published by nothing at all — for three days,
    while their environment variables looked perfectly correct, because
    ``--force-recreate`` rebuilds the container from the EXISTING image. The
    absence of this line is the cheapest signal that an image predates the
    feature. ``scripts/check-realtime-images.sh`` is the automatable form of the
    same check, and ``scripts/check-realtime-parity.sh`` cannot see it at all —
    it compares source trees, which were green throughout.
    """
    if not settings.REALTIME_ENABLED:
        logger.info("Realtime disabled — %s publishes nothing", component)
        return
    if not settings.REALTIME_REDIS_URL:
        # Enabled but unconfigured is a misconfiguration, not a deployment choice.
        logger.warning(
            "Realtime ENABLED but REALTIME_REDIS_URL is empty — %s publishes nothing", component
        )
        return
    logger.info("Realtime publisher ready — %s publishing to %s", component, _redacted_url())


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.REALTIME_REDIS_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=30,
            decode_responses=False,
        )
    return _client


async def emit(
    *,
    topic: str,
    audience: Audience,
    resource: tuple[str, str | int],
    hint: Mapping[str, str | int | float | bool | None] | None = None,
    scope_community_id: int | None = None,
) -> None:
    """Publish a realtime hint. NEVER raises.

    *** CALL THIS AFTER THE COMMIT. NEVER inside ``begin_nested()``, and never
    inside the ``try`` that owns the business write. ***

    Publishing pre-commit does not merely lose an event — it tells the browser to
    refetch and read PRE-COMMIT state, and because the transport is
    fire-and-forget there is NO second event, ever. The result is a permanently
    stale UI behind a 200, with no error anywhere. That is the same silhouette as
    the sweep-commit-ordering and notification-savepoint traps.

    Note the deliberate asymmetry with ``core.notifications.service.publish()``,
    which MUST run inside the caller's transaction because it writes rows. These
    two have opposite requirements. Do not "unify" them.

    Do not fire this as a bare ``asyncio.create_task`` either: in a worker that
    finishes immediately the task is orphaned (the event is lost anyway) and may
    log after teardown.
    """
    if not settings.REALTIME_ENABLED or not settings.REALTIME_REDIS_URL:
        return  # no-op: the default everywhere realtime is not deployed

    try:
        envelope = build_envelope(
            topic=topic,
            resource=resource,
            hint=hint,
            scope_community_id=scope_community_id,
        )
        if envelope is None:
            logger.warning("realtime: envelope rejected locally topic=%s", topic)
            return

        body = json.dumps(envelope, separators=(",", ":"))
        client = _get_client()
        async with asyncio.timeout(_PUBLISH_TIMEOUT_SECONDS):
            for channel in audience.channels():
                await client.publish(channel, body)
    # Blanket by contract: nothing this function can hit is worth propagating
    # into a caller that has already committed.
    except Exception:
        logger.warning("realtime: emit failed topic=%s", topic, exc_info=True)


async def close() -> None:
    """Release the client. For worker shutdown and test teardown."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.warning("realtime: client close failed", exc_info=True)
        _client = None
