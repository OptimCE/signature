"""Mappings of the CRM core tables this service touches.

This service owns none of these tables — they live in the CRM database. It reads
``app_user`` and ``community_user``, and *inserts* into ``notification``
through ``core/notifications``; it never updates or deletes anything here.

Keep this module minimal: the member and link reads go through
``ports/crm_read.py``, raw SELECTs behind a Protocol. ``core/notifications``
needs these three mapped by name.
"""

import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, BigInteger, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.database import CrmBase


class AppUserModel(CrmBase):
    # Partial mapping of the CRM `app_user` table: the columns the audit log
    # service needs to denormalise the writer's identity onto each row, plus the
    # locale and name pair `core/notifications` reads when it addresses a
    # queued email.
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auth_user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    # Preferred language. NULL for every account created before the column
    # existed, which the dispatcher's locale fallback is what handles.
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommunityUserModel(CrmBase):
    """Partial mapping of the CRM ``community_user`` join table.

    The membership roster of a community (one row per user, with their role).
    Read to narrow a notification fan-out to a community's managers. Owned by
    ``crm-backend``; read-only here.
    """

    __tablename__ = "community_user"
    id_community: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_user: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False)


class NotificationModel(CrmBase):
    """Mapping of the shared CRM ``notification`` table.

    A durable, per-recipient notification row (one row per user). The table is
    owned by ``crm-backend`` — which serves the read API the frontend polls — so
    this service only ever *inserts* here, through ``core/notifications``;
    ``read_at``/``created_at`` and the bigint ``id`` are managed by the DB.
    """

    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_community: Mapped[int | None] = mapped_column(Integer, nullable=True)
    id_user: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    read_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
