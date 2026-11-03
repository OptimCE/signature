"""The idempotency key for one queued outbound message.

Byte-identical across producers, like the rest of this package, and mirrored in
crm-backend's ``src/modules/notifications/shared/notification.dedupe.ts``. Keep
the two in step.
"""

import hashlib
import json
from typing import Any

# Hex characters kept from the payload digest. 128 bits.
_PAYLOAD_HASH_LENGTH = 32
# Hex characters kept from the address digest.
_ADDRESS_HASH_LENGTH = 16
# ``outbound_message.dedupe_key`` is VARCHAR(200).
_MAX_KEY_LENGTH = 200


def canonical_json(data: dict[str, Any]) -> str:
    """Deterministic JSON: keys sorted recursively, no whitespace.

    ``ensure_ascii=False`` so the output matches ``JSON.stringify`` on the
    TypeScript side; nothing requires the two languages to agree on a concrete
    key (each notification type has exactly one producer), but a gratuitous
    divergence is a trap for anyone comparing them.

    Raises on ``Decimal`` / ``date``, deliberately: ``data`` lands in a JSONB
    column and callers are required to stringify. That failure is the same one
    ``notification.data`` already has, just reached slightly earlier.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_dedupe_key(
    *,
    channel: int,
    type: str,
    data: dict[str, Any],
    id_user: int | None = None,
    recipient: str | None = None,
    override: str | None = None,
) -> str:
    """Derive the key that makes a redelivery, a re-run sweep and a retry one row.

        ``<channel>:<type>:u<id_user>:<h>``                       account-ful
        ``<channel>:<type>:a<sha256(lower(address))[:16]>:<h>``   account-less
        ``h = sha256(canonical_json(data))[:32]``

    The channel prefix is required because the table's grain is
    (message, channel, recipient), so one notification delivered over two
    channels is two rows. The ``u``/``a`` namespaces stop a user id and an
    address from ever colliding.

    **``data`` IS the idempotency key, for all time.** There is no time bucket,
    so two genuinely distinct occurrences of the same type to the same recipient
    with identical ``data`` collapse into a single message, permanently. That is
    correct for everything driven by a status transition — ``invoice.issued``,
    ``invoice.overdue``, ``admin_deadline.missed``, an invitation — each of which
    fires once per row and carries that row's id in ``data``. A producer whose
    sweep can re-emit WITHOUT mutating its source row must pass ``override``
    including the occurrence date; ``admin_deadline.due_soon`` is the one that
    does.

    Worst case length is 128 (type) + 2 + 10 + 1 + 32 = 175, inside VARCHAR(200).
    """
    if override:
        return override[:_MAX_KEY_LENGTH]
    payload_hash = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()[
        :_PAYLOAD_HASH_LENGTH
    ]
    if id_user is not None:
        recipient_ref = f"u{id_user}"
    else:
        address = (recipient or "").strip().lower()
        digest = hashlib.sha256(address.encode("utf-8")).hexdigest()[:_ADDRESS_HASH_LENGTH]
        recipient_ref = f"a{digest}"
    return f"{channel}:{type}:{recipient_ref}:{payload_hash}"


def type_prefix_of(type: str) -> str:
    """The ``notification_preference.type_prefix`` a type falls under.

    Its first dot-segment. ``''`` (the default row) is never produced here. The
    taxonomy guarantees exactly two segments, so this is total; a malformed key
    degrades to the whole string, which simply matches no preference row.
    """
    head, _, _ = type.partition(".")
    return head
