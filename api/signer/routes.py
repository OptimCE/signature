# No `from __future__ import annotations` here: with_default_error resolves the
# route's annotations against its own globals, and stringified Pydantic bodies
# would be demoted to query parameters (administrative-document's CLAUDE.md).
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Response, status

from api.deps import BusDep, CallerDep, CrmSession, FreshCallerDep, LocalSession, StoreDep
from api.signer.schemas import Accepted, CancelIn, DeclineIn, Json, Reminded, SignIn, ViewIn
from core.api_response import ApiResponse
from core.errors.errors import ErrorException
from service import signer_actions, views
from service.requests import cancel as cancel_request
from service.signer_actions import SignInput
from shared.const import SignMethod
from shared.custom_errors import errors

signer_routes = APIRouter()

_PDF: dict[int | str, dict[str, Any]] = {200: {"content": {"application/pdf": {}}}}
_FILE: dict[int | str, dict[str, Any]] = {200: {"content": {"application/octet-stream": {}}}}


@signer_routes.get("/me/requests", response_model=ApiResponse[list[Json]])
async def my_requests(
    caller: CallerDep,
    local: LocalSession,
    crm: CrmSession,
    request_status: Literal["pending", "done"] | None = Query(default=None, alias="status"),
):
    """My signatures, pending and past, across my members and communities."""
    items = await views.my_requests(caller=caller, local=local, crm=crm, status=request_status)
    return ApiResponse(data=items)


@signer_routes.get("/requests/{request_id}", response_model=ApiResponse[Json])
async def get_request(
    request_id: uuid.UUID, caller: CallerDep, local: LocalSession, crm: CrmSession
):
    """A request, its signers, my slots (with the consent text and my fields)."""
    return ApiResponse(data=await views.detail(request_id, caller=caller, local=local, crm=crm))


@signer_routes.get("/requests/{request_id}/document", responses=_PDF, response_class=Response)
async def get_document(
    request_id: uuid.UUID, caller: CallerDep, local: LocalSession, crm: CrmSession, store: StoreDep
):
    """The current revision. The browser hashes these bytes and sends the hash back."""
    content, number, digest = await views.document(
        request_id, caller=caller, local=local, crm=crm, store=store
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="rev-{number}.pdf"',
            "X-Document-Revision": str(number),
            "X-Document-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )


@signer_routes.post(
    "/requests/{request_id}/signers/{signer_id}/view", status_code=status.HTTP_204_NO_CONTENT
)
async def view(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    body: ViewIn,
    caller: CallerDep,
    local: LocalSession,
    crm: CrmSession,
):
    await signer_actions.view(
        request_id, signer_id, body.document_sha256, caller=caller, local=local, crm=crm
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@signer_routes.post(
    "/requests/{request_id}/signers/{signer_id}/sign",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[Accepted],
)
async def sign(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    body: SignIn,
    caller: FreshCallerDep,
    local: LocalSession,
    crm: CrmSession,
    store: StoreDep,
    bus: BusDep,
):
    """Sign: needs a login younger than the step-up window. 202, sealed by the worker."""
    await signer_actions.sign(
        request_id,
        signer_id,
        SignInput(
            method=SignMethod[body.method],
            typed_name=body.typed_name,
            image_png=body.image_png,
            fields=body.fields,
            consent_version=body.consent_version,
            consent_accepted=body.consent_accepted,
            document_sha256=body.document_sha256,
        ),
        caller=caller,
        local=local,
        crm=crm,
        store=store,
        bus=bus,
    )
    return ApiResponse(data=Accepted(status="signing"))


@signer_routes.post(
    "/requests/{request_id}/signers/{signer_id}/decline", status_code=status.HTTP_204_NO_CONTENT
)
async def decline(
    request_id: uuid.UUID,
    signer_id: uuid.UUID,
    body: DeclineIn,
    caller: CallerDep,
    local: LocalSession,
    crm: CrmSession,
    bus: BusDep,
):
    await signer_actions.decline(
        request_id, signer_id, body.reason, caller=caller, local=local, crm=crm, bus=bus
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@signer_routes.get(
    "/requests/{request_id}/artifacts/{kind}", responses=_FILE, response_class=Response
)
async def get_artifact(
    request_id: uuid.UUID,
    kind: Literal["final", "evidence-pdf", "evidence-json"],
    caller: CallerDep,
    local: LocalSession,
    crm: CrmSession,
    store: StoreDep,
):
    content, media_type, filename = await views.artifact(
        request_id, kind, caller=caller, local=local, crm=crm, store=store
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- the follow-up (managers and admins of the request's community) -------------


@signer_routes.get("/requests/{request_id}/events", response_model=ApiResponse[Json])
async def get_events(
    request_id: uuid.UUID, caller: CallerDep, local: LocalSession, crm: CrmSession
):
    """The evidence log in order, with the chain recomputed."""
    return ApiResponse(data=await views.timeline(request_id, caller=caller, local=local, crm=crm))


@signer_routes.post("/requests/{request_id}/remind", response_model=ApiResponse[Reminded])
async def remind(request_id: uuid.UUID, caller: CallerDep, local: LocalSession, crm: CrmSession):
    """Remind the signers whose turn it is; at most once a day per request."""
    count = await signer_actions.remind(request_id, caller=caller, local=local, crm=crm)
    return ApiResponse(data=Reminded(reminded=count))


@signer_routes.post("/requests/{request_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel(
    request_id: uuid.UUID,
    body: CancelIn,
    caller: CallerDep,
    local: LocalSession,
    crm: CrmSession,
    bus: BusDep,
):
    seen = await views.access(request_id, caller=caller, local=local, crm=crm)
    if not seen.follower:
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    await local.rollback()
    await cancel_request(
        request_id, local=local, crm=crm, bus=bus, actor=caller.actor, reason=body.reason
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
