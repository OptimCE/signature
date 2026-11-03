"""Read-only access to the CRM core (members, accounts, roles, the module).

Raw SELECTs on the CRM database, which crm-backend owns; this service never
writes there except notifications (``core/notifications``). Every id is the
CRM's INT; a user is also known by their Keycloak ``sub``
(``app_user.auth_user_id``), which is what the gateway forwards.

A member may be linked to several accounts and an account to several members
(``user_member_link``, no capacity): any account linked to the member when
they sign may sign that member's slot (decided 03/11/2026).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.const import FeatureName

MEMBER_TYPE_INDIVIDUAL = 1
MEMBER_TYPE_COMPANY = 2


@dataclass(frozen=True)
class MemberInfo:
    id: int
    id_community: int
    member_type: int
    display_name: str
    email: str | None


@dataclass(frozen=True)
class UserInfo:
    id: int
    sub: str
    email: str
    display_name: str
    locale: str | None


def _user_name(first: str | None, last: str | None, email: str) -> str:
    name = " ".join(part for part in (first, last) if part and part.strip())
    return name or email


class CrmRead:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def module_active(self, id_community: int) -> bool:
        row = await self._session.execute(
            text(
                "SELECT is_active FROM community_subscription "
                "WHERE id_community = :c AND feature = :f"
            ),
            {"c": id_community, "f": FeatureName.SIGNATURE.value},
        )
        return bool(row.scalar_one_or_none())

    async def communities_with_module(self, ids: Iterable[int]) -> set[int]:
        wanted = sorted(set(ids))
        if not wanted:
            return set()
        rows = await self._session.execute(
            text(
                "SELECT id_community FROM community_subscription "
                "WHERE feature = :f AND is_active AND id_community = ANY(:ids)"
            ),
            {"f": FeatureName.SIGNATURE.value, "ids": wanted},
        )
        return {int(r[0]) for r in rows}

    async def community_name(self, id_community: int) -> str | None:
        row = await self._session.execute(
            text("SELECT name FROM community WHERE id = :c"), {"c": id_community}
        )
        return row.scalar_one_or_none()

    async def internal_community_id(self, auth_community_id: str) -> int | None:
        row = await self._session.execute(
            text("SELECT id FROM community WHERE auth_community_id = :a"),
            {"a": auth_community_id},
        )
        value = row.scalar_one_or_none()
        return int(value) if value is not None else None

    async def member(self, id_member: int) -> MemberInfo | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT m.id, m.id_community, m.member_type, m.name, i.first_name, i.email "
                    "FROM member m LEFT JOIN individual i ON i.id = m.id "
                    "WHERE m.id = :m"
                ),
                {"m": id_member},
            )
        ).first()
        if row is None:
            return None
        member_id, community, member_type, name, first_name, email = row
        if member_type == MEMBER_TYPE_INDIVIDUAL and first_name:
            display = f"{first_name} {name}".strip()
        else:
            display = str(name)
        return MemberInfo(int(member_id), int(community), int(member_type), display, email)

    async def linked_users(self, id_member: int) -> list[UserInfo]:
        rows = await self._session.execute(
            text(
                "SELECT DISTINCT u.id, u.auth_user_id, u.email, u.first_name, u.last_name, "
                "u.locale FROM user_member_link l JOIN app_user u ON u.id = l.id_user "
                "WHERE l.id_member = :m ORDER BY u.id"
            ),
            {"m": id_member},
        )
        return [
            UserInfo(int(r[0]), str(r[1]), str(r[2]), _user_name(r[3], r[4], str(r[2])), r[5])
            for r in rows
        ]

    async def user_by_sub(self, sub: str) -> UserInfo | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, auth_user_id, email, first_name, last_name, locale "
                    "FROM app_user WHERE auth_user_id = :s"
                ),
                {"s": sub},
            )
        ).first()
        if row is None:
            return None
        return UserInfo(
            int(row[0]), str(row[1]), str(row[2]), _user_name(row[3], row[4], str(row[2])), row[5]
        )

    async def role_in_community(self, user_id: int, id_community: int) -> str | None:
        row = await self._session.execute(
            text("SELECT role FROM community_user WHERE id_user = :u AND id_community = :c"),
            {"u": user_id, "c": id_community},
        )
        value = row.scalar_one_or_none()
        return str(value) if value is not None else None

    async def is_linked(self, user_id: int, id_member: int) -> bool:
        row = await self._session.execute(
            text("SELECT 1 FROM user_member_link WHERE id_user = :u AND id_member = :m LIMIT 1"),
            {"u": user_id, "m": id_member},
        )
        return row.scalar_one_or_none() is not None

    async def member_ids_for_user(self, user_id: int) -> list[int]:
        rows = await self._session.execute(
            text("SELECT DISTINCT id_member FROM user_member_link WHERE id_user = :u"),
            {"u": user_id},
        )
        return [int(r[0]) for r in rows]
