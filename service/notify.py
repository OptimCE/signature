"""Who hears what: in-app + email notifications, and the realtime hints.

Notifications go into the CRM through ``core/notifications`` (the bell, and an
``outbound_message`` that notification-dispatch sends). Ordering, as in the
other annexes: the CRM transaction commits FIRST, then the local one; a local
commit lost after it re-runs the job and publishes the same ``data`` again,
which the email dedupe absorbs (``data`` is the email idempotency key).

``data`` holds the i18n parameters the front and the email templates use:
``title``, ``community``, ``path`` (the in-app link), plus ``signer`` and
``reason`` where they matter.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from core.notifications import (
    MANAGER_ROLES,
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationService,
    NotificationTarget,
    NotificationTypes,
    UsersTarget,
)
from core.realtime import CommunityAudience, Tier, UserAudience, UsersAudience, emit
from ports.crm_read import CrmRead
from shared.const import Capacity
from shared.models.local_models import SignatureRequestModel, SignatureSignerModel

logger = logging.getLogger(__name__)

TOPIC_SIGNED = "signature.signed"
TOPIC_REQUEST_UPDATED = "signature.request.updated"


def sign_path(request: SignatureRequestModel, signer: SignatureSignerModel) -> str:
    return f"/signatures/{request.id}/signers/{signer.id}"


def request_path(request: SignatureRequestModel) -> str:
    return f"/signatures/requests/{request.id}"


class Notifier:
    """Stages notifications in a CRM session; ``commit`` writes them and sends the hints."""

    def __init__(self, crm_session: AsyncSession) -> None:
        self._crm = crm_session
        self._read = CrmRead(crm_session)
        self._service = NotificationService(crm_session)
        self._hints: list[tuple[str, object, tuple[str, str], str]] = []

    async def recipients_for(
        self, request: SignatureRequestModel, signer: SignatureSignerModel
    ) -> list[int]:
        """The accounts that may sign this slot right now."""
        if signer.capacity == Capacity.COMMUNITY_ADMIN:
            user = await self._read.user_by_sub(str(signer.expected_user_sub))
            return [user.id] if user else []
        if signer.id_member is None:
            return []
        return [user.id for user in await self._read.linked_users(signer.id_member)]

    def _requester(self, request: SignatureRequestModel) -> NotificationTarget:
        if request.requested_by_user_id is not None:
            return UsersTarget([request.requested_by_user_id], community_id=request.id_community)
        return CommunityTarget(request.id_community, roles=MANAGER_ROLES)

    async def _base(self, request: SignatureRequestModel) -> dict[str, str]:
        community = await self._read.community_name(request.id_community)
        return {
            "request_id": str(request.id),
            "title": request.title,
            "community": community or "",
        }

    async def invited(
        self,
        request: SignatureRequestModel,
        signers: Sequence[SignatureSignerModel],
        *,
        reminder_on: datetime.date | None = None,
    ) -> None:
        base = await self._base(request)
        for signer in signers:
            users = await self.recipients_for(request, signer)
            if not users:
                continue
            data = {**base, "signer_id": str(signer.id), "path": sign_path(request, signer)}
            await self._service.publish(
                type=(
                    NotificationTypes.SIGNATURE_REMINDER
                    if reminder_on
                    else NotificationTypes.SIGNATURE_REQUESTED
                ),
                target=UsersTarget(users, community_id=request.id_community),
                category=NotificationCategory.TRANSACTIONAL,
                channels=[Channel.INAPP, Channel.EMAIL],
                data=data,
                # A reminder is a new occurrence of the same data: keyed by day.
                dedupe_key=(
                    f"signature.reminder:{signer.id}:{reminder_on.isoformat()}"
                    if reminder_on
                    else None
                ),
            )
            self._hints.append(
                (
                    TOPIC_REQUEST_UPDATED,
                    UsersAudience(users),
                    ("signature_request", str(request.id)),
                    "ready",
                )
            )

    async def signed(self, request: SignatureRequestModel, signer: SignatureSignerModel) -> None:
        base = await self._base(request)
        await self._service.publish(
            type=NotificationTypes.SIGNATURE_SIGNED,
            target=self._requester(request),
            category=NotificationCategory.INFORMATIONAL,
            channels=[Channel.INAPP],
            data={**base, "signer": signer.display_name, "path": request_path(request)},
        )
        if signer.signed_by_user_id is not None:
            self._hints.append(
                (
                    TOPIC_SIGNED,
                    UserAudience(signer.signed_by_user_id),
                    ("signature_signer", str(signer.id)),
                    "success",
                )
            )
        self._manager_hint(request, "success")

    async def declined(self, request: SignatureRequestModel, signer: SignatureSignerModel) -> None:
        base = await self._base(request)
        await self._service.publish(
            type=NotificationTypes.SIGNATURE_DECLINED,
            target=self._requester(request),
            category=NotificationCategory.TRANSACTIONAL,
            channels=[Channel.INAPP, Channel.EMAIL],
            data={
                **base,
                "signer": signer.display_name,
                "reason": signer.decline_reason or "",
                "path": request_path(request),
            },
        )
        self._manager_hint(request, "failed")

    async def completed(
        self, request: SignatureRequestModel, signers: Sequence[SignatureSignerModel]
    ) -> None:
        base = await self._base(request)
        data = {**base, "path": request_path(request)}
        await self._service.publish(
            type=NotificationTypes.SIGNATURE_COMPLETED,
            target=self._requester(request),
            category=NotificationCategory.INFORMATIONAL,
            channels=[Channel.INAPP, Channel.EMAIL],
            data=data,
        )
        signer_users = sorted(
            {s.signed_by_user_id for s in signers if s.signed_by_user_id is not None}
        )
        if signer_users:
            await self._service.publish(
                type=NotificationTypes.SIGNATURE_COMPLETED,
                target=UsersTarget(signer_users, community_id=request.id_community),
                category=NotificationCategory.INFORMATIONAL,
                channels=[Channel.INAPP, Channel.EMAIL],
                data={**data, "path": "/signatures"},
            )
            self._hints.append(
                (
                    TOPIC_REQUEST_UPDATED,
                    UsersAudience(signer_users),
                    ("signature_request", str(request.id)),
                    "ready",
                )
            )
        self._manager_hint(request, "ready")

    def changed(self, request: SignatureRequestModel, status: str) -> None:
        """A hint only (cancelled, expired, failed): lists refresh, nobody is notified."""
        self._manager_hint(request, status)

    def _manager_hint(self, request: SignatureRequestModel, status: str) -> None:
        self._hints.append(
            (
                TOPIC_REQUEST_UPDATED,
                CommunityAudience(request.id_community, Tier.MANAGER),
                ("signature_request", str(request.id)),
                status,
            )
        )

    async def commit(self) -> None:
        await self._crm.commit()
        await self._service.flush_realtime()

    async def send_hints(self) -> None:
        """After the LOCAL commit too: a hint names state the reader can now see."""
        hints, self._hints = self._hints, []
        for topic, audience, resource, status in hints:
            await emit(
                topic=topic,
                audience=audience,  # type: ignore[arg-type]
                resource=resource,
                hint={"status": status},
            )
