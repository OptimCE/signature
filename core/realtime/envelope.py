"""Realtime envelope construction and validation.

An envelope is a HINT — "something about resource X changed" — never data. The
rules below mirror ``crm-backend/src/shared/realtime/realtime.envelope.ts``,
which re-validates everything on the way out to a browser:

* **No business data.** No names, emails, EANs, amounts, invoice numbers,
  storage keys, error messages.
* **No display strings.** Toast text is chosen client-side from ``topic`` +
  ``hint["status"]`` against the i18n bundle. This is a security control: a
  compromised publisher gets a nuisance channel, never a text-injection channel
  into every open browser.
* **No recipient field.** The channel already says who. A recipient in the body
  invites a subscriber-side "is this for me?" check — authorization on the wrong
  leg.

``ref.id`` is permitted: any authorized reader can already see it, and the
client needs it to decide *which* row to refetch.
"""

import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

#: Hard ceiling on a serialized envelope, in BYTES (not characters).
MAX_ENVELOPE_BYTES: Final[int] = 1024

#: The topic registry. Mirrors crm-backend's realtime.topics.ts; an unknown topic
#: is dropped by the hub rather than forwarded, so publishing one is a silent
#: no-op that is much easier to find here.
TOPICS: Final[frozenset[str]] = frozenset(
    {
        "notification.created",
        "generation.finished",
        "simulation.finished",
        "billing_run.finished",
        "session.revoked",
        # The espace administratif (administrative-document), for managers.
        "admin.change.detected",
        "admin.dossier.updated",
        "admin.document.ready",
        "admin.deadline.due",
        # The signature annexe: a signer's seal done (the signing screen waits
        # for it), and any change of a request (inbox, follow-up view).
        "signature.signed",
        "signature.request.updated",
    }
)

_SCALARS = (str, int, float, bool)


def _is_scalar(value: Any) -> bool:
    # bool is a subclass of int, so it is already covered; None is allowed.
    return value is None or isinstance(value, _SCALARS)


def build_envelope(
    *,
    topic: str,
    resource: tuple[str, str | int],
    hint: Mapping[str, str | int | float | bool | None] | None = None,
    scope_community_id: int | None = None,
) -> dict[str, Any] | None:
    """Build a valid envelope, or ``None`` if the input violates the contract.

    Returns ``None`` rather than raising: every caller is a fire-and-forget side
    effect that must never affect a business write, so a malformed hint has to
    degrade to "no event", not to an exception travelling up through a commit
    path.
    """
    if topic not in TOPICS:
        return None

    kind, ref_id = resource
    if not kind or ref_id is None:
        return None

    flat: dict[str, Any] = {}
    for key, value in (hint or {}).items():
        if not _is_scalar(value):
            return None
        flat[str(key)] = value

    envelope: dict[str, Any] = {
        "v": 1,
        # A client-side dedupe key. Not sortable on purpose: there is no replay,
        # so ordering buys nothing.
        "id": secrets.token_hex(8),
        "topic": topic,
        "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "scope": {"community_id": scope_community_id},
        "ref": {"kind": str(kind), "id": str(ref_id)},
        "hint": flat,
    }

    if len(json.dumps(envelope, separators=(",", ":")).encode("utf-8")) > MAX_ENVELOPE_BYTES:
        return None
    return envelope
