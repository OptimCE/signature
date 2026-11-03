"""What a signer does through the API: view, sign, decline. And a manager: remind, cancel.

Authorization is decided at the moment of the action, from the CRM: a
member's slot is signed by an account linked to the member NOW (any of them),
an admin's slot by the named user while they are still ADMIN of the
community. The module must be on for the request's community.

What-you-see-is-what-you-sign: the browser hashes the bytes it displays and
sends that SHA-256 back. It must be one of THIS request's revisions from 1 on
(every later revision only adds signatures and their fields: the
certification guarantees nothing else changed); anything else is 409. Both
the revision the signer viewed and the one their signature lands in are kept.

The sign call itself seals nothing: it records the consent, the values and
the image, moves the slot to SIGNING and queues the job (202). A second call
on the slot answers 409.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import io
import logging
import re
import uuid
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from core.errors.errors import ErrorException
from domain import wording
from ports.bus import Bus, sign_job
from ports.crm_read import CrmRead, UserInfo
from ports.storage import DocumentStore
from service import keys
from service.log import Actor, append_event, lock_request, utcnow
from service.notify import Notifier
from service.requests import accepted_data
from shared.const import (
    MAX_DRAWN_PNG_BYTES,
    MAX_TEXT_FIELD_LENGTH,
    REMIND_INTERVAL_HOURS,
    SIGN_CALLS_PER_MINUTE,
    Capacity,
    DomainEvent,
    EventType,
    FieldType,
    RequestStatus,
    SignerStatus,
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

logger = logging.getLogger(__name__)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_IMAGE_SIZE = (1600, 800)
_OPEN_SLOT = (SignerStatus.INVITED, SignerStatus.VIEWED)


@dataclass(frozen=True)
class Caller:
    user: UserInfo
    actor: Actor


@dataclass(frozen=True)
class SignInput:
    method: SignMethod
    typed_name: str | None
    image_png: str
    fields: dict[str, str]
    consent_version: str
    consent_accepted: bool
    document_sha256: str


async def caller_for(crm: AsyncSession, actor: Actor) -> Caller:
    if not actor.sub:
        raise ErrorException(errors.auth.UNAUTHORIZED, status_code=401)
    user = await CrmRead(crm).user_by_sub(actor.sub)
    if user is None:
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    return Caller(user, Actor(**{**actor.__dict__, "user_id": user.id}))


async def require_module(crm: AsyncSession, request: SignatureRequestModel) -> None:
    if not await CrmRead(crm).module_active(request.id_community):
        raise ErrorException(errors.signature.MODULE_DISABLED, status_code=403)


async def may_sign(
    crm: AsyncSession, signer: SignatureSignerModel, request: SignatureRequestModel, user: UserInfo
) -> bool:
    read = CrmRead(crm)
    if signer.capacity == Capacity.COMMUNITY_ADMIN:
        return signer.expected_user_sub == user.sub and (
            await read.role_in_community(user.id, request.id_community) == "ADMIN"
        )
    return signer.id_member is not None and await read.is_linked(user.id, signer.id_member)


async def may_follow(crm: AsyncSession, request: SignatureRequestModel, user: UserInfo) -> bool:
    """Managers and admins of the request's community follow it."""
    role = await CrmRead(crm).role_in_community(user.id, request.id_community)
    return role in ("MANAGER", "ADMIN")


async def _load(
    local: AsyncSession, request_id: uuid.UUID, signer_id: uuid.UUID, *, lock: bool
) -> tuple[SignatureRequestModel, SignatureSignerModel]:
    if lock:
        request = await lock_request(local, request_id)
    else:
        request = await local.get(SignatureRequestModel, request_id)
    if request is None:
        raise ErrorException(errors.signature.REQUEST_NOT_FOUND, status_code=404)
    signer = await local.get(SignatureSignerModel, signer_id, populate_existing=True)
    if signer is None or signer.request_id != request.id:
        raise ErrorException(errors.signature.SIGNER_NOT_FOUND, status_code=404)
    return request, signer


