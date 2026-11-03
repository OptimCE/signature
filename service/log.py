"""Appending to the evidence log.

The caller holds the request row locked (``lock_request``) for the whole
transaction: the chain is per request, and two appends racing for the same
``seq`` would make one fail at COMMIT. The lock makes them wait instead.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.evidence import event_hash
from shared.const import EventType
from shared.models.local_models import SignatureEventModel, SignatureRequestModel


@dataclass(frozen=True)
class Actor:
    """Who did it, as the gateway told us (empty for the system)."""

    sub: str | None = None
    user_id: int | None = None
    ip: str | None = None
    user_agent: str | None = None
    keycloak_sid: str | None = None
    auth_time: datetime.datetime | None = None
    acr: str | None = None


SYSTEM = Actor()


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def lock_request(
    session: AsyncSession, request_id: uuid.UUID
) -> SignatureRequestModel | None:
    result = await session.execute(
        select(SignatureRequestModel)
        .where(SignatureRequestModel.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def append_event(
    session: AsyncSession,
    request: SignatureRequestModel,
    type: EventType,
    *,
    actor: Actor = SYSTEM,
    signer_id: uuid.UUID | None = None,
    document_sha256: str | None = None,
    consent_version: str | None = None,
    payload: dict[str, Any] | None = None,
    at: datetime.datetime | None = None,
) -> SignatureEventModel:
    last = (
        await session.execute(
            select(SignatureEventModel.seq, SignatureEventModel.hash)
            .where(SignatureEventModel.request_id == request.id)
            .order_by(SignatureEventModel.seq.desc())
            .limit(1)
        )
    ).first()
    seq = (last[0] + 1) if last else 1
    prev_hash = last[1] if last else request.original_sha256
    # Microsecond precision is what timestamptz keeps: hash exactly that.
    row: dict[str, Any] = {
        "request_id": request.id,
        "seq": seq,
        "signer_id": signer_id,
        "type": type.value,
        "at": (at or utcnow()).astimezone(datetime.UTC),
        "actor_sub": actor.sub,
        "actor_user_id": actor.user_id,
        "ip": actor.ip,
        "user_agent": actor.user_agent,
        "keycloak_sid": actor.keycloak_sid,
        "auth_time": actor.auth_time,
        "acr": actor.acr,
        "document_sha256": document_sha256,
        "consent_version": consent_version,
        "payload": payload or {},
        "prev_hash": prev_hash,
    }
    event = SignatureEventModel(**row, hash=event_hash(row))
    session.add(event)
    await session.flush()
    return event


def event_row(event: SignatureEventModel) -> dict[str, Any]:
    """A stored event as the mapping ``domain.evidence`` hashes."""
    return {
        "request_id": event.request_id,
        "seq": event.seq,
        "signer_id": event.signer_id,
        "type": event.type,
        "at": event.at,
        "actor_sub": event.actor_sub,
        "actor_user_id": event.actor_user_id,
        "ip": event.ip,
        "user_agent": event.user_agent,
        "keycloak_sid": event.keycloak_sid,
        "auth_time": event.auth_time,
        "acr": event.acr,
        "document_sha256": event.document_sha256,
        "consent_version": event.consent_version,
        "payload": event.payload,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
    }
