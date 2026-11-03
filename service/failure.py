"""Marking a live request FAILED: a permanent error, or retries exhausted."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from ports.bus import Bus
from service.log import append_event, lock_request, utcnow
from service.notify import Notifier
from service.requests import accepted_data
from shared.const import DomainEvent, EventType, FailureCode, RequestStatus


class PermanentJobError(Exception):
    """No retry can fix it: the request goes FAILED with this code."""

    def __init__(self, code: FailureCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


async def fail_request(
    request_id: uuid.UUID,
    code: FailureCode,
    detail: str,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    bus: Bus,
) -> None:
    """Mark a live request FAILED (permanent error, or retries exhausted)."""
    await local.rollback()
    request = await lock_request(local, request_id)
    if request is None or request.status not in (
        RequestStatus.PREPARING,
        RequestStatus.OPEN,
        RequestStatus.COMPLETING,
    ):
        await local.rollback()
        return
    now = utcnow()
    request.status = int(RequestStatus.FAILED)
    request.closed_at = now
    request.failure_code = code.value
    request.failure_detail = detail[:2000]
    await append_event(
        local,
        request,
        EventType.REQUEST_FAILED,
        payload={"code": code.value, "detail": detail[:500]},
        at=now,
    )
    notifier = Notifier(crm)
    notifier.changed(request, "failed")
    await local.commit()
    await notifier.send_hints()
    metrics.seal_jobs.add(1, {"job": "any", "outcome": code.value})
    await bus.domain_event(
        request.source_service,
        DomainEvent.FAILED,
        {**accepted_data(request), "code": code.value, "detail": detail[:500]},
    )
