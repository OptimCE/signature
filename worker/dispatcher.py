"""NATS consumers and the per-message ack / nak / dead-letter matrix.

Three durables:
- ``worker-signature-requests`` on ``signature.request.>`` (the domains' create
  and cancel, work queue);
- ``worker-signature-jobs`` on ``signature.job.>`` (prepare, sign, complete;
  work queue, internal);
- ``worker-signature-docgen`` on ``docgen.result.signature`` (the evidence
  render; the stream belongs to document-generation, attached with retries).

Failure classes:
- **permanent** (a malformed body, a slot the document lacks, a revision that
  does not validate) → the request goes FAILED, the domain hears ``failed``,
  ack;
- **transient** (storage, TSA, database, broker) → nak with a growing delay;
  after the last delivery the request goes FAILED and the message is parked
  on ``signature.dlq.<consumer>``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from functools import cache
from typing import Any

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig

from core import metrics
from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from core.queue.helper import Event, subscribe_durable
from crypto.pki import SealMaterial, load_seal_material
from ports.bus import NatsBus
from ports.storage import S3Store
from service import evidence_report, jobs, requests
from service.failure import PermanentJobError, fail_request
from shared.const import (
    SUBJECT_DLQ_PREFIX,
    SUBJECT_JOB_COMPLETE,
    SUBJECT_JOB_PREPARE,
    SUBJECT_JOB_SIGN,
    SUBJECT_REQUEST_CANCEL,
    SUBJECT_REQUEST_CREATE,
    FailureCode,
)

logger = logging.getLogger(__name__)

REQUESTS_DURABLE = "worker-signature-requests"
JOBS_DURABLE = "worker-signature-jobs"
DOCGEN_DURABLE = "worker-signature-docgen"

# The spec's bound: 4 deliveries, then FAILED and the dead-letter stream.
JOB_MAX_DELIVER = 4
REQUEST_MAX_DELIVER = 5
_ACK_WAIT_SECONDS = 120
_NAK_BASE_DELAY_SECONDS = 15

_ATTACH_RETRY_BASE_DELAY_SECONDS = 1
_ATTACH_RETRY_MAX_DELAY_SECONDS = 30
_ATTACH_RETRY_MAX_ATTEMPTS = 40


@cache
def seal_material() -> SealMaterial:
    """Loaded on first use and kept: the PKCS#12 does not change under a running worker."""
    return load_seal_material(
        pkcs12_path=settings.SEAL_PKCS12_PATH,
        passphrase=settings.SEAL_PKCS12_PASSPHRASE,
        ca_chain_paths=settings.SEAL_CA_CHAIN_PATHS,
        trust_root_paths=settings.SEAL_TRUST_ROOT_PATHS,
        tsa_urls=settings.TSA_URLS,
        tsa_timeout=settings.TSA_TIMEOUT_SECONDS,
    )


async def subscribe_all(js: JetStreamContext, *, inflight: set[asyncio.Task] | None = None) -> list:
    bus = NatsBus(js)
    store = S3Store()
    subs = [
        await subscribe_durable(
            js,
            subject="signature.request.>",
            durable=REQUESTS_DURABLE,
            cb=_handler(
                lambda msg: process_request_message(msg, js=js, bus=bus, store=store), inflight
            ),
            config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=REQUEST_MAX_DELIVER + 1),
        ),
        await subscribe_durable(
            js,
            subject="signature.job.>",
            durable=JOBS_DURABLE,
            cb=_handler(
                lambda msg: process_job_message(msg, js=js, bus=bus, store=store), inflight
            ),
            config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=JOB_MAX_DELIVER + 1),
        ),
    ]
    logger.info("Subscribed the requests and jobs consumers")

    async def _attach_docgen() -> Any:
        return await subscribe_durable(
            js,
            subject=settings.DOCGEN_RESULT_SUBJECT,
            durable=DOCGEN_DURABLE,
            cb=_handler(
                lambda msg: process_docgen_message(msg, js=js, bus=bus, store=store), inflight
            ),
            config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=JOB_MAX_DELIVER + 1),
        )

    try:
        subs.append(await _attach_docgen())
        logger.info("Subscribed the docgen results consumer")
    except Exception as exc:
        logger.warning("docgen results not ready (%s); retrying attach in the background", exc)
        _spawn_attach_retry("docgen results", _attach_docgen, inflight=inflight)
    return subs


