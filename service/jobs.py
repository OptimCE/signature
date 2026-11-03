"""The seal jobs: prepare, sign, complete (``signature.job.*``), one request at a time.

Each job locks its request row for its whole transaction (``lock_request``):
the signatures of a request build on each other, so two signers whose calls
arrive in the same second are sealed one after the other, never as a lost
update. A job that finds its request no longer waiting for it does nothing,
which makes redelivery safe.

Failures:
- ``SealConfigurationError`` / a storage or TSA hiccup → raised (transient),
  the consumer NAKs and, after the last delivery, ``fail_request`` marks the
  request FAILED and parks the message;
- a slot the document does not have, an original whose digest changed, a
  revision that does not validate → FAILED at once (permanent).

The CRM notifications commit before the local transaction (see
``service/notify.py``), then the realtime hints and the domain's event.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from crypto import seal
from crypto.pki import SealMaterial
from domain import wording
from domain.slots import parse_slot
from ports.bus import Bus, complete_job
from ports.crm_read import CrmRead
from ports.storage import DocumentStore
from service import keys
from service.evidence_report import request_evidence_render
from service.failure import PermanentJobError
from service.log import SYSTEM, append_event, lock_request, utcnow
from service.notify import Notifier
from service.requests import accepted_data
from shared.const import (
    Capacity,
    DomainEvent,
    EventType,
    FailureCode,
    FieldType,
    RequestStatus,
    RevisionKind,
    SignerStatus,
    SigningOrder,
)
from shared.models.local_models import (
    SignatureFieldModel,
    SignatureRequestModel,
    SignatureRevisionModel,
    SignatureSignerModel,
)

logger = logging.getLogger(__name__)

PDF = "application/pdf"


MaterialProvider = Callable[[], SealMaterial]


async def _timed(job: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    started = time.perf_counter()
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    finally:
        metrics.seal_duration.record(time.perf_counter() - started, {"job": job})


async def signers_of(local: AsyncSession, request_id: uuid.UUID) -> list[SignatureSignerModel]:
    result = await local.execute(
        select(SignatureSignerModel)
        .where(SignatureSignerModel.request_id == request_id)
        .order_by(SignatureSignerModel.position)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def fields_of(local: AsyncSession, signer_ids: list[uuid.UUID]) -> list[SignatureFieldModel]:
    if not signer_ids:
        return []
    result = await local.execute(
        select(SignatureFieldModel)
        .where(SignatureFieldModel.signer_id.in_(signer_ids))
        .order_by(SignatureFieldModel.name)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def current_revision(
    local: AsyncSession, request: SignatureRequestModel
) -> SignatureRevisionModel:
    result = await local.execute(
        select(SignatureRevisionModel).where(
            SignatureRevisionModel.request_id == request.id,
            SignatureRevisionModel.number == request.current_revision,
        )
    )
    return result.scalar_one()


async def _load_verified(store: DocumentStore, key: str, sha256: str) -> bytes:
    data = await store.get(key)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise PermanentJobError(FailureCode.SEAL_FAILED, f"{key} no longer matches its digest")
    return data


async def _invite(
    local: AsyncSession,
    request: SignatureRequestModel,
    signers: list[SignatureSignerModel],
    now: datetime.datetime,
) -> list[SignatureSignerModel]:
    """Open the turn of whoever may sign now; returns the newly invited."""
    waiting = [s for s in signers if s.status == SignerStatus.WAITING]
    if not waiting:
        return []
    if request.signing_order == SigningOrder.SEQUENTIAL:
        if any(
            s.status in (SignerStatus.INVITED, SignerStatus.VIEWED, SignerStatus.SIGNING)
            for s in signers
        ):
            return []
        waiting = waiting[:1]
    for signer in waiting:
        signer.status = int(SignerStatus.INVITED)
        signer.invited_at = now
        await append_event(local, request, EventType.SIGNER_INVITED, signer_id=signer.id, at=now)
    return waiting


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


async def run_prepare(
    request_id: uuid.UUID,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
    material: MaterialProvider,
) -> bool:
    request = await lock_request(local, request_id)
    if request is None or request.status != RequestStatus.PREPARING:
        await local.rollback()
        return False
    signers = await signers_of(local, request.id)
    form_fields = await fields_of(local, [s.id for s in signers])
    by_signer: dict[uuid.UUID, list[SignatureFieldModel]] = {}
    for item in form_fields:
        by_signer.setdefault(item.signer_id, []).append(item)
    original = await _load_verified(store, request.original_key, request.original_sha256)
    plans = [
        seal.SignerPlan(
            field_name=signer.field_name,
            slot=parse_slot(signer.slot, for_signature=True),
            label=signer.role_label,
            fields=[
                seal.FieldPlan(
                    f.pdf_field_name, FieldType(f.type), parse_slot(f.slot, for_signature=False)
                )
                for f in by_signer.get(signer.id, [])
            ],
        )
        for signer in signers
    ]
    try:
        prepared = await _timed(
            "prepare",
            seal.prepare,
            original,
            plans,
            material(),
            reason=wording.certification_reason(request.locale),
        )
    except seal.SlotNotFoundError as exc:
        raise PermanentJobError(FailureCode.SLOT_NOT_FOUND, str(exc)) from exc
    except seal.SealValidationError as exc:
        raise PermanentJobError(FailureCode.PREPARATION_FAILED, str(exc)) from exc

    now = utcnow()
    data: bytes = prepared.data
    digest = hashlib.sha256(data).hexdigest()
    key = keys.revision_key(request.id_community, request.id, 1)
    await store.put(key, data, PDF)
    local.add(
        SignatureRevisionModel(
            request_id=request.id, number=1, key=key, sha256=digest, kind=int(RevisionKind.PREPARED)
        )
    )
    for signer in signers:
        placement = prepared.placements.get(signer.field_name)
        signer.slot_resolved = placement.as_json() if placement else None
    for item in form_fields:
        placement = prepared.placements.get(item.pdf_field_name)
        item.slot_resolved = placement.as_json() if placement else None
    request.current_revision = 1
    request.status = int(RequestStatus.OPEN)
    await append_event(
        local,
        request,
        EventType.DOCUMENT_PREPARED,
        document_sha256=digest,
        payload={"revision": 1, "flattened_fields": prepared.flattened},
        at=now,
    )
    invited = await _invite(local, request, signers, now)

    notifier = Notifier(crm)
    await notifier.invited(request, invited)
    await notifier.commit()
    await local.commit()
    await notifier.send_hints()
    metrics.seal_jobs.add(1, {"job": "prepare", "outcome": "ok"})
    await bus.domain_event(
        request.source_service,
        DomainEvent.PREPARED,
        {**accepted_data(request), "revision": 1, "sha256": digest},
    )
    return True


# ---------------------------------------------------------------------------
# sign
# ---------------------------------------------------------------------------


async def run_sign(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
    material: MaterialProvider,
) -> bool:
    request = await lock_request(local, request_id)
    if request is None or request.status != RequestStatus.OPEN:
        await local.rollback()
        return False
    signers = await signers_of(local, request.id)
    signer = next((s for s in signers if s.id == signer_id), None)
    if signer is None or signer.status != SignerStatus.SIGNING:
        await local.rollback()
        return False
    revision = await current_revision(local, request)
    current = await _load_verified(store, revision.key, revision.sha256)
    image = await store.get(str(signer.image_key))
    if hashlib.sha256(image).hexdigest() != signer.image_sha256:
        raise PermanentJobError(FailureCode.SEAL_FAILED, "the signature image changed")
    now = utcnow()
    own_fields = await fields_of(local, [signer.id])
    values: list[seal.FieldValue] = []
    for item in own_fields:
        if item.type == FieldType.DATE:
            item.value = wording.signing_date(request.locale, now)
        if item.value is None:
            continue
        values.append(seal.FieldValue(item.pdf_field_name, FieldType(item.type), item.value))
    community = await CrmRead(crm).community_name(request.id_community)
    capacity = Capacity(signer.capacity)
    # The PDF names the person who signed (frozen at the sign call), and the
    # member they signed for when that is not themselves.
    person = signer.signed_by_name or signer.display_name
    for_member = wording.signed_for(
        capacity, person=person, slot_name=signer.display_name, member_label=signer.member_label
    )
    try:
        data: bytes = await _timed(
            "sign",
            seal.sign,
            current,
            field_name=signer.field_name,
            values=values,
            image_png=image,
            stamp_lines=wording.stamp_lines(
                request.locale,
                name=person,
                at=now,
                ref=wording.short_ref(request.id),
                for_member=for_member,
            ),
            signer_name=person,
            reason=wording.reason(
                request.locale,
                capacity,
                name=person,
                member=for_member,
                community=community,
            ),
            material=material(),
        )
    except seal.SlotNotFoundError as exc:
        raise PermanentJobError(FailureCode.SLOT_NOT_FOUND, str(exc)) from exc
    except seal.SealValidationError as exc:
        raise PermanentJobError(FailureCode.SEAL_FAILED, str(exc)) from exc

    number = request.current_revision + 1
    digest = hashlib.sha256(data).hexdigest()
    key = keys.revision_key(request.id_community, request.id, number)
    await store.put(key, data, PDF)
    local.add(
        SignatureRevisionModel(
            request_id=request.id,
            number=number,
            key=key,
            sha256=digest,
            kind=int(RevisionKind.SIGNED),
            signer_id=signer.id,
        )
    )
    for item in own_fields:
        if item.value is not None:
            item.filled_at = now
    signer.status = int(SignerStatus.SIGNED)
    signer.signed_at = now
    signer.signed_revision = number
    request.current_revision = number
    await append_event(
        local,
        request,
        EventType.SIGNER_SIGNED,
        signer_id=signer.id,
        actor=SYSTEM,
        document_sha256=digest,
        payload={
            "revision": number,
            "viewed_revision": signer.viewed_revision,
            "signed_by_sub": signer.signed_by_sub,
            "fields": {item.name: item.value for item in own_fields if item.value is not None},
        },
        at=now,
    )
    notifier = Notifier(crm)
    await notifier.signed(request, signer)
    finished = all(s.status == SignerStatus.SIGNED for s in signers)
    invited: list[SignatureSignerModel] = []
    if finished:
        request.status = int(RequestStatus.COMPLETING)
    else:
        invited = await _invite(local, request, signers, now)
        await notifier.invited(request, invited)
    await notifier.commit()
    await local.commit()
    await notifier.send_hints()
    metrics.seal_jobs.add(1, {"job": "sign", "outcome": "ok"})
    await bus.domain_event(
        request.source_service,
        DomainEvent.SIGNER_SIGNED,
        {
            **accepted_data(request),
            "signer_id": str(signer.id),
            "position": signer.position,
            "id_member": signer.id_member,
            "revision": number,
        },
    )
    if finished:
        await bus.job(*complete_job(request.id))
    return True


# ---------------------------------------------------------------------------
# complete (the PDF part; the evidence report follows through docgen)
# ---------------------------------------------------------------------------


async def run_complete(
    request_id: uuid.UUID,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
    material: MaterialProvider,
) -> bool:
    request = await lock_request(local, request_id)
    if request is None or request.status != RequestStatus.COMPLETING:
        await local.rollback()
        return False
    now = utcnow()
    if request.final_key is None:
        revision = await current_revision(local, request)
        current = await _load_verified(store, revision.key, revision.sha256)
        try:
            final: bytes = await _timed("complete", seal.complete, current, material())
        except seal.SealValidationError as exc:
            raise PermanentJobError(FailureCode.SEAL_FAILED, str(exc)) from exc
        number = request.current_revision + 1
        digest = hashlib.sha256(final).hexdigest()
        key = keys.final_key(request.id_community, request.id)
        await store.put(key, final, PDF)
        local.add(
            SignatureRevisionModel(
                request_id=request.id,
                number=number,
                key=key,
                sha256=digest,
                kind=int(RevisionKind.FINAL),
            )
        )
        request.current_revision = number
        request.final_key = key
        request.final_sha256 = digest
    await request_evidence_render(request, local=local, crm=crm, store=store, bus=bus, now=now)
    metrics.seal_jobs.add(1, {"job": "complete", "outcome": "ok"})
    return True
