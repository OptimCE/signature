"""Publish durable notifications into the shared CRM ``notification`` table.

Mirrors ``AuditLogService``: the write rides on the caller's CRM session inside a
SAVEPOINT and never raises — a notification failure must not abort the business
write that triggered it. The caller owns the commit.

This file is byte-identical across news-board, billing and
administrative-document. The only per-service module in this package is
``types.py``. Keep it that way: it is what makes Phase 2's extraction a move
rather than a rewrite.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.notifications.contract import (
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationTarget,
    UsersTarget,
    UserTarget,
)
from core.notifications.dedupe import build_dedupe_key, type_prefix_of
from core.notifications.repository import NotificationRepository
from core.realtime import UsersAudience, emit
from shared.models.crm_models import NotificationModel

logger = logging.getLogger(__name__)

# `notification_preference.mode`: 1 IMMEDIATE, 3 OFF. Only OFF changes anything,
# so it is the only value this module needs to know. 2 DAILY_DIGEST is reserved
# in the encoding and rejected by a DB CHECK until a digest runner exists.
_PREFERENCE_MODE_OFF = 3


@dataclass(frozen=True, slots=True)
class EmailRecipient:
    """One resolved addressee of the EMAIL channel.

    ``id_notification`` is ``None`` when INAPP was not among the effective
    channels — exactly the case step 3's ``outbound_message.id_notification
    BIGINT NULL`` exists for.
    """

    id_user: int
    id_notification: int | None


class NotificationService:
    """The producer-facing notification API (IMPLEMENTATION_PLAN.md §1.3)."""

    def __init__(self, crm_session: AsyncSession):
        self.crm_session = crm_session
        self.repository = NotificationRepository(crm_session)
        # Realtime hints staged by publish() and released by flush_realtime()
        # AFTER the caller commits. See flush_realtime's docstring for why this
        # cannot simply be emitted inline.
        self._pending_realtime: list[tuple[list[int], int | None]] = []

    async def publish(
        self,
        *,
        type: str,
        target: NotificationTarget,
        category: NotificationCategory,
        channels: Sequence[Channel],
        data: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        """Fan a notification out to ``target``. Returns the in-app rows staged.

        ``category`` and ``channels`` are both required and both meaningful:
        ``channels`` is what the producer *asks* for, ``category`` is what the
        producer *is*, and effective delivery is the intersection with the
        recipient's preferences — except for TRANSACTIONAL, which overrides
        them. Neither is defaulted: a defaulted ``channels`` fails silently (a
        producer that meant EMAIL quietly gets INAPP only), while a missing one
        fails loudly at the call site.

        ``data`` must hold JSON primitives only — it lands in a JSONB column, so
        ``Decimal`` and ``date`` have to be stringified by the caller. It is also
        the email idempotency key: see ``dedupe.build_dedupe_key``.

        ``dedupe_key`` overrides that derivation. Set it ONLY when a recurring
        sweep can re-emit the same payload for a genuinely new occurrence, and
        include the occurrence date.

        Returns 0 on an empty audience, on any swallowed failure, when INAPP is
        not among the effective channels (an email-only publish), and when every
        recipient has muted the in-app channel. The count is about in-app rows,
        not about delivery.
        """
        try:
            # `no_autoflush` because these reads happen OUTSIDE the savepoint
            # below. Without it, SQLAlchemy would flush whatever the caller
            # staged earlier (in billing's issue_invoice, the audit row) as a
            # side effect of our SELECT — and a failure there would abort the
            # caller's CRM transaction outside the savepoint's protection, get
            # swallowed by the blanket `except`, and resurface opaquely at their
            # commit. That is exactly the class of bug the savepoint exists to
            # prevent. The notification layer must never flush a caller's work.
            with self.crm_session.no_autoflush:
                recipient_ids, community_id = await self._resolve_audience(target)
                if not recipient_ids:
                    return 0

                effective = await self._effective_channels(
                    type=type,
                    category=category,
                    requested=channels,
                    recipient_ids=recipient_ids,
                )

            none: frozenset[Channel] = frozenset()
            inapp_ids = [uid for uid in recipient_ids if Channel.INAPP in effective.get(uid, none)]
            email_ids = [uid for uid in recipient_ids if Channel.EMAIL in effective.get(uid, none)]
            if not inapp_ids and not email_ids:
                return 0

            payload = data or {}
            rows = [
                NotificationModel(
                    id_community=community_id,
                    id_user=user_id,
                    type=type,
                    data=payload,
                )
                for user_id in inapp_ids
            ]

            # ONE savepoint around every write this publish performs. The in-app
            # rows and the outbound_message rows must land or vanish together,
            # and a failure must leave the caller's CRM transaction clean and
            # committable. insert_many flushes, so the notification ids exist by
            # the time _enqueue_email needs them.
            async with self.crm_session.begin_nested():
                await self.repository.insert_many(rows)
                if email_ids:
                    notification_ids = {row.id_user: row.id for row in rows}
                    await self._enqueue_email(
                        type=type,
                        data=payload,
                        category=category,
                        id_community=community_id,
                        dedupe_key=dedupe_key,
                        recipients=[
                            EmailRecipient(
                                id_user=user_id,
                                id_notification=notification_ids.get(user_id),
                            )
                            for user_id in email_ids
                        ],
                    )
            # Stage the realtime hint. A list append cannot abort a Postgres
            # transaction, and this sits outside the savepoint above, so a
            # savepoint rollback re-raises into the `except` below and nothing is
            # staged — no event for rows that never landed.
            #
            # The audience is the SAME inapp_ids resolution performed above, never
            # a second one: a divergence there means a notification row with no
            # event, or an event with no row.
            if inapp_ids:
                self._pending_realtime.append((list(inapp_ids), community_id))
            return len(rows)
        except Exception:
            logger.exception(
                "notification.publish failed",
                extra={"operation": "notification:publish", "type": type},
            )
            return 0

    async def flush_realtime(self) -> None:
        """Release the realtime hints staged by publish(). NEVER raises.

        *** CALL THIS AFTER YOUR COMMIT. ***

        publish() deliberately runs INSIDE the caller's transaction because it
        writes rows. A realtime hint has the exact opposite requirement: emitted
        before the commit, it tells the browser to refetch and read PRE-COMMIT
        state, and because the transport is fire-and-forget there is no second
        event — a permanently stale UI behind a 200. So publish() is split in two
        and the caller, which owns the commit, owns the boundary between them.

        Not wired to SQLAlchemy's ``after_commit`` event on purpose: that fires
        synchronously inside the greenlet, so an async publish there needs
        ``create_task``, and in a worker that exits immediately the task can be
        garbage-collected before it ever runs.

        Safe to call when nothing is staged, and safe to call twice.
        """
        staged, self._pending_realtime = self._pending_realtime, []
        for user_ids, community_id in staged:
            await emit(
                topic="notification.created",
                audience=UsersAudience(user_ids=user_ids),
                # The client refetches /unread-count and the recent slice, so it
                # needs neither a row id nor a count — and the envelope must
                # carry no business data regardless.
                resource=("notification", "0"),
                scope_community_id=community_id,
                hint={},
            )

    async def _resolve_audience(self, target: NotificationTarget) -> tuple[list[int], int | None]:
        """Turn a target into (de-duplicated recipient ids, source community)."""
        match target:
            case UserTarget():
                return [target.user_id], target.community_id
            case UsersTarget():
                # dict.fromkeys de-duplicates while preserving caller order,
                # mirroring TypeScript's [...new Set(userIds)].
                return list(dict.fromkeys(target.user_ids)), target.community_id
            case CommunityTarget():
                exclude_user_id = (
                    await self.repository.resolve_internal_user_id(target.exclude_auth_user_id)
                    if target.exclude_auth_user_id
                    else None
                )
                recipients = await self.repository.find_community_recipient_ids(
                    target.community_id,
                    exclude_user_id=exclude_user_id,
                    roles=target.roles,
                )
                return recipients, target.community_id
            case _:  # pragma: no cover — the union is closed
                raise TypeError(f"unsupported notification target: {target!r}")

    async def _effective_channels(
        self,
        *,
        type: str,
        category: NotificationCategory,
        requested: Sequence[Channel],
        recipient_ids: Sequence[int],
    ) -> dict[int, frozenset[Channel]]:
        """Requested channels ∩ each recipient's preference; TRANSACTIONAL overrides.

        Per-recipient, not per-publish: ``notification_preference`` is keyed by
        user and a community fan-out reaches many of them, so a single answer for
        everyone would let one manager who muted a reminder mute it for the whole
        community.

        ``category is TRANSACTIONAL`` skips the lookup entirely — an invoice or a
        missed regulatory deadline is not opt-out-able, so there is nothing to
        read and no query to pay for.
        """
        default = frozenset(requested)
        effective = {user_id: default for user_id in recipient_ids}
        if category is NotificationCategory.TRANSACTIONAL:
            return effective

        modes = await self.repository.find_preferences(recipient_ids, type_prefix_of(type))
        for (user_id, channel), mode in modes.items():
            if mode != _PREFERENCE_MODE_OFF or user_id not in effective:
                continue
            effective[user_id] = effective[user_id] - {Channel(channel)}
        return effective

    async def _enqueue_email(
        self,
        *,
        type: str,
        data: dict[str, Any],
        category: NotificationCategory,
        id_community: int | None,
        recipients: Sequence[EmailRecipient],
        dedupe_key: str | None = None,
    ) -> None:
        """Stage one ``outbound_message`` row per addressee.

        Called on the real path from inside ``publish``'s SAVEPOINT with the
        notification ids already flushed, so the queued mail shares the
        producer's transaction: the business write committing is what makes the
        message queued, and rolling back un-queues it.

        The recipient's address, display name and locale are resolved HERE and
        copied onto the row, so a later profile change never redirects an
        already-queued message. A recipient with no ``app_user`` row is skipped —
        not an error, their in-app notification stands on its own.

        There is deliberately no ``email_suppression`` check at enqueue: a bounce
        can land after a message is queued, so only the dispatcher's check before
        each send can be authoritative, and doing it twice would mean two places
        that must agree on address normalisation.
        """
        contacts = await self.repository.find_recipient_contacts(
            [recipient.id_user for recipient in recipients]
        )
        rows: list[dict[str, object]] = []
        for recipient in recipients:
            contact = contacts.get(recipient.id_user)
            if contact is None:
                continue
            address = contact.email.strip()
            # A newline in an address splits the SMTP header block, so a
            # producer-controlled value could inject headers or extra
            # recipients. The queue must never contain one.
            if not address or "\r" in address or "\n" in address:
                logger.warning(
                    "notification: rejected an unusable outbound recipient address",
                    extra={"operation": "notification:enqueue_email", "type": type},
                )
                continue
            rows.append(
                {
                    "id_notification": recipient.id_notification,
                    "id_community": id_community,
                    "channel": int(Channel.EMAIL),
                    "recipient": address,
                    "recipient_name": (contact.display_name or None),
                    # '' means "unknown": the dispatcher owns the fallback chain,
                    # because it is the only component that knows which locales
                    # it actually has templates for.
                    "locale": contact.locale or "",
                    "type": type,
                    "category": int(category),
                    "data": data,
                    "dedupe_key": build_dedupe_key(
                        channel=int(Channel.EMAIL),
                        type=type,
                        data=data,
                        id_user=recipient.id_user,
                        override=dedupe_key,
                    ),
                }
            )
        await self.repository.insert_outbound(rows)