async def _revision_for(local: AsyncSession, request: SignatureRequestModel, digest: str) -> int:
    if not _HEX64.match(digest or ""):
        raise ErrorException(errors.signature.DOCUMENT_CHANGED, status_code=409)
    number = (
        await local.execute(
            select(SignatureRevisionModel.number).where(
                SignatureRevisionModel.request_id == request.id,
                SignatureRevisionModel.sha256 == digest,
                SignatureRevisionModel.number >= 1,
            )
        )
    ).scalar_one_or_none()
    if number is None:
        raise ErrorException(errors.signature.DOCUMENT_CHANGED, status_code=409)
    return int(number)


async def _rate_limit(local: AsyncSession, sub: str, now: datetime.datetime) -> None:
    count = (
        await local.execute(
            select(func.count(SignatureEventModel.id)).where(
                SignatureEventModel.actor_sub == sub,
                SignatureEventModel.type.in_(
                    (EventType.SIGNER_REAUTHENTICATED.value, EventType.SIGNER_DECLINED.value)
                ),
                SignatureEventModel.at >= now - datetime.timedelta(minutes=1),
            )
        )
    ).scalar_one()
    if count >= SIGN_CALLS_PER_MINUTE:
        raise ErrorException(errors.signature.TOO_MANY_ATTEMPTS, status_code=429)


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


async def view(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    document_sha256: str,
    *,
    caller: Caller,
    local: AsyncSession,
    crm: AsyncSession,
) -> None:
    request, signer = await _load(local, request_id, signer_id, lock=True)
    await require_module(crm, request)
    if not await may_sign(crm, signer, request, caller.user):
        raise ErrorException(errors.signature.NOT_YOUR_SLOT, status_code=403)
    revision = await _revision_for(local, request, document_sha256)
    if request.status != RequestStatus.OPEN or signer.status not in _OPEN_SLOT:
        await local.rollback()
        return
    # One event per viewing, not per page refresh: the same revision viewed
    # by the same account within ten minutes is already recorded.
    recent = (
        await local.execute(
            select(SignatureEventModel.id).where(
                SignatureEventModel.request_id == request.id,
                SignatureEventModel.signer_id == signer.id,
                SignatureEventModel.type == EventType.SIGNER_VIEWED.value,
                SignatureEventModel.actor_sub == caller.user.sub,
                SignatureEventModel.document_sha256 == document_sha256,
                SignatureEventModel.at >= utcnow() - datetime.timedelta(minutes=10),
            )
        )
    ).first()
    if recent is None:
        await append_event(
            local,
            request,
            EventType.SIGNER_VIEWED,
            actor=caller.actor,
            signer_id=signer.id,
            document_sha256=document_sha256,
            payload={"revision": revision},
        )
    signer.status = int(SignerStatus.VIEWED)
    await local.commit()
    metrics.signer_actions.add(1, {"action": "view"})


# ---------------------------------------------------------------------------
# sign
# ---------------------------------------------------------------------------


def _decode_png(value: str) -> bytes:
    raw = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ErrorException(errors.signature.INVALID_SIGNATURE_IMAGE, status_code=422) from None
    if not data or len(data) > MAX_DRAWN_PNG_BYTES:
        raise ErrorException(errors.signature.INVALID_SIGNATURE_IMAGE, status_code=422)
    try:
        image = Image.open(io.BytesIO(data))
        if image.format != "PNG":
            raise ErrorException(errors.signature.INVALID_SIGNATURE_IMAGE, status_code=422)
        if image.width > _MAX_IMAGE_SIZE[0] or image.height > _MAX_IMAGE_SIZE[1]:
            raise ErrorException(errors.signature.INVALID_SIGNATURE_IMAGE, status_code=422)
        image.load()
    except (UnidentifiedImageError, OSError):
        raise ErrorException(errors.signature.INVALID_SIGNATURE_IMAGE, status_code=422) from None
    # Re-encoded: what we keep and stamp is pixels only, no metadata chunks.
    clean = io.BytesIO()
    image.convert("RGBA").save(clean, "PNG", optimize=True)
    return clean.getvalue()


