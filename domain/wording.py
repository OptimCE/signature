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


def reason(
    locale: str, capacity: Capacity, *, name: str, member: str | None, community: str | None
) -> str:
    return _REASON[_loc(locale)][capacity].format(
        name=name, member=member or "", community=community or ""
    )


def stamp_lines(locale: str, *, name: str, at: datetime.datetime, ref: str) -> list[str]:
    local = at.astimezone(BRUSSELS)
    first, second, third = _STAMP[_loc(locale)]
    return [
        f"{first} {name}",
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
