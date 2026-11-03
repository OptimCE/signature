from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.crm_models import AppUserModel, CommunityUserModel, NotificationModel
from shared.models.crm_notification_models import (
    NotificationPreferenceModel,
    OutboundMessageModel,
)


@dataclass(frozen=True, slots=True)
class RecipientContact:
    """Everything the delivery layer needs to address one recipient.

    Read once at enqueue and copied onto the queued row, so a later profile
    change never redirects or relabels an already-queued message.
    """

    id_user: int
    email: str
    locale: str | None
    display_name: str | None


class NotificationRepository:
    """Read community membership / write notifications against the CRM database.

    All the tables here (``app_user``, ``community_user``, ``notification``,
    ``outbound_message``, ``notification_preference``) are owned by
    ``crm-backend``; an annexe reads the membership and preference sides and
    inserts into the notification and outbound sides when one of its domain
    events should reach a user.

    Byte-identical across producers — see ``service.py``'s module docstring.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_internal_user_id(self, auth_user_id: str) -> int | None:
        """Map a Keycloak ``sub`` to its internal ``app_user.id`` (``None`` if absent)."""
        stmt = select(AppUserModel.id).where(AppUserModel.auth_user_id == auth_user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_community_recipient_ids(
        self,
        community_id: int,
        *,
        exclude_user_id: int | None = None,
        roles: Sequence[str] | None = None,
    ) -> list[int]:
        """Return the internal ids of a community's members.

        ``exclude_user_id`` drops one member (typically the author) from the
        fan-out; ``roles`` optionally narrows to specific roles (unused today,
        kept to mirror crm-backend's ``findCommunityRecipientIds``).
        """
        stmt = select(CommunityUserModel.id_user).where(
            CommunityUserModel.id_community == community_id
        )
        if exclude_user_id is not None:
            stmt = stmt.where(CommunityUserModel.id_user != exclude_user_id)
        if roles:
            stmt = stmt.where(CommunityUserModel.role.in_(roles))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def insert_many(self, rows: list[NotificationModel]) -> None:
        """Stage a batch of notification rows. The caller owns the commit.

        Flushes, so the generated ids exist by the time ``_enqueue_email`` needs
        them to link each queued message back to its in-app row.
        """
        if not rows:
            return
        self.session.add_all(rows)
        await self.session.flush()

    async def find_preferences(
        self, user_ids: Sequence[int], type_prefix: str
    ) -> dict[tuple[int, int], int]:
        """Effective ``mode`` per (user, channel) for one type, most-specific-wins.

        A row whose ``type_prefix`` is the type's first dot-segment beats the
        ``''`` default row. Pairs absent from the result have expressed no
        preference and default to IMMEDIATE.

        One round trip for the whole audience, and only ever called for
        INFORMATIONAL notifications — TRANSACTIONAL bypasses preference entirely
        and must not reach this query.
        """
        if not user_ids:
            return {}
        stmt = select(
            NotificationPreferenceModel.id_user,
            NotificationPreferenceModel.channel,
            NotificationPreferenceModel.mode,
            NotificationPreferenceModel.type_prefix,
        ).where(
            NotificationPreferenceModel.id_user.in_(list(user_ids)),
            NotificationPreferenceModel.type_prefix.in_(["", type_prefix]),
        )
        result = await self.session.execute(stmt)
        resolved: dict[tuple[int, int], int] = {}
        for id_user, channel, mode, row_prefix in result.all():
            key = (id_user, channel)
            # A specific prefix always wins; between two rows of the same
            # specificity there can only be one, since (id_user, type_prefix,
            # channel) is the primary key.
            if row_prefix != "" or key not in resolved:
                resolved[key] = mode
        return resolved

    async def find_recipient_contacts(self, user_ids: Sequence[int]) -> dict[int, RecipientContact]:
        """Resolve email, locale and display name for a set of internal user ids.

        Users with no row are simply absent — the caller queues nothing for them,
        which is not an error: the in-app notification stands on its own.
        """
        if not user_ids:
            return {}
        stmt = select(
            AppUserModel.id,
            AppUserModel.email,
            AppUserModel.locale,
            AppUserModel.first_name,
            AppUserModel.last_name,
        ).where(AppUserModel.id.in_(list(user_ids)))
        result = await self.session.execute(stmt)
        contacts: dict[int, RecipientContact] = {}
        for id_user, email, locale, first_name, last_name in result.all():
            name = " ".join(part for part in (first_name, last_name) if part and part.strip())
            contacts[id_user] = RecipientContact(
                id_user=id_user,
                email=email,
                locale=locale,
                display_name=name or None,
            )
        return contacts

    async def insert_outbound(self, rows: list[dict[str, object]]) -> None:
        """Stage outbound rows, skipping any whose ``dedupe_key`` already exists.

        Targeted ``ON CONFLICT``: an untargeted ``DO NOTHING`` would also swallow
        a violation of any future unique index on this table.
        """
        if not rows:
            return
        stmt = pg_insert(OutboundMessageModel).values(rows)
        await self.session.execute(stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]))
