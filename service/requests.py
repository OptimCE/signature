"""A domain's request comes in (``signature.request.create``) or is withdrawn (``.cancel``).

Creation checks everything a domain can get wrong BEFORE storing anything:
the module is on for the community, the document sits under the domain's
own prefix and exists, every member exists in the community and has at
least one account, every admin slot names an ADMIN of the community, and
every slot parses. Any of these is permanent: no request, a ``failed``
event. A store or broker hiccup is transient (raised; the consumer NAKs).

The same ``(source_service, idempotency_key)`` sent again answers with the
existing request (``accepted`` once more) and creates nothing.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from domain.payloads import CancelRequestIn, CreateRequestIn, SignerIn
from domain.slots import SlotError, parse_slot
from ports.bus import Bus, prepare_job
from ports.crm_read import MEMBER_TYPE_COMPANY, CrmRead
from ports.storage import DocumentStore, ObjectNotFound
from service.keys import document_allowed
from service.log import SYSTEM, Actor, append_event, lock_request, utcnow
from service.notify import Notifier
from shared.const import (
    CONSENT_VERSION,
    DEFAULT_EXPIRY_DAYS,
    Capacity,
    DomainEvent,
    EventType,
    FailureCode,
    FieldType,
    RequestStatus,
    SignerStatus,
    SigningOrder,
    SlotKind,
)
from shared.models.local_models import (
    SignatureFieldModel,
    SignatureRequestModel,
    SignatureSignerModel,
)

logger = logging.getLogger(__name__)


class RequestRefused(Exception):  # noqa: N818  # a domain outcome, not a crash
    def __init__(self, code: FailureCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _ResolvedSigner:
    spec: SignerIn
    capacity: Capacity
    display_name: str
    email: str | None
    member_label: str | None


def accepted_data(request: SignatureRequestModel) -> dict[str, Any]:
    return {
        "request_id": str(request.id),
        "source_ref": request.source_ref,
        "idempotency_key": request.idempotency_key,
        "status": RequestStatus(request.status).name.lower(),
    }


def failed_data(
    source_ref: str | None, idempotency_key: str | None, code: FailureCode, detail: str
) -> dict[str, Any]:
    return {
        "request_id": None,
        "source_ref": source_ref,
        "idempotency_key": idempotency_key,
        "code": code.value,
        "detail": detail[:500],
    }


async def handle_create(
    raw: dict[str, Any],
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
    now: datetime.datetime | None = None,
) -> SignatureRequestModel | None:
    """Create (or find) a request. Returns it, or None when refused (``failed`` sent)."""
    try:
        payload = CreateRequestIn.model_validate(raw)
    except ValidationError as exc:
        source = raw.get("source_service") if isinstance(raw, dict) else None
        if isinstance(source, str) and source.replace("_", "").isalnum():
            await bus.domain_event(
                source,
                DomainEvent.FAILED,
                failed_data(
                    _str_or_none(raw.get("source_ref")),
                    _str_or_none(raw.get("idempotency_key")),
                    FailureCode.INVALID_REQUEST,
                    str(exc.errors()[:3]),
                ),
            )
        metrics.requests_created.add(1, {"outcome": "invalid"})
        logger.warning("refused a malformed signature request: %s", exc.errors()[:3])
        return None

    existing = await _find(local, payload.source_service, payload.idempotency_key)
    if existing is not None:
        await bus.domain_event(
            payload.source_service, DomainEvent.ACCEPTED, accepted_data(existing)
        )
        if existing.status == RequestStatus.PREPARING:
            await bus.job(*prepare_job(existing.id))
        return existing

    try:
        request = await _create(payload, local=local, crm=crm, store=store, now=now or utcnow())
    except RequestRefused as refused:
        await local.rollback()
        metrics.requests_created.add(
            1, {"source": payload.source_service, "outcome": refused.code.value}
        )
        logger.info("signature request %s refused: %s", payload.idempotency_key, refused)
        await bus.domain_event(
            payload.source_service,
            DomainEvent.FAILED,
            failed_data(payload.source_ref, payload.idempotency_key, refused.code, refused.detail),
        )
        return None
    await local.commit()
    metrics.requests_created.add(1, {"source": payload.source_service, "outcome": "accepted"})
    await bus.domain_event(payload.source_service, DomainEvent.ACCEPTED, accepted_data(request))
    await bus.job(*prepare_job(request.id))
    return request


def _str_or_none(value: object) -> str | None:
    return value[:255] if isinstance(value, str) else None


async def _find(local: AsyncSession, source: str, key: str) -> SignatureRequestModel | None:
    result = await local.execute(
        select(SignatureRequestModel).where(
            SignatureRequestModel.source_service == source,
            SignatureRequestModel.idempotency_key == key,
        )
    )
    return result.scalar_one_or_none()


async def _create(
    payload: CreateRequestIn,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    now: datetime.datetime,
) -> SignatureRequestModel:
    read = CrmRead(crm)
    community = payload.community_id
    if not document_allowed(payload.source_service, community, payload.document_key):
        if payload.source_service not in _known_sources():
            raise RequestRefused(FailureCode.UNKNOWN_SOURCE, payload.source_service)
        raise RequestRefused(FailureCode.DOCUMENT_OUTSIDE_PREFIX, payload.document_key)
    if not await read.module_active(community):
        raise RequestRefused(FailureCode.MODULE_DISABLED, f"community {community}")

    resolved: list[_ResolvedSigner] = []
    for spec in payload.signers:
        resolved.append(await _resolve_signer(read, community, spec))
    _check_slots(payload)

    try:
        original = await store.get(payload.document_key)
    except ObjectNotFound:
        raise RequestRefused(FailureCode.DOCUMENT_NOT_FOUND, payload.document_key) from None
    if not original.startswith(b"%PDF-"):
        raise RequestRefused(FailureCode.INVALID_REQUEST, "the document is not a PDF")

    requester_id = None
    if payload.requested_by:
        requester = await read.user_by_sub(payload.requested_by)
        requester_id = requester.id if requester else None

    order = SigningOrder[payload.signing_order]
    request = SignatureRequestModel(
        id=uuid.uuid4(),
        id_community=community,
        source_service=payload.source_service,
        source_ref=payload.source_ref,
        idempotency_key=payload.idempotency_key,
        title=payload.title,
        locale=payload.locale,
        original_key=payload.document_key,
        original_sha256=hashlib.sha256(original).hexdigest(),
        current_revision=0,
        signing_order=int(order),
        status=int(RequestStatus.PREPARING),
        consent_version=CONSENT_VERSION,
        requested_by_sub=payload.requested_by,
        requested_by_user_id=requester_id,
        expires_at=payload.expires_at or now + datetime.timedelta(days=DEFAULT_EXPIRY_DAYS),
        created_at=now,
        updated_at=now,
    )
    if request.expires_at <= now:
        raise RequestRefused(FailureCode.INVALID_REQUEST, "expires_at is in the past")
    local.add(request)
    await local.flush()
    for position, item in enumerate(resolved, start=1):
        signer = SignatureSignerModel(
            id=uuid.uuid4(),
            request_id=request.id,
            position=position,
            id_member=item.spec.member_id,
            capacity=int(item.capacity),
            expected_user_sub=item.spec.user,
            display_name=item.display_name,
            email=item.email,
            member_label=item.member_label,
            role_label=item.spec.role_label,
            slot=item.spec.slot,
            field_name=_signature_field_name(item.spec, position),
            status=int(SignerStatus.WAITING),
        )
        local.add(signer)
        await local.flush()
        for spec_field in item.spec.fields:
            local.add(
                SignatureFieldModel(
                    id=uuid.uuid4(),
                    signer_id=signer.id,
                    name=spec_field.name,
                    type=int(FieldType[spec_field.type]),
                    label=spec_field.label,
                    required=spec_field.required or spec_field.type == "DATE",
                    slot=spec_field.slot,
                    pdf_field_name=_form_field_name(spec_field.slot, position, spec_field.name),
                )
            )
    await local.flush()
    await append_event(
        local,
        request,
        EventType.REQUEST_CREATED,
        actor=Actor(sub=payload.requested_by, user_id=requester_id),
        document_sha256=request.original_sha256,
        payload={
            "source_service": payload.source_service,
            "source_ref": payload.source_ref,
            "title": payload.title,
            "document_key": payload.document_key,
            "signing_order": order.name,
            "signers": [
                {
                    "position": position,
                    "id_member": item.spec.member_id,
                    "capacity": item.capacity.name,
                    "display_name": item.display_name,
                    "role_label": item.spec.role_label,
                }
                for position, item in enumerate(resolved, start=1)
            ],
        },
        at=now,
    )
    return request


def _known_sources() -> set[str]:
    from shared.const import SOURCE_DOCUMENT_PREFIXES

    return set(SOURCE_DOCUMENT_PREFIXES)


async def _resolve_signer(read: CrmRead, community: int, spec: SignerIn) -> _ResolvedSigner:
    if spec.capacity == "COMMUNITY_ADMIN":
        user = await read.user_by_sub(str(spec.user))
        if user is None or await read.role_in_community(user.id, community) != "ADMIN":
            raise RequestRefused(FailureCode.USER_NOT_ADMIN, str(spec.user))
        return _ResolvedSigner(spec, Capacity.COMMUNITY_ADMIN, user.display_name, user.email, None)
    if spec.member_id is None:
        raise RequestRefused(FailureCode.INVALID_REQUEST, "a member slot without its member")
    member = await read.member(spec.member_id)
    if member is None or member.id_community != community:
        raise RequestRefused(FailureCode.MEMBER_NOT_FOUND, str(spec.member_id))
    if not await read.linked_users(member.id):
        raise RequestRefused(FailureCode.MEMBER_WITHOUT_ACCOUNT, str(member.id))
    if spec.capacity is not None:
        capacity = Capacity[spec.capacity]
    else:
        capacity = (
            Capacity.LEGAL_REPRESENTATIVE
            if member.member_type == MEMBER_TYPE_COMPANY
            else Capacity.SELF
        )
    member_label = member.display_name if capacity is Capacity.LEGAL_REPRESENTATIVE else None
    return _ResolvedSigner(spec, capacity, member.display_name, member.email, member_label)


def _check_slots(payload: CreateRequestIn) -> None:
    seen: set[str] = set()
    try:
        for position, spec in enumerate(payload.signers, start=1):
            parse_slot(spec.slot, for_signature=True)
            name = _signature_field_name(spec, position)
            if name in seen:
                raise RequestRefused(FailureCode.INVALID_REQUEST, f"two slots on {name}")
            seen.add(name)
            for spec_field in spec.fields:
                field_slot = parse_slot(spec_field.slot, for_signature=False)
                if spec_field.type == "CHECKBOX" and field_slot.kind is SlotKind.PAGE:
                    raise RequestRefused(FailureCode.INVALID_REQUEST, "checkbox on a page slot")
                pdf_name = _form_field_name(spec_field.slot, position, spec_field.name)
                if pdf_name in seen:
                    raise RequestRefused(FailureCode.INVALID_REQUEST, f"two slots on {pdf_name}")
                seen.add(pdf_name)
    except SlotError as exc:
        raise RequestRefused(FailureCode.INVALID_REQUEST, str(exc)) from None


def _signature_field_name(spec: SignerIn, position: int) -> str:
    target = spec.slot.get(SlotKind.FIELD.value)
    if isinstance(target, str):
        return target
    # Partial field names may not contain periods.
    return f"OptimCE-Signer-{position}"


def _form_field_name(slot: dict[str, Any], position: int, name: str) -> str:
    target = slot.get(SlotKind.FIELD.value)
    if isinstance(target, str):
        return target
    return f"OptimCE-Signer-{position}-{name}"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def handle_cancel(
    raw: dict[str, Any],
    *,
    local: AsyncSession,
    crm: AsyncSession,
    bus: Bus,
) -> SignatureRequestModel | None:
    try:
        payload = CancelRequestIn.model_validate(raw)
    except ValidationError as exc:
        logger.warning("refused a malformed cancel: %s", exc.errors()[:3])
        return None
    request = await _find(local, payload.source_service, payload.idempotency_key)
    if request is None:
        return None
    actor = Actor(sub=payload.requested_by)
    return await cancel(
        request.id, local=local, crm=crm, bus=bus, actor=actor, reason=payload.reason
    )


async def cancel(
    request_id: uuid.UUID,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    bus: Bus,
    actor: Actor = SYSTEM,
    reason: str | None = None,
) -> SignatureRequestModel | None:
    """Withdraw a request still PREPARING or OPEN. Anything else: nothing happens."""
    request = await lock_request(local, request_id)
    if request is None:
        return None
    if request.status not in (RequestStatus.PREPARING, RequestStatus.OPEN):
        await local.rollback()
        return request
    now = utcnow()
    request.status = int(RequestStatus.CANCELLED)
    request.closed_at = now
    await append_event(
        local,
        request,
        EventType.REQUEST_CANCELLED,
        actor=actor,
        payload={"reason": reason or ""},
        at=now,
    )
    notifier = Notifier(crm)
    notifier.changed(request, "failed")
    await local.commit()
    await notifier.send_hints()
    await bus.domain_event(
        request.source_service,
        DomainEvent.CANCELLED,
        {**accepted_data(request), "reason": reason or ""},
    )
    return request
