"""ORM mapping of the LOCAL database (scripts/sql/schema.sql is the source).

The CHECKs and triggers live in the DDL only; these classes map the columns.
`signature_event` and `signature_revision` are append-only: never update or
delete through them.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.database import LocalBase


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class SignatureRequestModel(LocalBase):
    __tablename__ = "signature_request"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    source_service: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    original_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signing_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    consent_version: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_sub: Mapped[str | None] = mapped_column(String(255))
    requested_by_user_id: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    final_key: Mapped[str | None] = mapped_column(Text)
    final_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_json_key: Mapped[str | None] = mapped_column(Text)
    evidence_json_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_pdf_key: Mapped[str | None] = mapped_column(Text)
    evidence_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    last_reminded_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class SignatureSignerModel(LocalBase):
    __tablename__ = "signature_signer"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signature_request.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    id_member: Mapped[int | None] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    expected_user_sub: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    member_label: Mapped[str | None] = mapped_column(String(255))
    role_label: Mapped[str] = mapped_column(String(255), nullable=False)
    slot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    slot_resolved: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    method: Mapped[int | None] = mapped_column(SmallInteger)
    typed_name: Mapped[str | None] = mapped_column(String(255))
    image_key: Mapped[str | None] = mapped_column(Text)
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    signed_by_sub: Mapped[str | None] = mapped_column(String(255))
    signed_by_user_id: Mapped[int | None] = mapped_column(Integer)
    viewed_revision: Mapped[int | None] = mapped_column(Integer)
    signed_revision: Mapped[int | None] = mapped_column(Integer)
    invited_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    signed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    declined_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    decline_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class SignatureFieldModel(LocalBase):
    __tablename__ = "signature_field"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    signer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signature_signer.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    label: Mapped[str | None] = mapped_column(String(255))
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    slot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    slot_resolved: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pdf_field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    filled_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class SignatureRevisionModel(LocalBase):
    __tablename__ = "signature_revision"

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signature_request.id"), primary_key=True
    )
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    signer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signature_signer.id")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now
    )


class SignatureEventModel(LocalBase):
    __tablename__ = "signature_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signature_request.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    signer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signature_signer.id")
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    actor_sub: Mapped[str | None] = mapped_column(String(255))
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    keycloak_sid: Mapped[str | None] = mapped_column(String(255))
    auth_time: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    acr: Mapped[str | None] = mapped_column(String(64))
    document_sha256: Mapped[str | None] = mapped_column(String(64))
    consent_version: Mapped[str | None] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
