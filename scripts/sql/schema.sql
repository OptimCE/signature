-- ============================================================================
-- Local database schema (LOCAL_DATABASE_URL) — signature service.
--
-- Single source of truth for the LOCAL (owned) database `signature_local`.
-- The CRM core tables (community, member, app_user, user_member_link,
-- community_user, community_subscription) live in another database and are
-- only read (ports/crm_read.py). Cross-DB references (id_community,
-- id_member, user ids) are plain columns, never foreign keys.
--
-- Mirrors shared/models/local_models.py. Unreleased: this file is the
-- migration until the first release; after it, a change also needs a forward
-- migration under scripts/sql/migrations/.
--
-- Invariants the database enforces, because the evidence must be unforgeable
-- by any client, this service included:
--   1. signature_event and signature_revision are APPEND-ONLY (UPDATE/DELETE
--      raise).
--   2. signature_event is a hash chain: each row's prev_hash is the previous
--      row's hash (the first one chains from the original document's digest)
--      and its seq follows the previous one. The trigger checks the
--      continuity; domain/evidence.verify_chain recomputes the hashes.
--
-- Codes are SMALLINTs (shared/const.py); every multi-branch CHECK is wrapped
-- in COALESCE(…, FALSE), since a CHECK evaluating to NULL lets the row pass.
-- ============================================================================

