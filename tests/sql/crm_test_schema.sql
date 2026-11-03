-- Test-only DDL for the CRM tables this service reads, plus the two it appends
-- to through core/notifications (notification, outbound_message).
--
-- The real CRM schema is owned by crm-backend; this is the minimum the suite
-- needs, with the production column types (from administrative-document's
-- mirror of crm-backend's database_script/init.sql).

CREATE TABLE IF NOT EXISTS community (
    id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                     VARCHAR(255) NOT NULL UNIQUE,
    auth_community_id        VARCHAR(255) NOT NULL UNIQUE,
    regulator                VARCHAR(32)  NOT NULL DEFAULT 'BE-WAL-CWAPE',
    vat_number               VARCHAR(32),
    legal_name               VARCHAR(255),
    iban                     VARCHAR(34),
    account_holder_name      VARCHAR(255),
    -- crm-backend 2026-11-01_community_representative_documents.sql (phase 4).
    representative_first_name VARCHAR(255),
    representative_last_name  VARCHAR(255),
    representative_email      VARCHAR(255),
    statutes_date             DATE,
    internal_rules_date       DATE,
    headquarters_address_id  INTEGER,
    created_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS community_subscription (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER     NOT NULL,
    feature      VARCHAR(64) NOT NULL,
    is_active    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_community_subscription_community_feature
        UNIQUE (id_community, feature)
);

CREATE TABLE IF NOT EXISTS manager (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         VARCHAR(255),
    email        VARCHAR(255),
    id_community INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS member (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                VARCHAR(255) NOT NULL,
    member_type         INTEGER NOT NULL,   -- 1=INDIVIDUAL, 2=COMPANY
    status              INTEGER,            -- 1=ACTIVE, 2=INACTIVE, 3=PENDING
    iban                VARCHAR(255),
    id_home_address     INTEGER,
    id_billing_address  INTEGER,
    id_community        INTEGER NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS individual (
    id            INTEGER PRIMARY KEY REFERENCES member (id),
    first_name    VARCHAR(255),
    nrn           VARCHAR(255),
    email         VARCHAR(255),
    phone_number  VARCHAR(255),
    social_rate   BOOLEAN NOT NULL DEFAULT FALSE,
    id_manager    INTEGER
);

CREATE TABLE IF NOT EXISTS company (
    id          INTEGER PRIMARY KEY REFERENCES member (id),
    vat_number  VARCHAR(255),
    id_manager  INTEGER
);

CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_user_id  VARCHAR(255) NOT NULL UNIQUE,
    email         VARCHAR(256) NOT NULL,
    -- Preferred language, resolved onto every queued email at enqueue time.
    locale        VARCHAR(8)   NULL,
    -- Denormalised onto the queued row as the recipient display name.
    first_name    TEXT         NULL,
    last_name     TEXT         NULL
);

CREATE TABLE IF NOT EXISTS user_member_link (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_user   INTEGER NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    id_member INTEGER NOT NULL REFERENCES member (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS community_user (
    id_community INTEGER REFERENCES community(id) ON DELETE CASCADE,
    id_user      INTEGER REFERENCES app_user(id) ON DELETE CASCADE,
    role         VARCHAR(50) NOT NULL,
    PRIMARY KEY (id_community, id_user)
);

CREATE TABLE IF NOT EXISTS notification (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER REFERENCES community(id) ON DELETE CASCADE,
    id_user      INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    type         VARCHAR(128) NOT NULL,
    data         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    read_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS outbound_message (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_notification BIGINT NULL REFERENCES notification(id) ON DELETE SET NULL,
    id_community    INTEGER NULL REFERENCES community(id) ON DELETE CASCADE,
    channel         SMALLINT     NOT NULL CHECK (channel IN (1, 2)),
    recipient       VARCHAR(320) NOT NULL,
    recipient_name  VARCHAR(255) NULL,
    locale          VARCHAR(8)   NOT NULL DEFAULT '',
    type            VARCHAR(128) NOT NULL,
    category        SMALLINT     NOT NULL CHECK (category IN (1, 2)),
    data            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key      VARCHAR(200) NOT NULL,
    status          SMALLINT     NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3, 4, 5)),
    attempts        SMALLINT     NOT NULL DEFAULT 0,
    last_error      TEXT         NULL,
    scheduled_for   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at      TIMESTAMPTZ  NULL,
    sent_at         TIMESTAMPTZ  NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS email_suppression (
    email      VARCHAR(320) PRIMARY KEY,
    reason     SMALLINT     NOT NULL CHECK (reason IN (1, 2, 3, 4)),
    detail     TEXT         NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notification_preference (
    id_user     INTEGER      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    type_prefix VARCHAR(128) NOT NULL,
    channel     SMALLINT     NOT NULL CHECK (channel IN (1, 2)),
    -- 1 IMMEDIATE, 3 OFF
    mode        SMALLINT     NOT NULL CHECK (mode IN (1, 3)),

    PRIMARY KEY (id_user, type_prefix, channel)
);

CREATE INDEX IF NOT EXISTS idx_community_subscription_id_community
    ON community_subscription (id_community);

CREATE INDEX IF NOT EXISTS idx_user_member_link_user ON user_member_link (id_user);

CREATE INDEX IF NOT EXISTS idx_user_member_link_member ON user_member_link (id_member);

CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_message_dedupe
    ON outbound_message (dedupe_key);

CREATE INDEX IF NOT EXISTS ix_outbound_message_due
    ON outbound_message (scheduled_for) WHERE status = 1;

CREATE INDEX IF NOT EXISTS ix_outbound_message_stale
    ON outbound_message (claimed_at) WHERE status = 5;
