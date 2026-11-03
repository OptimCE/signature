"""A small CRM world for the tests, and the gateway headers of its people.

- Alice: an individual member, her own account linked to her member;
- Bob: the account linked to ACME SRL (a company member, so he signs as its
  legal representative);
- Carol: an ADMIN of the community; Mike: a MANAGER; Eve: a MEMBER with no link;
- an orphan member with no account at all.
"""

from __future__ import annotations

import datetime
import io
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class User:
    id: int
    sub: str
    email: str


@dataclass(frozen=True)
class World:
    community: int
    community_auth: str
    alice: User
    alice_member: int
    bob: User
    acme_member: int
    carol: User
    mike: User
    eve: User
    orphan_member: int


async def _one(session: AsyncSession, sql: str, **params: Any) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def _user(session: AsyncSession, first: str, last: str) -> User:
    sub = str(uuid.uuid4())
    email = f"{first.lower()}.{uuid.uuid4().hex[:6]}@example.test"
    user_id = await _one(
        session,
        "INSERT INTO app_user (auth_user_id, email, first_name, last_name, locale) "
        "VALUES (:s, :e, :f, :l, 'fr') RETURNING id",
        s=sub,
        e=email,
        f=first,
        l=last,
    )
    return User(user_id, sub, email)


async def _member(
    session: AsyncSession, community: int, name: str, kind: int, first: str | None = None
) -> int:
    member = await _one(
        session,
        "INSERT INTO member (name, member_type, status, id_community) "
        "VALUES (:n, :k, 1, :c) RETURNING id",
        n=name,
        k=kind,
        c=community,
    )
    if kind == 1:
        await session.execute(
            text("INSERT INTO individual (id, first_name, email) VALUES (:m, :f, :e)"),
            {"m": member, "f": first, "e": f"{(first or name).lower()}@example.test"},
        )
    else:
        await session.execute(
            text("INSERT INTO company (id, vat_number) VALUES (:m, 'BE0123456789')"), {"m": member}
        )
    return member


async def make_world(session: AsyncSession, *, module: bool = True) -> World:
    auth = str(uuid.uuid4())
    community = await _one(
        session,
        "INSERT INTO community (name, auth_community_id) VALUES (:n, :a) RETURNING id",
        n=f"Les Coteaux {auth[:6]}",
        a=auth,
    )
    await session.execute(
        text(
            "INSERT INTO community_subscription (id_community, feature, is_active) "
            "VALUES (:c, 'signature', :a)"
        ),
        {"c": community, "a": module},
    )
    alice = await _user(session, "Alice", "Martin")
    bob = await _user(session, "Bob", "Dupont")
    carol = await _user(session, "Carol", "Admin")
    mike = await _user(session, "Mike", "Manager")
    eve = await _user(session, "Eve", "Outsider")
    alice_member = await _member(session, community, "Martin", 1, "Alice")
    acme = await _member(session, community, "ACME SRL", 2)
    orphan = await _member(session, community, "Orphan", 1, "Oscar")
    for user, member in ((alice, alice_member), (bob, acme)):
        await session.execute(
            text("INSERT INTO user_member_link (id_user, id_member) VALUES (:u, :m)"),
            {"u": user.id, "m": member},
        )
    for user, role in (
        (alice, "MEMBER"),
        (bob, "MEMBER"),
        (carol, "ADMIN"),
        (mike, "MANAGER"),
        (eve, "MEMBER"),
    ):
        await session.execute(
            text("INSERT INTO community_user (id_community, id_user, role) VALUES (:c, :u, :r)"),
            {"c": community, "u": user.id, "r": role},
        )
    await session.flush()
    return World(community, auth, alice, alice_member, bob, acme, carol, mike, eve, orphan)


def headers(
    user: User,
    *,
    auth_age_seconds: int = 30,
    community_auth: str | None = None,
    role: str | None = None,
) -> dict[str, str]:
    """What KrakenD + nginx forward for this user (a login ``auth_age_seconds`` ago)."""
    auth_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=auth_age_seconds)
    out = {
        "x-user-id": user.sub,
        "x-auth-time": str(int(auth_time.timestamp())),
        "x-auth-sid": f"sid-{user.id}",
        "x-auth-acr": "1",
        "x-source-ip": "203.0.113.7",
        "user-agent": "pytest-browser/1.0",
    }
    if community_auth:
        out["x-community-id"] = community_auth
        out["x-user-orgs"] = (
            f"[orgId:{community_auth} orgPath:/Les Coteaux roles:[{role or 'MEMBER'}]]"
        )
    return out


def png_bytes(width: int = 300, height: int = 80) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    ImageDraw.Draw(image).line((10, height - 10, width - 10, 10), fill=(10, 20, 90, 255), width=4)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def anchored_pdf(tags: list[tuple[str, float, float]], *, pages: int = 1) -> bytes:
    """A plain PDF printing each ``(tag, x, y)`` on page 1, as a docgen template would."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=595.276, height=841.89)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        lines = ["BT /F1 14 Tf 56 780 Td (Convention de partage) Tj ET"]
        if index == 0:
            for tag, x, y in tags:
                lines.append(f"BT /F1 2 Tf {x} {y} Td ({tag}) Tj ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(lines).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
