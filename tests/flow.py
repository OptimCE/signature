"""Driving a request through its life in tests: create, prepare, sign, complete."""

from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from ports.bus import RecordingBus
from ports.storage import MemoryStore
from service import evidence_report, jobs, requests
from shared.const import CONSENT_VERSION, DomainEvent
from shared.models.local_models import SignatureRequestModel, SignatureSignerModel
from tests.world import User, World, anchored_pdf, headers, png_bytes

SOURCE = "administrative_document"


def doc_key(world: World, name: str = "piece") -> str:
    return f"administrative-document/{world.community}/42/v1/{name}-{uuid.uuid4().hex[:8]}.pdf"


def annex8_payload(
    world: World, key: str, *, idempotency_key: str | None = None, member: int | None = None
) -> dict[str, Any]:
    return {
        "idempotency_key": idempotency_key or f"admin-doc-{uuid.uuid4().hex[:10]}",
        "source_service": SOURCE,
        "source_ref": "document:42:v1",
        "community_id": world.community,
        "title": "Déclaration sur l'honneur — annexe 8",
        "locale": "fr",
        "document_key": key,
        "signing_order": "SEQUENTIAL",
        "requested_by": world.mike.sub,
        "signers": [
            {
                "member_id": member or world.alice_member,
                "role_label": "Participant",
                "slot": {"field": "Champ de signature 1"},
                "fields": [
                    {
                        "name": "place",
                        "type": "TEXT",
                        "label": "Fait à",
                        "required": True,
                        "slot": {"field": "Champ de texte 21"},
                    },
                    {"name": "date", "type": "DATE", "slot": {"field": "Champ de texte 22"}},
                ],
            }
        ],
    }


ANCHORS = [("[[sig:a]]", 100, 600), ("[[txt:a_place]]", 100, 520), ("[[chk:a_bylaws]]", 100, 480)]


def two_signer_payload(world: World, key: str, *, order: str = "SEQUENTIAL") -> dict[str, Any]:
    return {
        "idempotency_key": f"conv-{uuid.uuid4().hex[:10]}",
        "source_service": SOURCE,
        "source_ref": "convention:7",
        "community_id": world.community,
        "title": "Convention de partage 2027",
        "locale": "fr",
        "document_key": key,
        "signing_order": order,
        "requested_by": world.mike.sub,
        "signers": [
            {
                "member_id": world.alice_member,
                "role_label": "Membre",
                "slot": {"anchor": "[[sig:a]]"},
                "fields": [
                    {
                        "name": "place",
                        "type": "TEXT",
                        "label": "Fait à",
                        "required": True,
                        "slot": {"anchor": "[[txt:a_place]]", "width": 160, "height": 16},
                    },
                    {
                        "name": "bylaws",
                        "type": "CHECKBOX",
                        "label": "J'accepte les statuts",
                        "required": True,
                        "slot": {"anchor": "[[chk:a_bylaws]]"},
                    },
                    {
                        "name": "date",
                        "type": "DATE",
                        "slot": {"box": {"page": 1, "rect": [320, 520, 470, 536]}},
                    },
                ],
            },
            {
                "member_id": world.acme_member,
                "role_label": "Membre (société)",
                "slot": {"box": {"page": 1, "rect": [320, 300, 540, 360]}},
            },
        ],
    }


async def create(
    session: AsyncSession,
    store: MemoryStore,
    bus: RecordingBus,
    payload: dict[str, Any],
    pdf: bytes,
) -> SignatureRequestModel | None:
    store.objects[payload["document_key"]] = pdf
    return await requests.handle_create(payload, local=session, crm=session, store=store, bus=bus)


async def prepare(
    session: AsyncSession,
    store: MemoryStore,
    bus: RecordingBus,
    material: Any,
    request_id: uuid.UUID,
) -> None:
    await jobs.run_prepare(
        request_id, local=session, crm=session, store=store, bus=bus, material=lambda: material
    )


async def signers(session: AsyncSession, request_id: uuid.UUID) -> list[SignatureSignerModel]:
    return list(
        (
            await session.execute(
                select(SignatureSignerModel)
                .where(SignatureSignerModel.request_id == request_id)
                .order_by(SignatureSignerModel.position)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def displayed_sha(client: AsyncClient, user: User, request_id: uuid.UUID) -> str:
    response = await client.get(f"/requests/{request_id}/document", headers=headers(user))
    assert response.status_code == 200, response.text
    return hashlib.sha256(response.content).hexdigest()


def sign_body(
    sha: str, *, fields: dict[str, str] | None = None, method: str = "TYPED"
) -> dict[str, Any]:
    return {
        "method": method,
        "typed_name": "Alice Martin" if method == "TYPED" else None,
        "image_png": "data:image/png;base64," + base64.b64encode(png_bytes()).decode(),
        "fields": fields or {},
        "consent_version": CONSENT_VERSION,
        "consent_accepted": True,
        "document_sha256": sha,
    }


async def sign(
    client: AsyncClient,
    user: User,
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    *,
    fields: dict[str, str] | None = None,
    sha: str | None = None,
    auth_age_seconds: int = 30,
) -> Any:
    digest = sha or await displayed_sha(client, user, request_id)
    return await client.post(
        f"/requests/{request_id}/signers/{signer_id}/sign",
        json=sign_body(digest, fields=fields),
        headers=headers(user, auth_age_seconds=auth_age_seconds),
    )


async def seal(
    session: AsyncSession,
    store: MemoryStore,
    bus: RecordingBus,
    material: Any,
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
) -> None:
    await jobs.run_sign(
        request_id,
        signer_id,
        local=session,
        crm=session,
        store=store,
        bus=bus,
        material=lambda: material,
    )


async def complete(
    session: AsyncSession,
    store: MemoryStore,
    bus: RecordingBus,
    material: Any,
    request_id: uuid.UUID,
) -> str:
    """The complete job, then docgen's answer (a plain PDF in the evidence's place)."""
    await jobs.run_complete(
        request_id, local=session, crm=session, store=store, bus=bus, material=lambda: material
    )
    _subject, body = bus.docgen_requests[-1]
    key = f"{body['key_prefix']}/evidence.pdf"
    rendered = anchored_pdf([])
    store.objects[key] = rendered
    result = {
        "request_id": body["request_id"],
        "status": "success",
        "artifacts": [
            {
                "format": "pdf",
                "uri": f"s3://test-documents/{key}",
                "size_bytes": len(rendered),
                "sha256": hashlib.sha256(rendered).hexdigest(),
            }
        ],
        "metadata": body["metadata"],
    }
    return await evidence_report.handle_docgen_result(
        result, local=session, crm=session, store=store, bus=bus, material=lambda: material
    )


def events_of(bus: RecordingBus, kind: DomainEvent) -> list[dict[str, Any]]:
    return [data for _source, event, data in bus.events if event is kind]


TEMPLATES = Path(__file__).resolve().parent.parent / "document-templates"


def render_evidence_html(evidence: dict[str, Any], json_sha256: str, locale: str) -> str:
    """The configured evidence template, rendered as document-generation's
    jinja-html engine does (same loader, autoescape and StrictUndefined)."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

    folder = settings.EVIDENCE_TEMPLATE_URI.rstrip("/").split("/", 3)[3]
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES / folder)),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,
    )
    data = {"evidence": evidence, "evidence_json_sha256": json_sha256}
    return env.get_template("template.html").render(data=data, locale=locale)