def _handler(
    process: Callable[[Msg], Coroutine[Any, Any, None]], inflight: set[asyncio.Task] | None
) -> Callable[[Msg], Awaitable[None]]:
    def _on_done(task: asyncio.Task) -> None:
        if inflight is not None:
            inflight.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("handler task crashed: %r", task.exception())

    async def handle(msg: Msg) -> None:
        if inflight is None:
            await process(msg)
            return
        task: asyncio.Task[None] = asyncio.create_task(process(msg))
        inflight.add(task)
        task.add_done_callback(_on_done)

    return handle


def _spawn_attach_retry(
    name: str, attach: Callable[[], Awaitable[Any]], *, inflight: set[asyncio.Task] | None
) -> None:
    if inflight is None:
        return

    async def _retry() -> None:
        for attempt in range(1, _ATTACH_RETRY_MAX_ATTEMPTS + 1):
            await asyncio.sleep(
                min(
                    _ATTACH_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                    _ATTACH_RETRY_MAX_DELAY_SECONDS,
                )
            )
            try:
                await attach()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("%s attach retry %d failed: %s", name, attempt, exc)
                continue
            logger.info("%s attached after %d background retr(y/ies)", name, attempt)
            return
        logger.warning("%s still unavailable after %d retries", name, _ATTACH_RETRY_MAX_ATTEMPTS)

    task: asyncio.Task[None] = asyncio.create_task(
        _retry(), name=f"{name.replace(' ', '-')}-attach"
    )
    inflight.add(task)
    task.add_done_callback(inflight.discard)


async def _to_dlq(js: JetStreamContext, consumer: str, msg: Msg) -> None:
    try:
        await js.publish(
            f"{SUBJECT_DLQ_PREFIX}.{consumer}", msg.data, headers={"dlq-origin": msg.subject}
        )
    except Exception:
        logger.exception("failed to park a message on the dead-letter stream")


def _delivered(msg: Msg) -> int:
    try:
        return int(msg.metadata.num_delivered)
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


async def process_request_message(msg: Msg, *, js: JetStreamContext, bus: Any, store: Any) -> None:
    try:
        event = Event.decode(msg.data)
        data = event.data if isinstance(event.data, dict) else {}
    except Exception:
        logger.exception("undecodable message on %s; acked and dropped", msg.subject)
        metrics.worker_messages.add(1, {"consumer": "requests", "outcome": "undecodable"})
        await msg.ack()
        return
    try:
        async with AsyncSessionLocalFactory() as local, AsyncSessionCRMFactory() as crm:
            if msg.subject == SUBJECT_REQUEST_CREATE:
                await requests.handle_create(data, local=local, crm=crm, store=store, bus=bus)
            elif msg.subject == SUBJECT_REQUEST_CANCEL:
                await requests.handle_cancel(data, local=local, crm=crm, bus=bus)
            else:
                logger.warning("unknown request subject %s; dropped", msg.subject)
    except Exception as exc:
        if _delivered(msg) >= REQUEST_MAX_DELIVER:
            logger.error("request message exhausted its retries (%s); parked", exc)
            await _to_dlq(js, "requests", msg)
            await msg.ack()
        else:
            logger.warning("request message failed (%s); retrying", exc)
            await msg.nak(delay=_NAK_BASE_DELAY_SECONDS * _delivered(msg))
        metrics.worker_messages.add(1, {"consumer": "requests", "outcome": "transient"})
        return
    metrics.worker_messages.add(1, {"consumer": "requests", "outcome": "ok"})
    await msg.ack()


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


