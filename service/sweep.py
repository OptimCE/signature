"""The worker's periodic passes: expiry (hourly) and requeue (every few minutes).

**Expiry**: a request still PREPARING or OPEN past ``expires_at`` becomes
EXPIRED and its domain hears ``expired``.

**Requeue**: every publish happens after the commit that decided it, so a
broker outage between the two leaves a request waiting for a job nobody will
run. This pass republishes what each waiting state needs, once it has waited
longer than a job normally takes; the jobs ignore what is no longer theirs,
so a duplicate is harmless.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ports.bus import Bus, complete_job, prepare_job, sign_job
from service.log import append_event, lock_request, utcnow
from service.notify import Notifier
from service.requests import accepted_data
from shared.const import DomainEvent, EventType, RequestStatus, SignerStatus
from shared.models.local_models import SignatureRequestModel, SignatureSignerModel

logger = logging.getLogger(__name__)

PREPARE_GRACE = datetime.timedelta(minutes=5)
SIGN_GRACE = datetime.timedelta(minutes=2)
COMPLETE_GRACE = datetime.timedelta(minutes=10)


async def expire_due(
    *, local: AsyncSession, crm: AsyncSession, bus: Bus, now: datetime.datetime | None = None
) -> list[uuid.UUID]:
    now = now or utcnow()
    due = list(
        (
            await local.execute(
                select(SignatureRequestModel.id).where(
                    SignatureRequestModel.status.in_(
                        [int(RequestStatus.PREPARING), int(RequestStatus.OPEN)]
                    ),
                    SignatureRequestModel.expires_at < now,
                )
            )
        ).scalars()
    )
    await local.rollback()
    expired: list[uuid.UUID] = []
    for request_id in due:
        request = await lock_request(local, request_id)
        if (
            request is None
            or request.status not in (RequestStatus.PREPARING, RequestStatus.OPEN)
            or request.expires_at >= now
        ):
            await local.rollback()
            continue
        request.status = int(RequestStatus.EXPIRED)
        request.closed_at = now
        await append_event(local, request, EventType.REQUEST_EXPIRED, at=now)
        notifier = Notifier(crm)
        notifier.changed(request, "failed")
        await local.commit()
        await notifier.send_hints()
        expired.append(request.id)
        try:
            await bus.domain_event(
                request.source_service, DomainEvent.EXPIRED, accepted_data(request)
            )
        except Exception:
            logger.exception(
                "could not tell %s that request %s expired", request.source_service, request.id
            )
    return expired


async def requeue_stuck(
    *, local: AsyncSession, bus: Bus, now: datetime.datetime | None = None
) -> list[tuple[str, str]]:
    now = now or utcnow()
    jobs: list[tuple[str, dict[str, str]]] = []
    for request_id in (
        await local.execute(
            select(SignatureRequestModel.id).where(
                SignatureRequestModel.status == int(RequestStatus.PREPARING),
                SignatureRequestModel.updated_at < now - PREPARE_GRACE,
            )
        )
    ).scalars():
        jobs.append(prepare_job(request_id))
    for request_id, signer_id in (
        await local.execute(
            select(SignatureSignerModel.request_id, SignatureSignerModel.id)
            .join(
                SignatureRequestModel, SignatureRequestModel.id == SignatureSignerModel.request_id
            )
            .where(
                SignatureRequestModel.status == int(RequestStatus.OPEN),
                SignatureSignerModel.status == int(SignerStatus.SIGNING),
                SignatureSignerModel.updated_at < now - SIGN_GRACE,
            )
        )
    ).all():
        jobs.append(sign_job(request_id, signer_id))
    for request_id in (
        await local.execute(
            select(SignatureRequestModel.id).where(
                SignatureRequestModel.status == int(RequestStatus.COMPLETING),
                SignatureRequestModel.updated_at < now - COMPLETE_GRACE,
            )
        )
    ).scalars():
        jobs.append(complete_job(request_id))
    await local.rollback()
    sent: list[tuple[str, str]] = []
    for subject, data in jobs:
        try:
            await bus.job(subject, data)
            sent.append((subject, data["request_id"]))
        except Exception:
            logger.exception("requeue of %s %s failed; next pass", subject, data)
            break
    if sent:
        logger.warning("requeued %d waiting job(s): %s", len(sent), sent)
    return sent
