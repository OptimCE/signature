from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The SHA-256 the browser computed over the bytes it displays.
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["TYPED", "DRAWN"]
    typed_name: str | None = Field(default=None, max_length=255)
    # The signature as the signer saw it: a PNG, base64 (a data URL is fine).
    image_png: str = Field(min_length=16, max_length=420_000)
    fields: dict[str, str] = Field(default_factory=dict)
    consent_version: str = Field(min_length=1, max_length=16)
    consent_accepted: bool
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


class Accepted(BaseModel):
    status: str


class Reminded(BaseModel):
    reminded: int


# The read models are plain dicts built by service/views.py; typed loosely here
# so the OpenAPI the gateway is generated from stays readable.
Json = dict[str, Any]
