"""What the documents and the evidence say, in the request's language.

The consent statement is versioned: ``consent/<version>/<locale>.json`` holds
one sentence per capacity, and its SHA-256 (of the file's bytes) is stored on
every signed event with the version, so the evidence names the exact words
the signer ticked. A new wording is a new version folder, never an edit.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from shared.const import DEFAULT_LOCALE, LOCALES, Capacity

CONSENT_DIR = Path(__file__).resolve().parent.parent / "consent"
BRUSSELS = ZoneInfo("Europe/Brussels")


@dataclass(frozen=True)
class Consent:
    version: str
    locale: str
    text: str
    sha256: str


@cache
def _consent_file(version: str, locale: str) -> tuple[dict[str, str], str]:
    path = CONSENT_DIR / version / f"{locale}.json"
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def consent(
    version: str,
    locale: str,
    capacity: Capacity,
    *,
    member: str | None,
    community: str | None,
) -> Consent:
    texts, digest = _consent_file(version, locale if locale in LOCALES else DEFAULT_LOCALE)
    text = texts[capacity.name].format(member=member or "", community=community or "")
    return Consent(version, locale, text, digest)


_REASON = {
    "fr": {
        Capacity.SELF: "Signé par {name} en son nom propre",
        Capacity.LEGAL_REPRESENTATIVE: (
            "Signé par {name} en qualité de représentant légal de {member}"
        ),
        Capacity.COMMUNITY_ADMIN: "Signé par {name} pour la communauté {community}",
    },
    "en": {
        Capacity.SELF: "Signed by {name} on their own behalf",
        Capacity.LEGAL_REPRESENTATIVE: "Signed by {name} as legal representative of {member}",
        Capacity.COMMUNITY_ADMIN: "Signed by {name} for the community {community}",
    },
    "nl": {
        Capacity.SELF: "Ondertekend door {name} in eigen naam",
        Capacity.LEGAL_REPRESENTATIVE: (
            "Ondertekend door {name} als wettelijk vertegenwoordiger van {member}"
        ),
        Capacity.COMMUNITY_ADMIN: "Ondertekend door {name} voor de gemeenschap {community}",
    },
    "de": {
        Capacity.SELF: "Unterzeichnet von {name} im eigenen Namen",
        Capacity.LEGAL_REPRESENTATIVE: (
            "Unterzeichnet von {name} als gesetzlicher Vertreter von {member}"
        ),
        Capacity.COMMUNITY_ADMIN: "Unterzeichnet von {name} für die Gemeinschaft {community}",
    },
}

# A member's own slot signed by another account linked to the member.
_ON_BEHALF = {
    "fr": "Signé par {name} pour {member}",
    "en": "Signed by {name} for {member}",
    "nl": "Ondertekend door {name} voor {member}",
    "de": "Unterzeichnet von {name} für {member}",
}

_STAMP_FOR = {
    "fr": "pour {member}",
    "en": "for {member}",
    "nl": "voor {member}",
    "de": "für {member}",
}

_STAMP = {
    "fr": ("Signé électroniquement par", "le {date} à {time}", "réf. {ref}"),
    "en": ("Signed electronically by", "on {date} at {time}", "ref. {ref}"),
    "nl": ("Elektronisch ondertekend door", "op {date} om {time}", "ref. {ref}"),
    "de": ("Elektronisch unterzeichnet von", "am {date} um {time}", "Ref. {ref}"),
}

_CERTIFICATION = {
    "fr": "Document émis par OptimCE",
    "en": "Document issued by OptimCE",
    "nl": "Document uitgegeven door OptimCE",
    "de": "Von OptimCE ausgestelltes Dokument",
}

_EVIDENCE = {
    "fr": "Rapport de preuve OptimCE",
    "en": "OptimCE evidence report",
    "nl": "OptimCE bewijsrapport",
    "de": "OptimCE-Nachweisbericht",
}


def _loc(locale: str) -> str:
    return locale if locale in LOCALES else DEFAULT_LOCALE


def _name_words(name: str) -> list[str]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return sorted(word for word in re.split(r"[\s\-]+", plain.casefold()) if word)


def same_person(
    user_name: str, user_email: str | None, member_name: str, member_email: str | None
) -> bool:
    """Whether the account signing a member's slot is that member: the same
    e-mail, or the same words in the name (accents, case and order aside)."""
    emails = {(e or "").strip().casefold() for e in (user_email, member_email)}
    if len(emails) == 1 and "" not in emails:
        return True
    words = _name_words(user_name)
    return bool(words) and words == _name_words(member_name)


def signed_by_name(
    capacity: Capacity,
    *,
    user_name: str,
    user_email: str | None,
    slot_name: str,
    slot_email: str | None,
) -> str:
    """The name the PDF and the evidence give whoever signs, frozen at the sign call.

    A member signing its own slot keeps the member's name, the one the
    document was drawn up for; any other account is named as itself: the
    representative of a company, another account linked to the member, an admin.
    """
    if capacity is Capacity.SELF and same_person(user_name, user_email, slot_name, slot_email):
        return slot_name
    return user_name


def signed_for(
    capacity: Capacity, *, person: str, slot_name: str, member_label: str | None
) -> str | None:
    """The member the person signs for, when it is not themselves."""
    if capacity is Capacity.LEGAL_REPRESENTATIVE:
        return member_label or slot_name
    if capacity is Capacity.SELF and person != slot_name:
        return slot_name
    return None


def reason(
    locale: str, capacity: Capacity, *, name: str, member: str | None, community: str | None
) -> str:
    """``name`` is the person who signs; ``member`` what ``signed_for`` returns.

    A SELF slot with a ``member`` was signed by another account linked to the
    member, and says so (« pour … ») rather than « en son nom propre ».
    """
    if capacity is Capacity.SELF and member:
        return _ON_BEHALF[_loc(locale)].format(name=name, member=member)
    return _REASON[_loc(locale)][capacity].format(
        name=name, member=member or "", community=community or ""
    )


def stamp_lines(
    locale: str,
    *,
    name: str,
    at: datetime.datetime,
    ref: str,
    for_member: str | None = None,
) -> list[str]:
    local = at.astimezone(BRUSSELS)
    first, second, third = _STAMP[_loc(locale)]
    lines = [f"{first} {name}"]
    if for_member:
        lines.append(_STAMP_FOR[_loc(locale)].format(member=for_member))
    return [
        *lines,
        second.format(date=local.strftime("%d/%m/%Y"), time=local.strftime("%H:%M")),
        third.format(ref=ref),
    ]


def signing_date(locale: str, at: datetime.datetime) -> str:
    """What a DATE field shows: the signing day, Brussels time."""
    return at.astimezone(BRUSSELS).strftime("%d/%m/%Y")


def certification_reason(locale: str) -> str:
    return _CERTIFICATION[_loc(locale)]


def evidence_reason(locale: str) -> str:
    return _EVIDENCE[_loc(locale)]


def short_ref(request_id: object) -> str:
    """The short request reference printed on the stamps: 8 hex characters."""
    return str(request_id).replace("-", "")[:8].upper()
