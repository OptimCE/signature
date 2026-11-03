# No `from __future__ import annotations` (see api/signer/routes.py).
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.deps import CrmSession, LocalSession
from core.api_response import ApiResponse
from core.context_vars import current_internal_community_id
from core.errors.errors import ErrorException
from core.security.community_scope import resolve_internal_community
from core.security.dependencies import require_feature, require_min_role
from core.security.user_context import Role
from service import views
from shared.const import FeatureName
from shared.custom_errors import errors

# The active community's follow-up list: managers and admins, module on.
admin_routes = APIRouter(
    prefix="/admin",
    # resolve_internal_community first: require_feature scopes its read with
    # the internal id it sets.
    dependencies=[
        Depends(require_min_role(Role.MANAGER)),
        Depends(resolve_internal_community),
        Depends(require_feature(FeatureName.SIGNATURE)),
    ],
)


@admin_routes.get("/requests", response_model=ApiResponse[dict[str, Any]])
async def community_requests(
    local: LocalSession,
    crm: CrmSession,
    status: str | None = Query(default=None, max_length=16),
    source: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """The community's requests, newest first, with their progress."""
    community = current_internal_community_id.get()
    if community is None:
        raise ErrorException(errors.auth.AUTHORIZATION_MISSING, status_code=401)
    data = await views.community_requests(
        community, local=local, crm=crm, status=status, source=source, limit=limit, offset=offset
    )
    return ApiResponse(data=data)
