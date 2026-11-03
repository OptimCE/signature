"""The NATS contract a requesting domain speaks: ``signature.request.create`` / ``.cancel``.

Bodies travel in the ``Event`` envelope of ``core/queue/helper.py`` (``data``
holds the models below). Ids are the CRM's INTs; users are Keycloak ``sub``s.
A malformed body is permanent: acknowledged, answered with ``failed``.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.const import MAX_FIELDS_PER_SIGNER, MAX_SIGNERS

_KEY = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$"


class FieldIn(BaseModel):
    """A field the signer fills; DATE is filled by the server, never by the signer."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    type: Literal["DATE", "INITIALS", "CHECKBOX", "TEXT"]
    label: str | None = Field(default=None, max_length=255)
    required: bool = False
    slot: dict[str, Any]


class SignerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The member signed for; absent for a COMMUNITY_ADMIN slot.
    member_id: int | None = Field(default=None, gt=0)
    # Derived from the member when absent: an individual signs as SELF, a
    # company through its LEGAL_REPRESENTATIVE.
    capacity: Literal["SELF", "LEGAL_REPRESENTATIVE", "COMMUNITY_ADMIN"] | None = None
    # The Keycloak sub of the admin who signs a COMMUNITY_ADMIN slot.
    user: str | None = Field(default=None, min_length=1, max_length=255)
    role_label: str = Field(min_length=1, max_length=255)
    slot: dict[str, Any]
    fields: list[FieldIn] = Field(default_factory=list, max_length=MAX_FIELDS_PER_SIGNER)

    @model_validator(mode="after")
    def _who(self) -> SignerIn:
        if self.capacity == "COMMUNITY_ADMIN":
            if self.user is None or self.member_id is not None:
                raise ValueError("a COMMUNITY_ADMIN slot names a user, not a member")
        elif self.member_id is None or self.user is not None:
            raise ValueError("a member's slot names a member_id, not a user")
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("field names repeat")
        return self


class CreateRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(pattern=_KEY)
    source_service: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    source_ref: str = Field(min_length=1, max_length=255)
    community_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=255)
    locale: Literal["fr", "en", "nl", "de"] = "fr"
    document_key: str = Field(min_length=1, max_length=1024)
    signing_order: Literal["PARALLEL", "SEQUENTIAL"] = "PARALLEL"
    expires_at: datetime.datetime | None = None
    # The Keycloak sub of the user who asked; absent when a job asked.
    requested_by: str | None = Field(default=None, min_length=1, max_length=255)
    signers: list[SignerIn] = Field(min_length=1, max_length=MAX_SIGNERS)

    @model_validator(mode="after")
    def _expiry(self) -> CreateRequestIn:
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at carries its time zone")
        if ".." in self.document_key or self.document_key.startswith("/"):
            raise ValueError("document_key is a bucket key")
        return self


class CancelRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_service: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    idempotency_key: str = Field(pattern=_KEY)
    reason: str | None = Field(default=None, max_length=1000)
    requested_by: str | None = Field(default=None, min_length=1, max_length=255)
