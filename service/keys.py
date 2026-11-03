"""Where everything of a request lives in OUTPUT_BUCKET: signature/{community}/{request}/.

Every key is written once. A revision has its number in its name, so nothing
is ever overwritten; a retried job that died before its commit writes the
same key again with bytes nobody references yet.
"""

from __future__ import annotations

import uuid

from shared.const import SOURCE_DOCUMENT_PREFIXES, STORAGE_KEY_PREFIX


def request_folder(id_community: int, request_id: uuid.UUID) -> str:
    return f"{STORAGE_KEY_PREFIX}/{id_community}/{request_id}"


def revision_key(id_community: int, request_id: uuid.UUID, number: int) -> str:
    return f"{request_folder(id_community, request_id)}/rev-{number}.pdf"


def final_key(id_community: int, request_id: uuid.UUID) -> str:
    return f"{request_folder(id_community, request_id)}/final.pdf"


def image_key(id_community: int, request_id: uuid.UUID, signer_id: uuid.UUID) -> str:
    return f"{request_folder(id_community, request_id)}/signature-{signer_id}.png"


def evidence_json_key(id_community: int, request_id: uuid.UUID) -> str:
    return f"{request_folder(id_community, request_id)}/evidence.json"


def evidence_pdf_key(id_community: int, request_id: uuid.UUID) -> str:
    return f"{request_folder(id_community, request_id)}/evidence.pdf"


def evidence_render_prefix(id_community: int, request_id: uuid.UUID) -> str:
    """Where docgen writes the unsealed evidence report (its own key layout below it)."""
    return f"{request_folder(id_community, request_id)}/evidence-render"


def document_allowed(source_service: str, id_community: int, key: str) -> bool:
    """A domain may only have a document under its own prefix signed."""
    template = SOURCE_DOCUMENT_PREFIXES.get(source_service)
    if template is None:
        return False
    prefix = template.format(id_community=id_community)
    return key.startswith(prefix) and ".." not in key
