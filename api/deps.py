"""Request-scoped dependencies: who is calling (from the gateway), sessions, store, bus.

The gateway (KrakenD) validates the Keycloak token and forwards its claims as
headers: ``x-user-id`` (sub), and for this service ``x-auth-time`` (the last
interactive login, epoch seconds), ``x-auth-sid`` and ``x-auth-acr``; nginx
sets ``x-source-ip`` and blanks every one of these on the way in, so a client
cannot supply its own. The service trusts them as every annexe trusts
``x-user-id``: it is only reachable through the gateway.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.database import get_crm_session, get_local_session
from core.errors.errors import ErrorException
from core.queue.init import get_jetstream
from ports.bus import Bus, NatsBus
from ports.storage import DocumentStore, S3Store
from service.log import Actor, utcnow
from service.signer_actions import Caller, caller_for
from shared.custom_errors import errors

LocalSession = Annotated[AsyncSession, Depends(get_local_session)]
CrmSession = Annotated[AsyncSession, Depends(get_crm_session)]


def _auth_time(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(float(raw)), tz=datetime.UTC)
    except (ValueError, OverflowError, OSError):
        return None


def actor_from(request: Request) -> Actor:
    headers = request.headers
    return Actor(
        sub=headers.get("x-user-id") or None,
        ip=(headers.get("x-source-ip") or None),
        user_agent=(headers.get("user-agent") or "")[:512] or None,
        keycloak_sid=headers.get("x-auth-sid") or None,
        auth_time=_auth_time(headers.get("x-auth-time")),
        acr=(headers.get("x-auth-acr") or None),
    )


async def current_caller(request: Request, crm: CrmSession) -> Caller:
    return await caller_for(crm, actor_from(request))


async def fresh_caller(request: Request, crm: CrmSession) -> Caller:
    """The caller, who logged in interactively within STEP_UP_MAX_AGE_SECONDS.

    A refreshed token keeps its ``auth_time``: only a real login moves it.
    The front answers ``step_up_required`` by sending the user through
    Keycloak with ``max_age=0`` and back to the same screen.
    """
    caller = await current_caller(request, crm)
    window = settings.STEP_UP_MAX_AGE_SECONDS
    if window > 0:
        auth_time = caller.actor.auth_time
        if auth_time is None or (utcnow() - auth_time).total_seconds() > window:
            raise ErrorException(errors.signature.STEP_UP_REQUIRED, status_code=401)
    return caller


def get_store() -> DocumentStore:
    return S3Store()


async def get_bus() -> AsyncGenerator[Bus, None]:
    try:
        yield NatsBus(get_jetstream())
    except RuntimeError:
        raise ErrorException(errors.signature.QUEUE_UNAVAILABLE, status_code=503) from None


CallerDep = Annotated[Caller, Depends(current_caller)]
FreshCallerDep = Annotated[Caller, Depends(fresh_caller)]
StoreDep = Annotated[DocumentStore, Depends(get_store)]
BusDep = Annotated[Bus, Depends(get_bus)]
