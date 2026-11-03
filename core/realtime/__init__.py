"""Shared realtime publisher — BYTE-IDENTICAL across every producing service.

Copied verbatim into billing, administrative-document, news-board,
allocation-key-generation and simulation-key, mirroring the ``core/notifications``
convention. ``scripts/check-realtime-parity.sh`` at the monorepo root is the gate:
make the change in the reference service (news-board) and copy it out, never edit
one copy.

Consumed by crm-backend's realtime hub (``src/shared/realtime/``) and delivered
to browsers over SSE. Fire-and-forget by contract: if the recipient has no stream
open the event is dropped, which is correct — every event is a hint, and the
client refetches authoritative state through the API gateway.

Usage, always AFTER the owning transaction commits::

    from core.realtime import CommunityAudience, Tier, emit

    await session.commit()
    await emit(
        topic="generation.finished",
        audience=CommunityAudience(community_id=cid, tier=Tier.MANAGER),
        resource=("generation", generation_id),
        scope_community_id=cid,
        hint={"status": "success"},
    )
"""

from .bus import close, emit, log_realtime_state
from .channels import (
    Audience,
    CommunityAudience,
    Tier,
    UserAudience,
    UsersAudience,
    community_channel,
    user_channel,
)
from .envelope import MAX_ENVELOPE_BYTES, TOPICS, build_envelope

__all__ = [
    "MAX_ENVELOPE_BYTES",
    "TOPICS",
    "Audience",
    "CommunityAudience",
    "Tier",
    "UserAudience",
    "UsersAudience",
    "build_envelope",
    "close",
    "community_channel",
    "emit",
    "log_realtime_state",
    "user_channel",
]