async def process_job_message(msg: Msg, *, js: JetStreamContext, bus: Any, store: Any) -> None:
    try:
        data = Event.decode(msg.data).data
        request_id = uuid.UUID(str(data["request_id"]))
        signer_id = uuid.UUID(str(data["signer_id"])) if msg.subject == SUBJECT_JOB_SIGN else None
    except Exception:
        logger.exception("undecodable job on %s; acked and dropped", msg.subject)
        await msg.ack()
        return
    await run_job(
        msg.subject,
        request_id,
        signer_id,
        bus=bus,
        store=store,
        delivered=_delivered(msg),
        ack=msg.ack,
        nak=msg.nak,
        park=lambda: _to_dlq(js, "jobs", msg),
    )


async def run_job(
    subject: str,
    request_id: uuid.UUID,
    signer_id: uuid.UUID | None,
    *,
    bus: Any,
    store: Any,
    delivered: int,
    ack: Callable[[], Awaitable[None]],
    nak: Callable[..., Awaitable[None]],
    park: Callable[[], Awaitable[None]],
    material: Callable[[], SealMaterial] = seal_material,
) -> str:
    """One job, with its failure handling. Returns the outcome (for tests and metrics)."""
    async with AsyncSessionLocalFactory() as local, AsyncSessionCRMFactory() as crm:
        try:
            if subject == SUBJECT_JOB_PREPARE:
                await jobs.run_prepare(
                    request_id, local=local, crm=crm, store=store, bus=bus, material=material
                )
            elif subject == SUBJECT_JOB_SIGN and signer_id is not None:
                await jobs.run_sign(
                    request_id,
                    signer_id,
                    local=local,
                    crm=crm,
                    store=store,
                    bus=bus,
                    material=material,
                )
            elif subject == SUBJECT_JOB_COMPLETE:
                await jobs.run_complete(
                    request_id, local=local, crm=crm, store=store, bus=bus, material=material
                )
            else:
                await ack()
                return "dropped"
        except PermanentJobError as exc:
            logger.warning("request %s failed permanently: %s", request_id, exc)
            await fail_request(request_id, exc.code, exc.detail, local=local, crm=crm, bus=bus)
            await ack()
            outcome = "failed"
        except Exception as exc:
            if delivered >= JOB_MAX_DELIVER:
                logger.error("request %s: %s exhausted its retries (%s)", request_id, subject, exc)
                await fail_request(
                    request_id,
                    FailureCode.SEAL_FAILED,
                    f"retries exhausted: {type(exc).__name__}: {exc}",
                    local=local,
                    crm=crm,
                    bus=bus,
                )
                await park()
                await ack()
                outcome = "dead_lettered"
            else:
                logger.warning("request %s: %s failed (%s); retrying", request_id, subject, exc)
                await nak(delay=_NAK_BASE_DELAY_SECONDS * delivered)
                outcome = "retry"
        else:
            await ack()
            outcome = "ok"
    metrics.worker_messages.add(1, {"consumer": "jobs", "outcome": outcome})
    return outcome


# ---------------------------------------------------------------------------
# docgen results
# ---------------------------------------------------------------------------


async def process_docgen_message(msg: Msg, *, js: JetStreamContext, bus: Any, store: Any) -> None:
    import json

    try:
        body = json.loads(msg.data)
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except Exception:
        logger.exception("undecodable docgen result; acked and dropped")
        await msg.ack()
        return
    async with AsyncSessionLocalFactory() as local, AsyncSessionCRMFactory() as crm:
        try:
            outcome = await evidence_report.handle_docgen_result(
                body, local=local, crm=crm, store=store, bus=bus, material=seal_material
            )
        except Exception as exc:
            if _delivered(msg) >= JOB_MAX_DELIVER:
                logger.error("evidence result exhausted its retries (%s); parked", exc)
                await _to_dlq(js, "docgen_result", msg)
                await msg.ack()
            else:
                await msg.nak(delay=_NAK_BASE_DELAY_SECONDS * _delivered(msg))
            metrics.worker_messages.add(1, {"consumer": "docgen", "outcome": "transient"})
            return
    metrics.worker_messages.add(1, {"consumer": "docgen", "outcome": outcome})
    await msg.ack()
