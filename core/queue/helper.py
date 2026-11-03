# nats/helper.py
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig
from nats.js.errors import APIError, NotFoundError

logger = logging.getLogger(__name__)


@dataclass
class Event:
    type: str
    data: dict
    version: int = 1

    def encode(self) -> bytes:
        return json.dumps(
            {
                "type": self.type,
                "version": self.version,
                "data": self.data,
            }
        ).encode()

    @classmethod
    def decode(cls, raw: bytes) -> "Event":
        parsed = json.loads(raw)
        return cls(
            type=parsed["type"],
            version=parsed["version"],
            data=parsed["data"],
        )


# Hard cap on a publish round-trip. Without this, a stalled broker would
# block the API request thread until the client's default request timeout
# (≥30 s with reconnects). Five seconds is plenty for a healthy broker
# and short enough to fail fast back to the user with a 500 + FAILED row.
_PUBLISH_TIMEOUT_SECONDS = 5.0


async def send_event(
    js: JetStreamContext,
    subject: str,
    event: Event,
    *,
    timeout: float = _PUBLISH_TIMEOUT_SECONDS,  # noqa: ASYNC109  # forwarded to js.publish(timeout=...)
) -> None:
    ack = await js.publish(
        subject,
        event.encode(),
        headers={"Event-Type": event.type},
        timeout=timeout,
    )
    logger.debug(
        "Published %s to %s (stream=%s, seq=%d)",
        event.type,
        subject,
        ack.stream,
        ack.seq,
    )


# Consumer settings ``subscribe_durable`` keeps in step with the code on a
# durable that already exists. The broker accepts a change to any of them on a
# live consumer (nats-server 2.10).
_RECONCILED_CONSUMER_FIELDS = ("ack_wait", "max_deliver", "max_ack_pending", "backoff")


async def subscribe_durable(
    js: JetStreamContext,
    *,
    subject: str,
    durable: str,
    cb: Callable[[Msg], Awaitable[None]],
    config: ConsumerConfig,
    stream: str | None = None,
) -> JetStreamContext.PushSubscription:
    """Push-subscribe, manual ack, to ``durable`` in a queue group of the same name.

    Use this instead of ``js.subscribe``. nats-py 2.14 applies ``config`` only
    when it creates the durable: on one that already exists it binds with the
    broker's stored config and drops ``config`` without a word. A new
    ``ack_wait``, ``max_deliver`` or ``max_ack_pending`` in code therefore never
    reached a consumer created by an earlier build. This writes those fields onto
    the existing durable first (``ensure_consumer_config``), then subscribes.
    """
    if stream is None:
        stream = await js.find_stream_name_by_subject(subject)
    await ensure_consumer_config(js, stream, durable, config)
    return await js.subscribe(
        subject=subject,
        stream=stream,
        durable=durable,
        queue=durable,
        manual_ack=True,
        cb=cb,
        config=config,
    )


async def ensure_consumer_config(
    js: JetStreamContext, stream: str, durable: str, config: ConsumerConfig
) -> None:
    """Update an existing durable so that every field ``config`` sets matches it.

    A field ``config`` leaves at its default (``None``) keeps the broker's value.
    Setting a field outside ``_RECONCILED_CONSUMER_FIELDS`` raises ``ValueError``:
    it would apply only to a consumer created from scratch. Does nothing when the
    durable does not exist yet, since ``js.subscribe`` then creates it from
    ``config``.
    """
    declared = _declared_consumer_fields(config)
    try:
        info = await js.consumer_info(stream, durable)
    except NotFoundError:
        return
    current = info.config
    drift = {name: value for name, value in declared.items() if getattr(current, name) != value}
    if not drift:
        return
    # Start from the stored config: the deliver subject, queue group and filter
    # cannot change on a push consumer, and must go back exactly as they are.
    try:
        await js.add_consumer(stream, config=current.evolve(**drift))
    except APIError as exc:
        raise RuntimeError(
            f"JetStream consumer {durable!r} on stream {stream!r} could not be updated "
            f"to {drift}. Delete it and let the worker recreate it, e.g. "
            f"`nats consumer rm {stream} {durable}`. Cause: {exc}"
        ) from exc
    logger.warning(
        "Updated JetStream consumer %s/%s in place: %s",
        stream,
        durable,
        ", ".join(f"{name} {getattr(current, name)!r} -> {new!r}" for name, new in drift.items()),
    )


def _declared_consumer_fields(config: ConsumerConfig) -> dict[str, object]:
    defaults = ConsumerConfig()
    declared = {
        f.name: getattr(config, f.name)
        for f in fields(config)
        if getattr(config, f.name) != getattr(defaults, f.name)
    }
    unsupported = sorted(set(declared) - set(_RECONCILED_CONSUMER_FIELDS))
    if unsupported:
        raise ValueError(
            f"ConsumerConfig field(s) {', '.join(unsupported)} would only apply when the "
            "durable is first created; js.subscribe ignores them on an existing one"
        )
    return declared