def _check_fields(own: list[SignatureFieldModel], given: dict[str, str]) -> dict[str, str]:
    by_name = {f.name: f for f in own}
    unknown = set(given) - {name for name, f in by_name.items() if f.type != FieldType.DATE}
    if unknown:
        raise ErrorException(errors.signature.INVALID_FIELD_VALUE, status_code=422)
    values: dict[str, str] = {}
    for name, item in by_name.items():
        if item.type == FieldType.DATE:
            continue
        value = given.get(name)
        if item.type == FieldType.CHECKBOX:
            if value not in (None, "true", "false"):
                raise ErrorException(errors.signature.INVALID_FIELD_VALUE, status_code=422)
            if item.required and value != "true":
                raise ErrorException(errors.signature.REQUIRED_FIELD_MISSING, status_code=422)
            values[name] = value or "false"
            continue
        text = (value or "").strip()
        if len(text) > MAX_TEXT_FIELD_LENGTH or _CONTROL.search(text):
            raise ErrorException(errors.signature.INVALID_FIELD_VALUE, status_code=422)
        if item.required and not text:
            raise ErrorException(errors.signature.REQUIRED_FIELD_MISSING, status_code=422)
        if text:
            values[name] = text
    return values


async def sign(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    body: SignInput,
    *,
    caller: Caller,
    local: AsyncSession,
    crm: AsyncSession,
    store: DocumentStore,
    bus: Bus,
) -> None:
    now = utcnow()
    await _rate_limit(local, caller.user.sub, now)
    request, signer = await _load(local, request_id, signer_id, lock=True)
    await require_module(crm, request)
    if not await may_sign(crm, signer, request, caller.user):
        raise ErrorException(errors.signature.NOT_YOUR_SLOT, status_code=403)
    if request.status != RequestStatus.OPEN:
        raise ErrorException(errors.signature.REQUEST_NOT_OPEN, status_code=409)
    if signer.status not in _OPEN_SLOT:
        raise ErrorException(errors.signature.SLOT_NOT_OPEN, status_code=409)
    viewed = await _revision_for(local, request, body.document_sha256)
    if not body.consent_accepted or body.consent_version != request.consent_version:
        raise ErrorException(errors.signature.CONSENT_REQUIRED, status_code=422)
    typed = (body.typed_name or "").strip()
    if body.method is SignMethod.TYPED and not typed:
        raise ErrorException(errors.signature.TYPED_NAME_REQUIRED, status_code=422)
    if len(typed) > 255 or _CONTROL.search(typed):
        raise ErrorException(errors.signature.TYPED_NAME_REQUIRED, status_code=422)
    own = list(
        (
            await local.execute(
                select(SignatureFieldModel).where(SignatureFieldModel.signer_id == signer.id)
            )
        ).scalars()
    )
    values = _check_fields(own, body.fields)
    image = _decode_png(body.image_png)
    image_sha = hashlib.sha256(image).hexdigest()
    key = keys.image_key(request.id_community, request.id, signer.id)
    await store.put(key, image, "image/png")

    community = await CrmRead(crm).community_name(request.id_community)
    consent = wording.consent(
        request.consent_version,
        request.locale,
        Capacity(signer.capacity),
        member=signer.member_label,
        community=community,
    )
    for item in own:
        if item.name in values:
            item.value = values[item.name]
    signer.status = int(SignerStatus.SIGNING)
    signer.method = int(body.method)
    signer.typed_name = typed or None
    signer.image_key = key
    signer.image_sha256 = image_sha
    signer.signed_by_sub = caller.user.sub
    signer.signed_by_user_id = caller.user.id
    signer.viewed_revision = viewed
    await append_event(
        local,
        request,
        EventType.SIGNER_REAUTHENTICATED,
        actor=caller.actor,
        signer_id=signer.id,
        document_sha256=body.document_sha256,
        consent_version=request.consent_version,
        payload={
            "viewed_revision": viewed,
            "method": body.method.name,
            "typed_name": typed or None,
            "image_sha256": image_sha,
            "fields": values,
            "consent_sha256": consent.sha256,
            "consent_text": consent.text,
        },
        at=now,
    )
    await local.commit()
    metrics.signer_actions.add(1, {"action": "sign"})
    try:
        await bus.job(*sign_job(request.id, signer.id))
    except Exception:
        # The slot is SIGNING and the worker's requeue pass picks it up.
        logger.exception("could not queue the seal of signer %s; the requeue pass will", signer.id)


