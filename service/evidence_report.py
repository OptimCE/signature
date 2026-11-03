"""The evidence report: ``evidence.json``, rendered to ``evidence.pdf`` by docgen, sealed.

The report is a separate file because adding pages to the signed PDF would
break its signatures. It names the final PDF by its SHA-256, lists every
signer (capacity, member, method, the fields they filled) and every event of
the log, and states the chain's head after ``verify_chain`` recomputed it:
a broken chain refuses the report (and fails the request) instead of
certifying a log that was tampered with.

docgen renders it from ``document-templates/signature/evidence/v3`` (the
signature domain's own template); its result comes back on
``docgen.result.signature``, correlated by ``evidence_request_id`` and never by
its metadata. The PDF is then sealed PAdES-B-LT and the request COMPLETED.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.storage import parse_s3_uri
from crypto import seal
from domain import wording
from domain.evidence import verify_chain
from ports.bus import Bus
from ports.crm_read import CrmRead
from ports.storage import DocumentStore
from service import keys
from service.failure import PermanentJobError, fail_request
from service.log import append_event, event_row, lock_request, utcnow
from service.notify import Notifier
from service.requests import accepted_data
from shared.const import (
    Capacity,
    DomainEvent,
    EventType,
    FailureCode,
    RequestStatus,
    RevisionKind,
    SignMethod,
)
from shared.models.local_models import (
    SignatureEventModel,
    SignatureFieldModel,
    SignatureRequestModel,
    SignatureRevisionModel,
    SignatureSignerModel,
)

logger = logging.getLogger(__name__)

FORMAT = "optimce.signature.evidence/1"


def _iso(value: datetime.datetime | None) -> str | None:
    return value.astimezone(datetime.UTC).isoformat() if value else None


async def build_evidence(
    request: SignatureRequestModel,
    *,
    local: AsyncSession,
    community: str | None,
    now: datetime.datetime,
) -> dict[str, Any]:
    events = list(
        (
            await local.execute(
                select(SignatureEventModel)
                .where(SignatureEventModel.request_id == request.id)
                .order_by(SignatureEventModel.seq)
            )
        ).scalars()
    )
    check = verify_chain(request.original_sha256, [event_row(e) for e in events])
    if not check.ok:
        raise PermanentJobError(
            FailureCode.EVIDENCE_FAILED,
            f"the evidence chain is broken at event {check.broken_at} ({check.reason})",
        )
    signers = list(
        (
            await local.execute(
                select(SignatureSignerModel)
                .where(SignatureSignerModel.request_id == request.id)
                .order_by(SignatureSignerModel.position)
            )
        ).scalars()
    )
    signer_ids = [s.id for s in signers]
    fields = (
        list(
            (
                await local.execute(
                    select(SignatureFieldModel).where(SignatureFieldModel.signer_id.in_(signer_ids))
                )
            ).scalars()
        )
        if signer_ids
        else []
    )
    revisions = list(
        (
            await local.execute(
                select(SignatureRevisionModel)
                .where(SignatureRevisionModel.request_id == request.id)
                .order_by(SignatureRevisionModel.number)
            )
        ).scalars()
    )
    position = {s.id: s.position for s in signers}
    # The statement each signer ticked, word for word, as their sign call recorded it.
    agreed = {
        e.signer_id: e.payload.get("consent_text")
        for e in events
        if e.type == EventType.SIGNER_REAUTHENTICATED.value and e.signer_id
    }
    consent_digest = wording.consent(
        request.consent_version, request.locale, Capacity.SELF, member=None, community=None
    ).sha256
    return {
        "format": FORMAT,
        "generated_at": _iso(now),
        "request": {
            "id": str(request.id),
            "reference": wording.short_ref(request.id),
            "community_id": request.id_community,
            "community": community,
            "title": request.title,
            "locale": request.locale,
            "source_service": request.source_service,
            "source_ref": request.source_ref,
            "signing_order": "SEQUENTIAL" if request.signing_order == 2 else "PARALLEL",
            "requested_by_sub": request.requested_by_sub,
            "created_at": _iso(request.created_at),
        },
        "document": {
            "original_key": request.original_key,
            "original_sha256": request.original_sha256,
            "final_key": request.final_key,
            "final_sha256": request.final_sha256,
            "revisions": [
                {
                    "number": r.number,
                    "kind": RevisionKind(r.kind).name,
                    "sha256": r.sha256,
                    "signer_position": position.get(r.signer_id) if r.signer_id else None,
                    "created_at": _iso(r.created_at),
                }
                for r in revisions
            ],
        },
        "consent": {
            "version": request.consent_version,
            "locale": request.locale,
            "sha256": consent_digest,
        },
        "signers": [
            {
                "position": s.position,
                "capacity": Capacity(s.capacity).name,
                "id_member": s.id_member,
                "display_name": s.display_name,
                "member_label": s.member_label,
                "role_label": s.role_label,
                "method": SignMethod(s.method).name if s.method else None,
                "typed_name": s.typed_name,
                "image_sha256": s.image_sha256,
                "signed_by_name": s.signed_by_name,
                # Whom they signed for when not themselves: the PDF's « pour … ».
                "signed_for": wording.signed_for(
                    Capacity(s.capacity),
                    person=s.signed_by_name or s.display_name,
                    slot_name=s.display_name,
                    member_label=s.member_label,
                ),
                "signed_by_sub": s.signed_by_sub,
                "signed_by_user_id": s.signed_by_user_id,
                "viewed_revision": s.viewed_revision,
                "signed_revision": s.signed_revision,
                "signed_at": _iso(s.signed_at),
                "consent_text": agreed.get(s.id),
                "fields": {
                    f.name: f.value for f in fields if f.signer_id == s.id and f.value is not None
                },
            }
            for s in signers
        ],
        "events": [
            {
                "seq": e.seq,
                "type": e.type,
                "at": _iso(e.at),
                "signer_position": position.get(e.signer_id) if e.signer_id else None,
                "actor_sub": e.actor_sub,
                "ip": e.ip,
                "user_agent": e.user_agent,
                "keycloak_sid": e.keycloak_sid,
                "auth_time": _iso(e.auth_time),
                "acr": e.acr,
                "document_sha256": e.document_sha256,
                "consent_version": e.consent_version,
                "payload": e.payload,
                "prev_hash": e.prev_hash,
                "hash": e.hash,
            }
            for e in events
        ],
        "chain": {
            "algorithm": "sha256(prev_hash || canonical JSON of the event without id and hash)",
            "anchor": request.original_sha256,
            "events": check.events,
            "head": check.head,
            "verified": True,
        },
    }


async def request_evidence_render(
    request: SignatureRequestModel,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
    now: datetime.datetime,
) -> None:
    """Write evidence.json (once) and ask docgen for the PDF. The caller holds the request lock.

    Asked again (the requeue pass, a redelivery), it re-sends the SAME render
    request from the evidence.json already written: that file is written once.
    """
    if request.evidence_json_key and request.evidence_json_sha256:
        raw = await store.get(request.evidence_json_key)
        if hashlib.sha256(raw).hexdigest() != request.evidence_json_sha256:
            raise PermanentJobError(
                FailureCode.EVIDENCE_FAILED, "evidence.json no longer matches its digest"
            )
        evidence = json.loads(raw)
    else:
        community = await CrmRead(crm).community_name(request.id_community)
        evidence = await build_evidence(request, local=local, community=community, now=now)
        raw = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        key = keys.evidence_json_key(request.id_community, request.id)
        await store.put(key, raw, "application/json")
        request.evidence_json_key = key
        request.evidence_json_sha256 = hashlib.sha256(raw).hexdigest()
    render_id = request.evidence_request_id or uuid.uuid4()
    request.evidence_request_id = render_id
    await local.commit()
    await bus.docgen(settings.DOCGEN_REQUEST_SUBJECT, docgen_body(request, evidence, render_id))


def docgen_body(
    request: SignatureRequestModel, evidence: dict[str, Any], render_id: uuid.UUID
) -> dict[str, Any]:
    return {
        "request_id": str(render_id),
        "tenant_id": str(request.id_community),
        "requested_by": "signature",
        "template": {"uri": settings.EVIDENCE_TEMPLATE_URI},
        "outputs": [{"format": "pdf"}],
        "data": {"evidence": evidence, "evidence_json_sha256": request.evidence_json_sha256},
        "key_prefix": keys.evidence_render_prefix(request.id_community, request.id),
        "reply_to": settings.DOCGEN_RESULT_SUBJECT,
        "options": {"locale": request.locale, "presign_ttl": settings.DOCGEN_PRESIGN_TTL},
        "metadata": {"signature_request_id": str(request.id)},
    }


async def handle_docgen_result(
    body: dict[str, Any],
    *,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
    material: Any,
) -> str:
    """Seal the rendered report and complete the request. Returns what happened."""
    render_id_raw = body.get("request_id")
    try:
        render_id = uuid.UUID(str(render_id_raw))
    except ValueError:
        return "drop"
    found = (
        await local.execute(
            select(SignatureRequestModel.id).where(
                SignatureRequestModel.evidence_request_id == render_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        return "drop"
    request = await lock_request(local, found)
    if request is None or request.status != RequestStatus.COMPLETING or request.evidence_pdf_key:
        await local.rollback()
        return "drop"
    if body.get("status") != "success":
        error = body.get("error") or {}
        await fail_request(
            request.id,
            FailureCode.EVIDENCE_FAILED,
            f"docgen: {error.get('code')} {error.get('message')}",
            local=local,
            crm=crm,
            bus=bus,
        )
        return "failed"
    artifact = next((a for a in body.get("artifacts") or [] if a.get("format") == "pdf"), None)
    if artifact is None:
        await fail_request(
            request.id,
            FailureCode.EVIDENCE_FAILED,
            "docgen returned no PDF",
            local=local,
            crm=crm,
            bus=bus,
        )
        return "failed"
    _bucket, rendered_key = parse_s3_uri(str(artifact["uri"]))
    rendered = await store.get(rendered_key)
    try:
        sealed: bytes = await asyncio.to_thread(
            seal.seal_evidence, rendered, material(), reason=wording.evidence_reason(request.locale)
        )
    except seal.SealValidationError as exc:
        await fail_request(
            request.id, FailureCode.EVIDENCE_FAILED, str(exc), local=local, crm=crm, bus=bus
        )
        return "failed"
    now = utcnow()
    key = keys.evidence_pdf_key(request.id_community, request.id)
    await store.put(key, sealed, "application/pdf")
    request.evidence_pdf_key = key
    request.evidence_pdf_sha256 = hashlib.sha256(sealed).hexdigest()
    request.status = int(RequestStatus.COMPLETED)
    request.closed_at = now
    await append_event(
        local,
        request,
        EventType.REQUEST_COMPLETED,
        document_sha256=request.final_sha256,
        payload={
            "final_key": request.final_key,
            "final_sha256": request.final_sha256,
            "evidence_json_sha256": request.evidence_json_sha256,
            "evidence_pdf_sha256": request.evidence_pdf_sha256,
        },
        at=now,
    )
    signers = list(
        (
            await local.execute(
                select(SignatureSignerModel).where(SignatureSignerModel.request_id == request.id)
            )
        ).scalars()
    )
    notifier = Notifier(crm)
    await notifier.completed(request, signers)
    await notifier.commit()
    await local.commit()
    await notifier.send_hints()
    await bus.domain_event(
        request.source_service,
        DomainEvent.COMPLETED,
        {
            **accepted_data(request),
            "final_key": request.final_key,
            "final_sha256": request.final_sha256,
            "evidence_pdf_key": request.evidence_pdf_key,
            "evidence_pdf_sha256": request.evidence_pdf_sha256,
            "evidence_json_key": request.evidence_json_key,
            "evidence_json_sha256": request.evidence_json_sha256,
        },
    )
    return "completed"
