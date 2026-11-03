"""Constants of the signature annexe.

Coded values are 1-based SMALLINTs, as everywhere in OptimCE; the database
CHECKs list the same sets (scripts/sql/schema.sql). Strings that travel
(NATS subjects, event and slot kinds) are named here once.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class FeatureName(StrEnum):
    """The annexe catalogue's feature key (crm-backend config/annexes-services.json)."""

    SIGNATURE = "signature"


class RequestStatus(IntEnum):
    PREPARING = 1
    OPEN = 2
    COMPLETING = 3
    COMPLETED = 4
    DECLINED = 5
    CANCELLED = 6
    EXPIRED = 7
    FAILED = 8


FINAL_REQUEST_STATUSES = frozenset(
    {
        RequestStatus.COMPLETED,
        RequestStatus.DECLINED,
        RequestStatus.CANCELLED,
        RequestStatus.EXPIRED,
        RequestStatus.FAILED,
    }
)


class SignerStatus(IntEnum):
    WAITING = 1  # SEQUENTIAL, not their turn yet
    INVITED = 2
    VIEWED = 3
    # The sign call was accepted (202) and the seal job is queued: a second
    # call answers 409 instead of queueing a second signature.
    SIGNING = 4
    SIGNED = 5
    DECLINED = 6


class Capacity(IntEnum):
    SELF = 1
    LEGAL_REPRESENTATIVE = 2
    COMMUNITY_ADMIN = 3


class SigningOrder(IntEnum):
    PARALLEL = 1
    SEQUENTIAL = 2


class SignMethod(IntEnum):
    TYPED = 1
    DRAWN = 2


class FieldType(IntEnum):
    DATE = 1
    INITIALS = 2
    CHECKBOX = 3
    TEXT = 4


class RevisionKind(IntEnum):
    PREPARED = 1
    SIGNED = 2
    FINAL = 3


class SlotKind(StrEnum):
    """Where a signer's field goes, in this order of preference."""

    FIELD = "field"  # an existing field of the PDF, by name (the CWaPE forms)
    ANCHOR = "anchor"  # an invisible tag the docgen template prints, e.g. [[sig:member]]
    BOX = "box"  # an explicit page and rectangle, in PDF points
    PAGE = "page"  # a signature page appended at preparation


class EventType(StrEnum):
    """The evidence log's event types (``signature_event.type``)."""

    REQUEST_CREATED = "request.created"
    DOCUMENT_PREPARED = "document.prepared"
    SIGNER_INVITED = "signer.invited"
    SIGNER_VIEWED = "signer.viewed"
    SIGNER_REAUTHENTICATED = "signer.reauthenticated"
    SIGNER_SIGNED = "signer.signed"
    SIGNER_DECLINED = "signer.declined"
    REQUEST_REMINDED = "request.reminded"
    REQUEST_COMPLETED = "request.completed"
    REQUEST_CANCELLED = "request.cancelled"
    REQUEST_EXPIRED = "request.expired"
    REQUEST_FAILED = "request.failed"


class DomainEvent(StrEnum):
    """What a requesting domain hears back, on ``signature.event.<source>.<type>``."""

    ACCEPTED = "accepted"
    PREPARED = "prepared"
    SIGNER_SIGNED = "signer_signed"
    COMPLETED = "completed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class FailureCode(StrEnum):
    """Why a request was refused or failed; carried by the ``failed`` event."""

    INVALID_REQUEST = "invalid_request"
    MODULE_DISABLED = "module_disabled"
    UNKNOWN_SOURCE = "unknown_source"
    DOCUMENT_OUTSIDE_PREFIX = "document_outside_prefix"
    DOCUMENT_NOT_FOUND = "document_not_found"
    MEMBER_NOT_FOUND = "member_not_found"
    MEMBER_WITHOUT_ACCOUNT = "member_without_account"
    USER_NOT_ADMIN = "user_not_admin"
    SLOT_NOT_FOUND = "slot_not_found"
    PREPARATION_FAILED = "preparation_failed"
    SEAL_FAILED = "seal_failed"
    EVIDENCE_FAILED = "evidence_failed"


LOCALES = ("fr", "en", "nl", "de")
DEFAULT_LOCALE = "fr"

# The consent statement a signer ticks; its texts live in consent/<version>/.
CONSENT_VERSION = "v2"

# ---- NATS -------------------------------------------------------------------
SUBJECT_REQUEST_CREATE = "signature.request.create"
SUBJECT_REQUEST_CANCEL = "signature.request.cancel"
SUBJECT_JOB_PREPARE = "signature.job.prepare"
SUBJECT_JOB_SIGN = "signature.job.sign"
SUBJECT_JOB_COMPLETE = "signature.job.complete"
SUBJECT_EVENT_PREFIX = "signature.event"
SUBJECT_DLQ_PREFIX = "signature.dlq"

EVENT_REQUEST_CREATE = "signature.request.create"
EVENT_REQUEST_CANCEL = "signature.request.cancel"
EVENT_JOB = "signature.job"


def domain_event_subject(source_service: str, event: DomainEvent) -> str:
    return f"{SUBJECT_EVENT_PREFIX}.{source_service}.{event.value}"


# ---- Storage ----------------------------------------------------------------
# Every revision, artifact and drawn image lives under this prefix of
# OUTPUT_BUCKET, one folder per request: signature/{id_community}/{request_id}/.
STORAGE_KEY_PREFIX = "signature"

# A requesting domain may only name a document under its own prefix of
# OUTPUT_BUCKET; {id_community} is the request's. Unknown sources are refused.
SOURCE_DOCUMENT_PREFIXES: dict[str, str] = {
    "administrative_document": "administrative-document/{id_community}/",
}

# ---- Limits -----------------------------------------------------------------
DEFAULT_EXPIRY_DAYS = 30
MAX_SIGNERS = 10
MAX_FIELDS_PER_SIGNER = 10
MAX_TEXT_FIELD_LENGTH = 200
# A drawn signature arrives as a PNG data URL; this bounds the decoded bytes.
MAX_DRAWN_PNG_BYTES = 300 * 1024
# The upload middleware's cap for this service: the sign call is the largest
# body (a drawn PNG, base64) and nothing else takes a file.
UPLOAD_MAX_BODY_BYTES = 1024 * 1024
# A reminder per request at most once a day.
REMIND_INTERVAL_HOURS = 24
# Sign + decline calls per user and minute.
SIGN_CALLS_PER_MINUTE = 10