-- ---- Shared utilities ------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION forbid_mutation()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS schema_version (
    version      INTEGER     PRIMARY KEY,
    description  TEXT        NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO schema_version (version, description) VALUES
    (1, 'Signature initial schema')
ON CONFLICT DO NOTHING;

-- ---- signature_request -----------------------------------------------------
-- One per document to sign.
--   signing_order  1 parallel, 2 sequential
--   status         1 preparing, 2 open, 3 completing, 4 completed,
--                  5 declined, 6 cancelled, 7 expired, 8 failed
-- (source_service, idempotency_key) is the domain's idempotency: the same key
-- sent again names the same request.
CREATE TABLE IF NOT EXISTS signature_request (
    id                    UUID          PRIMARY KEY,
    id_community          INTEGER       NOT NULL,
    source_service        VARCHAR(64)   NOT NULL,
    source_ref            VARCHAR(255)  NOT NULL,
    idempotency_key       VARCHAR(255)  NOT NULL,
    title                 VARCHAR(255)  NOT NULL,
    locale                VARCHAR(2)    NOT NULL,
    original_key          TEXT          NOT NULL,
    original_sha256       CHAR(64)      NOT NULL,
    current_revision      INTEGER       NOT NULL DEFAULT 0,
    signing_order         SMALLINT      NOT NULL,
    status                SMALLINT      NOT NULL,
    consent_version       VARCHAR(16)   NOT NULL,
    requested_by_sub      VARCHAR(255),
    requested_by_user_id  INTEGER,
    expires_at            TIMESTAMPTZ   NOT NULL,
    final_key             TEXT,
    final_sha256          CHAR(64),
    evidence_json_key     TEXT,
    evidence_json_sha256  CHAR(64),
    evidence_pdf_key      TEXT,
    evidence_pdf_sha256   CHAR(64),
    -- The evidence render in flight at docgen (correlates its result).
    evidence_request_id   UUID,
    failure_code          VARCHAR(64),
    failure_detail        TEXT,
    last_reminded_at      TIMESTAMPTZ,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    closed_at             TIMESTAMPTZ,
    CONSTRAINT uq_signature_request_idempotency UNIQUE (source_service, idempotency_key),
    CONSTRAINT ck_signature_request_order CHECK (COALESCE(signing_order IN (1, 2), FALSE)),
    CONSTRAINT ck_signature_request_status
        CHECK (COALESCE(status IN (1, 2, 3, 4, 5, 6, 7, 8), FALSE)),
    CONSTRAINT ck_signature_request_locale
        CHECK (COALESCE(locale IN ('fr', 'en', 'nl', 'de'), FALSE)),
    CONSTRAINT ck_signature_request_source
        CHECK (COALESCE(source_service ~ '^[a-z][a-z0-9_]{1,62}$', FALSE)),
    -- A final status carries its closing time; a live one has none.
    CONSTRAINT ck_signature_request_closed
        CHECK (COALESCE((status IN (4, 5, 6, 7, 8)) = (closed_at IS NOT NULL), FALSE)),
    -- COMPLETED carries its three artifacts.
    CONSTRAINT ck_signature_request_completed CHECK (
        status <> 4
        OR (final_key IS NOT NULL AND final_sha256 IS NOT NULL
            AND evidence_json_key IS NOT NULL AND evidence_pdf_key IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_signature_request_community
    ON signature_request (id_community, status, created_at DESC);
-- The hourly sweep: open requests past their expiry.
CREATE INDEX IF NOT EXISTS ix_signature_request_expiry
    ON signature_request (expires_at) WHERE status IN (1, 2);
CREATE UNIQUE INDEX IF NOT EXISTS uq_signature_request_evidence
    ON signature_request (evidence_request_id) WHERE evidence_request_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_signature_request_updated_at ON signature_request;
CREATE TRIGGER trg_signature_request_updated_at BEFORE UPDATE ON signature_request
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- signature_signer ------------------------------------------------------
-- One per expected signature.
--   capacity  1 self, 2 legal representative, 3 community admin
--   status    1 waiting, 2 invited, 3 viewed, 4 signing (sign accepted, seal
--             queued), 5 signed, 6 declined
--   method    1 typed, 2 drawn
-- A member's slot is signed by any user linked to the member at signing time
-- (expected_user_sub NULL); a community admin's slot names its user. Who
-- actually signed is signed_by_sub / signed_by_user_id.
CREATE TABLE IF NOT EXISTS signature_signer (
    id                 UUID          PRIMARY KEY,
    request_id         UUID          NOT NULL REFERENCES signature_request (id),
    position           SMALLINT      NOT NULL,
    id_member          INTEGER,
    capacity           SMALLINT      NOT NULL,
    expected_user_sub  VARCHAR(255),
    display_name       VARCHAR(255)  NOT NULL,
    email              VARCHAR(320),
    -- The company a legal representative signs for ("ACME SRL").
    member_label       VARCHAR(255),
    role_label         VARCHAR(255)  NOT NULL,
    slot               JSONB         NOT NULL,
    -- Where preparation put the field: {"page": n, "rect": [x1, y1, x2, y2]}.
    slot_resolved      JSONB,
    field_name         VARCHAR(255)  NOT NULL,
    status             SMALLINT      NOT NULL,
    method             SMALLINT,
    typed_name         VARCHAR(255),
    image_key          TEXT,
    image_sha256       CHAR(64),
    signed_by_sub      VARCHAR(255),
    signed_by_user_id  INTEGER,
    -- The revision the signer viewed when they signed, and the one holding
    -- their signature.
    viewed_revision    INTEGER,
    signed_revision    INTEGER,
    invited_at         TIMESTAMPTZ,
    signed_at          TIMESTAMPTZ,
    declined_at        TIMESTAMPTZ,
    decline_reason     TEXT,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_signature_signer_position UNIQUE (request_id, position),
    CONSTRAINT uq_signature_signer_field UNIQUE (request_id, field_name),
    CONSTRAINT ck_signature_signer_capacity CHECK (COALESCE(capacity IN (1, 2, 3), FALSE)),
    CONSTRAINT ck_signature_signer_status CHECK (COALESCE(status IN (1, 2, 3, 4, 5, 6), FALSE)),
    CONSTRAINT ck_signature_signer_method CHECK (method IS NULL OR method IN (1, 2)),
    -- A member's slot names its member; an admin's slot names its user.
    CONSTRAINT ck_signature_signer_who CHECK (COALESCE(
        CASE WHEN capacity = 3 THEN id_member IS NULL AND expected_user_sub IS NOT NULL
             ELSE id_member IS NOT NULL END,
        FALSE)),
    -- From the sign call on, the slot carries how it is signed and by whom.
    CONSTRAINT ck_signature_signer_signing CHECK (
        status NOT IN (4, 5)
        OR (method IS NOT NULL AND image_key IS NOT NULL AND signed_by_sub IS NOT NULL)),
    CONSTRAINT ck_signature_signer_signed CHECK (
        status <> 5 OR (signed_at IS NOT NULL AND signed_revision IS NOT NULL)),
    CONSTRAINT ck_signature_signer_declined CHECK (
        status <> 6 OR (declined_at IS NOT NULL AND decline_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_signature_signer_member ON signature_signer (id_member, status);
CREATE INDEX IF NOT EXISTS ix_signature_signer_user ON signature_signer (expected_user_sub, status);

DROP TRIGGER IF EXISTS trg_signature_signer_updated_at ON signature_signer;
CREATE TRIGGER trg_signature_signer_updated_at BEFORE UPDATE ON signature_signer
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- signature_field -------------------------------------------------------
-- One per field a signer fills, declared by the requesting domain.
--   type  1 date (the server writes the signing date, never editable),
--         2 initials, 3 checkbox, 4 text
-- `value` is written by the sign call and sealed by the sign job
-- (`filled_at`, when the revision holding it was written).
CREATE TABLE IF NOT EXISTS signature_field (
    id              UUID          PRIMARY KEY,
    signer_id       UUID          NOT NULL REFERENCES signature_signer (id),
    name            VARCHAR(64)   NOT NULL,
    type            SMALLINT      NOT NULL,
    label           VARCHAR(255),
    required        BOOLEAN       NOT NULL DEFAULT FALSE,
    slot            JSONB         NOT NULL,
    slot_resolved   JSONB,
    pdf_field_name  VARCHAR(255)  NOT NULL,
    value           TEXT,
    filled_at       TIMESTAMPTZ,
    CONSTRAINT uq_signature_field_name UNIQUE (signer_id, name),
    CONSTRAINT ck_signature_field_type CHECK (COALESCE(type IN (1, 2, 3, 4), FALSE))
);

-- ---- signature_revision ----------------------------------------------------
-- One row per PDF written to the bucket; never overwritten (append-only).
--   kind  1 prepared (revision 1: the document as issued, certified),
--         2 signed (one per signature), 3 final (PAdES-B-LTA)
-- Revision 0 is the original itself (signature_request.original_key).
CREATE TABLE IF NOT EXISTS signature_revision (
    request_id  UUID          NOT NULL REFERENCES signature_request (id),
    number      INTEGER       NOT NULL,
    key         TEXT          NOT NULL,
    sha256      CHAR(64)      NOT NULL,
    kind        SMALLINT      NOT NULL,
    signer_id   UUID          REFERENCES signature_signer (id),
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (request_id, number),
    CONSTRAINT ck_signature_revision_kind CHECK (COALESCE(kind IN (1, 2, 3), FALSE)),
    CONSTRAINT ck_signature_revision_number CHECK (number >= 1),
    CONSTRAINT ck_signature_revision_signer CHECK (COALESCE((kind = 2) = (signer_id IS NOT NULL), FALSE))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_signature_revision_sha
    ON signature_revision (request_id, sha256);

DROP TRIGGER IF EXISTS trg_signature_revision_immutable ON signature_revision;
CREATE TRIGGER trg_signature_revision_immutable
    BEFORE UPDATE OR DELETE ON signature_revision
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---- signature_event -------------------------------------------------------
-- THE EVIDENCE LOG: append-only and hash-chained, per request.
--   hash = sha256(prev_hash || canonical JSON of the row without id and hash)
-- (domain/evidence.py says exactly which fields and how). `at` is the server's
-- UTC time; the actor's identity comes from the gateway's headers.
CREATE TABLE IF NOT EXISTS signature_event (
    id               BIGSERIAL     PRIMARY KEY,
    request_id       UUID          NOT NULL REFERENCES signature_request (id),
    seq              INTEGER       NOT NULL,
    signer_id        UUID          REFERENCES signature_signer (id),
    type             VARCHAR(64)   NOT NULL,
    at               TIMESTAMPTZ   NOT NULL,
    actor_sub        VARCHAR(255),
    actor_user_id    INTEGER,
    ip               VARCHAR(64),
    user_agent       VARCHAR(512),
    keycloak_sid     VARCHAR(255),
    auth_time        TIMESTAMPTZ,
    acr              VARCHAR(64),
    document_sha256  CHAR(64),
    consent_version  VARCHAR(16),
    payload          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    prev_hash        CHAR(64)      NOT NULL,
    hash             CHAR(64)      NOT NULL,
    CONSTRAINT uq_signature_event_seq UNIQUE (request_id, seq),
    CONSTRAINT uq_signature_event_hash UNIQUE (hash),
    CONSTRAINT ck_signature_event_type CHECK (COALESCE(type IN (
        'request.created', 'document.prepared', 'signer.invited', 'signer.viewed',
        'signer.reauthenticated', 'signer.signed', 'signer.declined', 'request.reminded',
        'request.completed', 'request.cancelled', 'request.expired', 'request.failed'), FALSE))
);

DROP TRIGGER IF EXISTS trg_signature_event_immutable ON signature_event;
CREATE TRIGGER trg_signature_event_immutable
    BEFORE UPDATE OR DELETE ON signature_event
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- The chain's continuity: seq follows the request's last row and prev_hash is
-- its hash (the original document's digest for the first row). Two appends
-- racing for the same seq are already stopped by uq_signature_event_seq; this
-- also stops a row that skips one or chains from the wrong place.
CREATE OR REPLACE FUNCTION check_signature_event_chain()
RETURNS trigger AS $$
DECLARE
    v_seq   INTEGER;
    v_hash  CHAR(64);
BEGIN
    SELECT seq, hash INTO v_seq, v_hash
      FROM signature_event
     WHERE request_id = NEW.request_id
     ORDER BY seq DESC
     LIMIT 1;
    IF v_seq IS NULL THEN
        SELECT original_sha256 INTO v_hash FROM signature_request WHERE id = NEW.request_id;
        v_seq := 0;
    END IF;
    IF NEW.seq <> v_seq + 1 OR NEW.prev_hash IS DISTINCT FROM v_hash THEN
        RAISE EXCEPTION
            'signature_event: request % expects seq % chained from %, got seq % from %',
            NEW.request_id, v_seq + 1, v_hash, NEW.seq, NEW.prev_hash
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_signature_event_chain ON signature_event;
CREATE TRIGGER trg_signature_event_chain
    BEFORE INSERT ON signature_event
    FOR EACH ROW EXECUTE FUNCTION check_signature_event_chain();
