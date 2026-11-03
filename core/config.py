import os
from enum import StrEnum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# CORS contract:
# - Local/test: ALLOW_ORIGIN may default to "*".
# - Staging/production: ALLOW_ORIGIN is REQUIRED, must not contain "*", and may
#   be a comma-separated list (e.g. "https://app.example.com,https://admin.example.com").
# Enforced in Settings.validate_env_config below; example values live in
# .env.staging.exemple and .env.production.exemple.
class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


def _get_env_file() -> str:
    env = os.getenv("ENV", "local").strip()
    return f".env.{env}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_get_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ---- Core ----
    ENV: Environment = Environment.LOCAL

    # ---- CRM Database ----
    CRM_DATABASE_URL: str  # postgresql+asyncpg://...
    CRM_DB_POOL_SIZE: int = 20
    CRM_DB_MAX_OVERFLOW: int = 10
    CRM_DB_POOL_RECYCLE: int = 3600  # seconds — recycle connections after 1 hour
    CRM_DB_POOL_TIMEOUT: int = 30  # seconds — wait for available connection
    CRM_DB_SSL: bool = False  # enable SSL/TLS for database connection

    # ---- LOCAL Database ----
    LOCAL_DATABASE_URL: str  # postgresql+asyncpg://...
    LOCAL_DB_POOL_SIZE: int = 20
    LOCAL_DB_MAX_OVERFLOW: int = 10
    LOCAL_DB_POOL_RECYCLE: int = 3600  # seconds — recycle connections after 1 hour
    LOCAL_DB_POOL_TIMEOUT: int = 30  # seconds — wait for available connection
    LOCAL_DB_SSL: bool = False  # enable SSL/TLS for database connection
    # ---- NATS ----
    NATS_URL: str = ""

    # ---- Storage (S3-compatible, MinIO in dev) ----
    # Endpoint URL of the S3-compatible storage server (e.g. http://minio:9000).
    # Empty in local/test where the storage module is mocked.
    STORAGE_ENDPOINT: str = ""
    STORAGE_BUCKET: str = "crm-files"
    STORAGE_ACCESS_KEY: str = ""
    STORAGE_SECRET_KEY: str = ""
    # MinIO ignores region but botocore still requires it to sign requests.
    STORAGE_REGION: str = "us-east-1"
    # The platform documents bucket: originals are read from the requesting
    # domain's prefix, every revision and artifact is written under
    # signature/{id_community}/{request_id}/ (shared.const.STORAGE_KEY_PREFIX).
    OUTPUT_BUCKET: str = "optimce-documents"
    # Holds the evidence report's bundle; document-generation reads it.
    TEMPLATES_BUCKET: str = "optimce-templates"

    # ---- document-generation (the evidence report) ----
    DOCGEN_REQUEST_SUBJECT: str = "docgen.request"
    DOCGEN_RESULT_SUBJECT: str = "docgen.result.signature"
    DOCGEN_PRESIGN_TTL: int = 3600
    EVIDENCE_TEMPLATE_URI: str = "s3://optimce-templates/signature/evidence/v1/"

    # ---- The seal (worker only) ----
    # The platform's seal certificate and key, a PKCS#12 file mounted as a
    # secret, readable by the seal worker only; never in the database. The API
    # does not need it.
    SEAL_PKCS12_PATH: str = ""
    SEAL_PKCS12_PASSPHRASE: str = ""
    # Extra CA certificates (PEM or DER files, comma-separated) the chain needs
    # when the PKCS#12 does not carry them.
    SEAL_CA_CHAIN_PATHS: str = ""
    # Trust anchors for the validation data embedded at completion (PEM or DER
    # files, comma-separated). Production: the seal's root. Dev: the test CA.
    SEAL_TRUST_ROOT_PATHS: str = ""
    # RFC 3161 timestamp authorities, tried in order (comma-separated URLs).
    TSA_URLS: str = ""
    TSA_TIMEOUT_SECONDS: int = 10

    # ---- Signing ----
    # The sign call needs an interactive login at most this old (the token's
    # auth_time, forwarded by the gateway as x-auth-time). 0 turns the check
    # off; only the test environment may do that.
    STEP_UP_MAX_AGE_SECONDS: int = 300
    # Where the in-app links of the notifications point (the SPA's origin).
    APP_BASE_URL: str = "http://localhost:8087"
    # The hourly sweep (expiry). Off in the test environment.
    SWEEP_ENABLED: bool = True
    SWEEP_INTERVAL_SECONDS: int = 3600

    # ---- Realtime (Redis pub/sub) ----
    # Fire-and-forget SSE hints. Deliberately NOT in validate_env_config's
    # required set (contrast NATS_URL below): realtime is optional by design, and
    # making it mandatory would turn a broker outage into a boot failure. An
    # empty URL makes core.realtime.emit() a silent no-op.
    REALTIME_ENABLED: bool = False
    REALTIME_REDIS_URL: str = ""

    # ---- CORS ----
    ALLOW_ORIGIN: str = "*"

    LOGGING_TOKEN: str = ""
    LOGGING_TRACES_URL: str = ""
    LOGGING_LOGS_URL: str = ""
    LOGGING_METRICS_URL: str = ""

    @model_validator(mode="after")
    def validate_env_config(self) -> "Settings":
        if self.ENV != Environment.LOCAL:
            origins = [o.strip() for o in self.ALLOW_ORIGIN.split(",") if o.strip()]
            if not origins:
                raise ValueError(
                    "ALLOW_ORIGIN is required when ENV is not local; "
                    "set it explicitly in .env.{env} (no implicit fallback to '*')"
                )
            if "*" in self.ALLOW_ORIGIN:
                raise ValueError("Wildcard CORS not allowed in staging/production")
            if not self.CRM_DATABASE_URL.strip():
                raise ValueError("CRM_DATABASE_URL is required when ENV is not local")
            if not self.LOCAL_DATABASE_URL.strip():
                raise ValueError("LOCAL_DATABASE_URL is required when ENV is not local")
        if self.ENV in (Environment.STAGING, Environment.PRODUCTION):
            if not self.NATS_URL.strip():
                raise ValueError("NATS_URL is required in staging/production")
            if not self.STORAGE_ENDPOINT.strip():
                raise ValueError("STORAGE_ENDPOINT is required in staging/production")
            if not self.STORAGE_ACCESS_KEY.strip():
                raise ValueError("STORAGE_ACCESS_KEY is required in staging/production")
            if not self.STORAGE_SECRET_KEY.strip():
                raise ValueError("STORAGE_SECRET_KEY is required in staging/production")
            if not self.OUTPUT_BUCKET.strip():
                raise ValueError("OUTPUT_BUCKET is required in staging/production")
            if not self.TEMPLATES_BUCKET.strip():
                raise ValueError("TEMPLATES_BUCKET is required in staging/production")
            if not self.DOCGEN_REQUEST_SUBJECT.strip():
                raise ValueError("DOCGEN_REQUEST_SUBJECT is required in staging/production")
            if not self.DOCGEN_RESULT_SUBJECT.strip():
                raise ValueError("DOCGEN_RESULT_SUBJECT is required in staging/production")
            if not self.TSA_URLS.strip():
                raise ValueError("TSA_URLS is required in staging/production")
            if self.STEP_UP_MAX_AGE_SECONDS <= 0:
                raise ValueError("STEP_UP_MAX_AGE_SECONDS must be positive in staging/production")
        if self.ENV == Environment.PRODUCTION:
            if not self.LOGGING_TOKEN:
                raise ValueError("LOGGING_TOKEN required for staging/production")
            if not self.LOGGING_LOGS_URL:
                raise ValueError("LOGGING_LOGS_URL required for staging/production")
            if not self.LOGGING_METRICS_URL:
                raise ValueError("LOGGING_METRICS_URL required for staging/production")
        return self


settings = Settings()