# ---------------------------------------------------------------------------
# decline
# ---------------------------------------------------------------------------


async def decline(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    reason: str,
    *,
    caller: Caller,
    local: AsyncSession,
    crm: AsyncSession,
    bus: Bus,
) -> None:
    text = (reason or "").strip()
    if not text or len(text) > 1000 or _CONTROL.search(text):
        raise ErrorException(errors.signature.DECLINE_REASON_REQUIRED, status_code=422)
    now = utcnow()
    await _rate_limit(local, caller.user.sub, now)
    request, signer = await _load(local, request_id, signer_id, lock=True)
    await require_module(crm, request)
    if not await may_sign(crm, signer, request, caller.user):
        raise ErrorException(errors.signature.NOT_YOUR_SLOT, status_code=403)
    if request.status != RequestStatus.OPEN:
        raise ErrorException(errors.signature.REQUEST_NOT_OPEN, status_code=409)
    if signer.status not in _OPEN_SLOT:
        raise ErrorException(errors.signature.SLOT_NOT_OPEN, status_code=409)
    signer.status = int(SignerStatus.DECLINED)
    signer.declined_at = now
    signer.decline_reason = text
    signer.signed_by_sub = None
    request.status = int(RequestStatus.DECLINED)
    request.closed_at = now
    await append_event(
        local,
        request,
        EventType.SIGNER_DECLINED,
        actor=caller.actor,
        signer_id=signer.id,
        payload={"reason": text},
        at=now,
    )
    notifier = Notifier(crm)
    await notifier.declined(request, signer)
    await notifier.commit()
    await local.commit()
    await notifier.send_hints()
    metrics.signer_actions.add(1, {"action": "decline"})
    await bus.domain_event(
        request.source_service,
        DomainEvent.DECLINED,
        {
            **accepted_data(request),
            "signer_id": str(signer.id),
            "position": signer.position,
            "id_member": signer.id_member,
            "reason": text,
        },
    )


# ---------------------------------------------------------------------------
# a manager: remind
# ---------------------------------------------------------------------------


async def remind(
    request_id: uuid.UUID,
    *,
    caller: Caller,
    local: AsyncSession,
    crm: AsyncSession,
) -> int:
    request = await lock_request(local, request_id)
    if request is None:
        raise ErrorException(errors.signature.REQUEST_NOT_FOUND, status_code=404)
    await require_module(crm, request)
    if not await may_follow(crm, request, caller.user):
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    if request.status != RequestStatus.OPEN:
        raise ErrorException(errors.signature.REQUEST_NOT_OPEN, status_code=409)
    now = utcnow()
    if request.last_reminded_at and now - request.last_reminded_at < datetime.timedelta(
        hours=REMIND_INTERVAL_HOURS
    ):
        raise ErrorException(errors.signature.REMINDED_RECENTLY, status_code=409)
    pending = list(
        (
            await local.execute(
                select(SignatureSignerModel)
                .where(
                    SignatureSignerModel.request_id == request.id,
                    SignatureSignerModel.status.in_([int(s) for s in _OPEN_SLOT]),
                )
                .order_by(SignatureSignerModel.position)
            )
        ).scalars()
    )
    request.last_reminded_at = now
    await append_event(
        local,
        request,
        EventType.REQUEST_REMINDED,
        actor=caller.actor,
        payload={"signers": [s.position for s in pending]},
        at=now,
    )
    notifier = Notifier(crm)
    await notifier.invited(request, pending, reminder_on=now.date())
    await notifier.commit()
    await local.commit()
    await notifier.send_hints()
    return len(pending)
