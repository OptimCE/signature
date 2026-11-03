"""The evidence log's hash chain: pure, session-free.

Each ``signature_event`` row carries ``hash = sha256(prev_hash || canonical)``,
where ``canonical`` is the JSON of the row's hashed fields (below), keys
sorted, no whitespace, UTF-8, timestamps in UTC ISO 8601 with microseconds.
The first row chains from the original document's SHA-256, so the chain also
binds the request to the exact file it started from.

The database checks the chain's continuity on insert (seq and prev_hash,
``check_signature_event_chain``); ``verify_chain`` recomputes every hash, which
only Python can do faithfully: Postgres' jsonb reorders keys and reformats
numbers, so a SQL recomputation would not be byte-identical.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# The hashed fields, in no particular order (the JSON sorts them). Everything
# the row says except its database id and its own hash.
HASHED_FIELDS = (
    "request_id",
    "seq",
    "signer_id",
    "type",
    "at",
    "actor_sub",
    "actor_user_id",
    "ip",
    "user_agent",
    "keycloak_sid",
    "auth_time",
    "acr",
    "document_sha256",
    "consent_version",
    "payload",
    "prev_hash",
)


def _normalise(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    if isinstance(value, float):
        # Floats do not round-trip byte-identically through every reader; the
        # log never needs one, so refuse it rather than hash something fragile.
        raise ValueError("evidence payloads carry no floats; use strings or integers")
    return value


def canonical_json(value: Any) -> bytes:
    """The bytes the hash covers: sorted keys, compact separators, UTF-8."""
    return json.dumps(
        _normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def hashed_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a row the hash covers. A missing field counts as null."""
    return {name: row.get(name) for name in HASHED_FIELDS}


def event_hash(row: Mapping[str, Any]) -> str:
    """sha256(prev_hash || canonical JSON of the hashed fields), hex."""
    prev_hash = row.get("prev_hash")
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        raise ValueError("an event row needs its 64-hex prev_hash")
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(canonical_json(hashed_view(row)))
    return digest.hexdigest()


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    events: int
    head: str
    # The first row that does not hold, and why; None when the chain is intact.
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(original_sha256: str, rows: Iterable[Mapping[str, Any]]) -> ChainCheck:
    """Recompute a request's chain from the original document's digest.

    ``rows`` are the request's events in any order; they are checked in ``seq``
    order. Broken: a gap or a repeat in ``seq``, a ``prev_hash`` that is not the
    previous row's hash, or a stored ``hash`` that does not match the row.
    """
    ordered = sorted(rows, key=lambda row: int(row["seq"]))
    expected_prev = original_sha256
    for index, row in enumerate(ordered, start=1):
        seq = int(row["seq"])
        if seq != index:
            return ChainCheck(False, len(ordered), expected_prev, seq, "sequence")
        if row.get("prev_hash") != expected_prev:
            return ChainCheck(False, len(ordered), expected_prev, seq, "prev_hash")
        if event_hash(row) != row.get("hash"):
            return ChainCheck(False, len(ordered), expected_prev, seq, "hash")
        expected_prev = str(row["hash"])
    return ChainCheck(True, len(ordered), expected_prev)
