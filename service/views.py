"""What the API reads: the inbox, a request, its document, its timeline, a community's list.

A request is visible to whoever may sign one of its slots now, to whoever
signed one, and to the managers and admins of its community. Files are
streamed (the bucket stays private) after the same check.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors.errors import ErrorException
from domain import wording
from domain.evidence import verify_chain
from ports.crm_read import CrmRead
from ports.storage import DocumentStore, ObjectNotFound, TransientStorageError
from service.log import event_row
from service.signer_actions import Caller, consent_for, may_follow, may_sign, require_module
from shared.const import (
    Capacity,
    FieldType,
    RequestStatus,
    RevisionKind,
    SignerStatus,
    SigningOrder,
    SignMethod,
)
from shared.custom_errors import errors
from shared.models.local_models import (
    SignatureEventModel,
    SignatureFieldModel,
    SignatureRequestModel,
    SignatureRevisionModel,
    SignatureSignerModel,
)

PENDING_SLOT = (
    SignerStatus.WAITING,
    SignerStatus.INVITED,
    SignerStatus.VIEWED,
    SignerStatus.SIGNING,
)


def _iso(value: datetime.datetime | None) -> str | None:
    return value.astimezone(datetime.UTC).isoformat() if value else None


def _name(enum_type: Any, value: int | None) -> str | None:
    return enum_type(value).name if value is not None else None


@dataclass(frozen=True)
class Access:
    request: SignatureRequestModel
    signers: list[SignatureSignerModel]
    my_slots: list[SignatureSignerModel]
    follower: bool


async def access(
    request_id: uuid.UUID, *, caller: Caller, local: AsyncSession, crm: AsyncSession
) -> Access:
    request = await local.get(SignatureRequestModel, request_id)
    if request is None:
        raise ErrorException(errors.signature.REQUEST_NOT_FOUND, status_code=404)
    await require_module(crm, request)
    signers = list(
        (
            await local.execute(
                select(SignatureSignerModel)
                .where(SignatureSignerModel.request_id == request.id)
                .order_by(SignatureSignerModel.position)
            )
        ).scalars()
    )
    mine = [
        s
        for s in signers
        if s.signed_by_sub == caller.user.sub or await may_sign(crm, s, request, caller.user)
    ]
    follower = await may_follow(crm, request, caller.user)
    if not mine and not follower:
        # Not a 403: nobody learns whether a request they cannot see exists.
        raise ErrorException(errors.signature.REQUEST_NOT_FOUND, status_code=404)
    return Access(request, signers, mine, follower)


def request_summary(request: SignatureRequestModel, community: str | None = None) -> dict[str, Any]:
    return {
        "id": str(request.id),
        "reference": wording.short_ref(request.id),
        "id_community": request.id_community,
        "community": community,
        "title": request.title,
        "locale": request.locale,
        "source_service": request.source_service,
        "source_ref": request.source_ref,
        "status": RequestStatus(request.status).name,
        "signing_order": SigningOrder(request.signing_order).name,
        "created_at": _iso(request.created_at),
        "expires_at": _iso(request.expires_at),
        "closed_at": _iso(request.closed_at),
        "failure_code": request.failure_code,
        "current_revision": request.current_revision,
        "has_artifacts": request.status == RequestStatus.COMPLETED,
    }


def signer_view(signer: SignatureSignerModel) -> dict[str, Any]:
    return {
        "id": str(signer.id),
        "position": signer.position,
        "id_member": signer.id_member,
        "capacity": Capacity(signer.capacity).name,
        "display_name": signer.display_name,
        "member_label": signer.member_label,
        "role_label": signer.role_label,
        "status": SignerStatus(signer.status).name,
        "method": _name(SignMethod, signer.method),
        "slot": signer.slot_resolved,
        "invited_at": _iso(signer.invited_at),
        "signed_at": _iso(signer.signed_at),
        # Who signed, frozen at the sign call: a representative, or another
        # account linked to the member, differs from display_name.
        "signed_by_name": signer.signed_by_name,
        "declined_at": _iso(signer.declined_at),
        "decline_reason": signer.decline_reason,
    }


async def my_requests(
    *, caller: Caller, local: AsyncSession, crm: AsyncSession, status: str | None
) -> list[dict[str, Any]]:
    read = CrmRead(crm)
    member_ids = await read.member_ids_for_user(caller.user.id)
    conditions = [
        SignatureSignerModel.expected_user_sub == caller.user.sub,
        SignatureSignerModel.signed_by_sub == caller.user.sub,
    ]
    if member_ids:
        conditions.append(SignatureSignerModel.id_member.in_(member_ids))
    rows = (
        await local.execute(
            select(SignatureSignerModel, SignatureRequestModel)
            .join(
                SignatureRequestModel, SignatureRequestModel.id == SignatureSignerModel.request_id
            )
            .where(or_(*conditions))
            .where(SignatureRequestModel.status != RequestStatus.PREPARING)
            .order_by(SignatureRequestModel.created_at.desc(), SignatureSignerModel.position)
        )
    ).all()
    active = await read.communities_with_module(r.id_community for _s, r in rows)
    names: dict[int, str | None] = {}
    out: list[dict[str, Any]] = []
    for signer, request in rows:
        if request.id_community not in active:
            continue
        # An admin's slot of someone else, or a member slot the caller is no
        # longer linked to and did not sign: not theirs.
        if (
            signer.capacity == Capacity.COMMUNITY_ADMIN
            and signer.expected_user_sub != caller.user.sub
        ):
            continue
        pending = request.status == RequestStatus.OPEN and signer.status in PENDING_SLOT
        if status == "pending" and not pending:
            continue
        if status == "done" and pending:
            continue
        if request.id_community not in names:
            names[request.id_community] = await read.community_name(request.id_community)
        out.append(
            {
                "request": request_summary(request, names[request.id_community]),
                "signer": signer_view(signer),
                "pending": pending,
                "your_turn": pending
                and signer.status in (SignerStatus.INVITED, SignerStatus.VIEWED),
            }
        )
    return out


async def detail(
    request_id: uuid.UUID, *, caller: Caller, local: AsyncSession, crm: AsyncSession
) -> dict[str, Any]:
    seen = await access(request_id, caller=caller, local=local, crm=crm)
    request = seen.request
    community = await CrmRead(crm).community_name(request.id_community)
    fields = list(
        (
            await local.execute(
                select(SignatureFieldModel)
                .where(SignatureFieldModel.signer_id.in_([s.id for s in seen.signers]))
                .order_by(SignatureFieldModel.name)
            )
        ).scalars()
    )
    revision = (
        await local.execute(
            select(SignatureRevisionModel).where(
                SignatureRevisionModel.request_id == request.id,
                SignatureRevisionModel.number == request.current_revision,
            )
        )
    ).scalar_one_or_none()
    mine = {s.id for s in seen.my_slots}
    my_slots = []
    for signer in seen.my_slots:
        consent = consent_for(request, signer, caller.user, community)
        my_slots.append(
            {
                "signer_id": str(signer.id),
                "consent_version": request.consent_version,
                "consent_text": consent.text,
                "fields": [
                    {
                        "name": f.name,
                        "type": FieldType(f.type).name,
                        "label": f.label,
                        "required": f.required,
                        "slot": f.slot_resolved,
                        "value": f.value,
                    }
                    for f in fields
                    if f.signer_id == signer.id
                ],
            }
        )
    signed = sum(1 for s in seen.signers if s.status == SignerStatus.SIGNED)
    return {
        "request": request_summary(request, community),
        "signers": [{**signer_view(s), "mine": s.id in mine} for s in seen.signers],
        "progress": {"signed": signed, "total": len(seen.signers)},
        "document": (
            {
                "revision": revision.number,
                "sha256": revision.sha256,
                "kind": RevisionKind(revision.kind).name,
            }
            if revision
            else None
        ),
        "my_slots": my_slots,
        "can_follow": seen.follower,
    }


async def document(
    request_id: uuid.UUID,
    *,
    caller: Caller,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
) -> tuple[bytes, int, str]:
    seen = await access(request_id, caller=caller, local=local, crm=crm)
    revision = (
        await local.execute(
            select(SignatureRevisionModel).where(
                SignatureRevisionModel.request_id == seen.request.id,
                SignatureRevisionModel.number == seen.request.current_revision,
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise ErrorException(errors.signature.ARTIFACT_NOT_READY, status_code=409)
    return await _read(store, revision.key), revision.number, revision.sha256


ARTIFACTS = {
    "final": ("final_key", "application/pdf", "signed.pdf"),
    "evidence-pdf": ("evidence_pdf_key", "application/pdf", "evidence.pdf"),
    "evidence-json": ("evidence_json_key", "application/json", "evidence.json"),
}


async def artifact(
    request_id: uuid.UUID,
    kind: str,
    *,
    caller: Caller,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
) -> tuple[bytes, str, str]:
    if kind not in ARTIFACTS:
        raise ErrorException(errors.signature.ARTIFACT_NOT_READY, status_code=404)
    seen = await access(request_id, caller=caller, local=local, crm=crm)
    attribute, media_type, filename = ARTIFACTS[kind]
    if seen.request.status != RequestStatus.COMPLETED:
        raise ErrorException(errors.signature.ARTIFACT_NOT_READY, status_code=409)
    key = getattr(seen.request, attribute)
    if not key:
        raise ErrorException(errors.signature.ARTIFACT_NOT_READY, status_code=409)
    return await _read(store, key), media_type, f"{wording.short_ref(seen.request.id)}-{filename}"


async def _read(store: DocumentStore, key: str) -> bytes:
    try:
        return await store.get(key)
    except (ObjectNotFound, TransientStorageError):
        raise ErrorException(errors.signature.STORAGE_FAILED, status_code=503) from None


async def timeline(
    request_id: uuid.UUID, *, caller: Caller, local: AsyncSession, crm: AsyncSession
) -> dict[str, Any]:
    seen = await access(request_id, caller=caller, local=local, crm=crm)
    if not seen.follower:
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    events = list(
        (
            await local.execute(
                select(SignatureEventModel)
                .where(SignatureEventModel.request_id == seen.request.id)
                .order_by(SignatureEventModel.seq)
            )
        ).scalars()
    )
    check = verify_chain(seen.request.original_sha256, [event_row(e) for e in events])
    position = {s.id: s for s in seen.signers}
    return {
        "chain": {
            "ok": check.ok,
            "events": check.events,
            "head": check.head,
            "broken_at": check.broken_at,
        },
        "events": [
            {
                "seq": e.seq,
                "type": e.type,
                "at": _iso(e.at),
                "signer": (
                    {
                        "position": position[e.signer_id].position,
                        "display_name": position[e.signer_id].display_name,
                    }
                    if e.signer_id in position
                    else None
                ),
                "document_sha256": e.document_sha256,
                "payload": {k: v for k, v in e.payload.items() if k not in ("consent_text",)},
            }
            for e in events
        ],
    }


async def community_requests(
    id_community: int,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    status: str | None,
    source: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    query = select(SignatureRequestModel).where(SignatureRequestModel.id_community == id_community)
    if status:
        try:
            query = query.where(SignatureRequestModel.status == int(RequestStatus[status.upper()]))
        except KeyError:
            query = query.where(SignatureRequestModel.status == -1)
    if source:
        query = query.where(SignatureRequestModel.source_service == source)
    total = (await local.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    requests = list(
        (
            await local.execute(
                query.order_by(SignatureRequestModel.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars()
    )
    ids = [r.id for r in requests]
    counts: dict[uuid.UUID, tuple[int, int]] = {}
    if ids:
        for request_id, signed, total_signers in (
            await local.execute(
                select(
                    SignatureSignerModel.request_id,
                    func.count().filter(SignatureSignerModel.status == int(SignerStatus.SIGNED)),
                    func.count(),
                )
                .where(SignatureSignerModel.request_id.in_(ids))
                .group_by(SignatureSignerModel.request_id)
            )
        ).all():
            counts[request_id] = (int(signed), int(total_signers))
    community = await CrmRead(crm).community_name(id_community)
    return {
        "total": int(total),
        "items": [
            {
                **request_summary(r, community),
                "progress": {
                    "signed": counts.get(r.id, (0, 0))[0],
                    "total": counts.get(r.id, (0, 0))[1],
                },
                "last_reminded_at": _iso(r.last_reminded_at),
            }
            for r in requests
        ],
    }
