from core.errors.errors import Error


# ---------------------------------------------------------------------------
# Auth (no domain code — use xxx)
# ---------------------------------------------------------------------------
class _AuthErrors:
    UNAUTHORIZED = Error(code=1, key="ERRORS.AUTH.UNAUTHORIZED")
    FORBIDDEN = Error(code=2, key="ERRORS.AUTH.FORBIDDEN")
    RATE_LIMITED = Error(code=3, key="ERRORS.AUTH.RATE_LIMITED")
    AUTHORIZATION_MISSING = Error(code=4, key="ERRORS.AUTH.AUTHORIZATION_MISSING")


class _SubscriptionErrors:
    NOT_SUBSCRIBED = Error(code=1003, key="ERRORS.SUBSCRIPTION.NOT_SUBSCRIBED")


class _SignatureErrors:
    """Signature domain errors (2500 block)."""

    # ---- reads ----
    REQUEST_NOT_FOUND = Error(code=2500, key="ERRORS.SIGNATURE.REQUEST_NOT_FOUND")
    SIGNER_NOT_FOUND = Error(code=2501, key="ERRORS.SIGNATURE.SIGNER_NOT_FOUND")
    # The caller is not (or no longer) the person this slot expects. → 403.
    NOT_YOUR_SLOT = Error(code=2502, key="ERRORS.SIGNATURE.NOT_YOUR_SLOT")
    # The community's signature module is off: an explicit error, never an
    # empty list. → 403.
    MODULE_DISABLED = Error(code=2503, key="ERRORS.SIGNATURE.MODULE_DISABLED")
    ARTIFACT_NOT_READY = Error(code=2504, key="ERRORS.SIGNATURE.ARTIFACT_NOT_READY")
    STORAGE_FAILED = Error(code=2505, key="ERRORS.SIGNATURE.STORAGE_FAILED")

    # ---- signing ----
    # The token's last interactive login is older than the allowed window. → 401.
    STEP_UP_REQUIRED = Error(code=2510, key="ERRORS.SIGNATURE.STEP_UP_REQUIRED")
    # The hash sent back is not one of this request's revisions. → 409.
    DOCUMENT_CHANGED = Error(code=2511, key="ERRORS.SIGNATURE.DOCUMENT_CHANGED")
    # Signed, signing, declined, or not their turn yet. → 409.
    SLOT_NOT_OPEN = Error(code=2512, key="ERRORS.SIGNATURE.SLOT_NOT_OPEN")
    REQUEST_NOT_OPEN = Error(code=2513, key="ERRORS.SIGNATURE.REQUEST_NOT_OPEN")
    CONSENT_REQUIRED = Error(code=2514, key="ERRORS.SIGNATURE.CONSENT_REQUIRED")
    REQUIRED_FIELD_MISSING = Error(code=2515, key="ERRORS.SIGNATURE.REQUIRED_FIELD_MISSING")
    INVALID_FIELD_VALUE = Error(code=2516, key="ERRORS.SIGNATURE.INVALID_FIELD_VALUE")
    INVALID_SIGNATURE_IMAGE = Error(code=2517, key="ERRORS.SIGNATURE.INVALID_SIGNATURE_IMAGE")
    TYPED_NAME_REQUIRED = Error(code=2518, key="ERRORS.SIGNATURE.TYPED_NAME_REQUIRED")
    DECLINE_REASON_REQUIRED = Error(code=2519, key="ERRORS.SIGNATURE.DECLINE_REASON_REQUIRED")
    TOO_MANY_ATTEMPTS = Error(code=2520, key="ERRORS.SIGNATURE.TOO_MANY_ATTEMPTS")

    # ---- follow-up ----
    REMINDED_RECENTLY = Error(code=2530, key="ERRORS.SIGNATURE.REMINDED_RECENTLY")
    QUEUE_UNAVAILABLE = Error(code=2531, key="ERRORS.SIGNATURE.QUEUE_UNAVAILABLE")


class _Errors:
    auth = _AuthErrors()
    subscription = _SubscriptionErrors()
    signature = _SignatureErrors()


errors = _Errors()
