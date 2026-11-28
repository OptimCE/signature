<p align="center">
  <img src="docs/logo.svg" alt="OptimCE logo" width="160">
</p>

# OptimCE — Signature

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/en/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![en](https://img.shields.io/badge/lang-en-43a047.svg)](README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](docs/README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](docs/README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](docs/README.nl.md)

**Signature** is the electronic signature microservice of the OptimCE platform.
Members and admins sign the PDFs the platform generated, inside the app: every
revision is sealed with the platform's certificate, and every step is kept in an
append-only evidence log that ends in a sealed evidence report.

OptimCE is an open-source platform for managing renewable energy communities,
built for the Belgian energy-sharing context. To learn more about the project,
visit [www.optimce.be](https://www.optimce.be/en/). This service is normally run
as part of the full platform: see the
[development monorepo](https://github.com/OptimCE/monorepo), which aggregates all
OptimCE services and provides the Docker Compose environment to run them
together.

## How It Works

The service is split into two deployables built from the same codebase:

- **API** (`main:app`) — a [FastAPI](https://fastapi.tiangolo.com/) HTTP
  application: the signers' inbox, the document, the sign and decline calls, and
  the follow-up for managers and admins.
- **Seal worker** (`python -m worker.main`) — the NATS consumers that prepare,
  sign and complete the PDFs (pyHanko), plus the expiry and requeue passes. It is
  the only deployable that holds the seal's private key.

A request goes through the service like this:

1. A domain (administrative documents today) publishes `signature.request.create`
   for a document it generated, to be signed by one or more members or admins, in
   parallel or in order.
2. **Prepare** — the worker flattens every form field that no signer fills, adds
   the signers' fields, certifies the document and writes revision 1. The signers
   are invited: all of them in parallel, the first one in sequence.
3. **Sign** — the signer signs from the app's "To sign" inbox, after a fresh
   login: typed or drawn, with the consent statement and the fields the domain
   declared. The API answers `202`, and the worker seals the next revision.
4. **Complete** — once everyone has signed, the worker adds the validation data
   and a document timestamp to the final file, then asks document-generation for
   the evidence report and seals it.
5. The domain hears back on `signature.event.<source>.<type>` at every step.

Every revision is sealed with the platform's certificate (PAdES-B-T) and the
final file is PAdES-B-LTA. The evidence report (PDF and JSON, sealed) records
every event of a hash-chained, append-only log.

## Legal Level

A **simple electronic signature** (eIDAS). The cryptographic signer is the
platform; each person's identity is proven by their authentication and by the
evidence log.

## NATS Contract

Bodies travel in the `Event` envelope of `core/queue/helper.py`; the models of
the requests are in `domain/payloads.py`.

| Subject | Direction | Purpose |
|---|---|---|
| `signature.request.create` | domain → service | Ask for a document to be signed; idempotent on (`source_service`, `idempotency_key`) |
| `signature.request.cancel` | domain → service | Cancel a request |
| `signature.event.<source>.<type>` | service → domain | `accepted`, `prepared`, `signer_signed`, `completed`, `declined`, `cancelled`, `expired` or `failed` |
| `signature.job.>` | internal | The worker's `prepare`, `sign` and `complete` jobs |
| `signature.dlq.>` | internal | Messages parked after their last delivery |
| `docgen.request` / `docgen.result.signature` | service ↔ document-generation | The render of the evidence report |

A request the service cannot accept (module off, document outside the domain's
prefix or missing, member unknown or without an account, bad slot) is answered
with `failed` and stores nothing.

## Repository Structure

| Path | Description |
|---|---|
| `api/` | HTTP layer — the signer and follow-up routes, the community's list, health; the gateway's headers and the step-up check |
| `service/` | Use cases shared by the API and the worker — requests, jobs, evidence report, signer actions, views, sweep |
| `crypto/` | The seal (pyHanko, pypdf) — PDF fields, appearances, signing and completion, certificates and timestamp authorities |
| `domain/` | Pure logic — the evidence chain, slots, the NATS contract, wording in four languages |
| `ports/` | Adapters to the outside world — CRM reads, NATS bus, object storage |
| `worker/` | NATS consumers, scheduler and entry point |
| `consent/` | The consent statement, by version and language; a new wording is a new version, never an edit |
| `document-templates/` | The evidence report's template, by version, rendered by document-generation |
| `devpki/` | A disposable PKI and timestamp authority for the tests and the dev stack; never in a production image |
| `shared/` | Constants, error catalogue, ORM models |
| `core/` | Cross-cutting infrastructure — config, database, queue, storage, security, middleware, i18n, tracing, metrics, logging |
| `tests/` | Test suite (pytest) |
| `locales/` | Translated API error messages (en, fr, nl, de) |
| `scripts/` | Utilities — OpenAPI export and SQL schema |

## API

In the platform these routes are reached through the gateway under
`/api/signature`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/me/requests` | The caller's signatures, `pending` or `done`, across their members and communities |
| `GET` | `/requests/{id}` | A request, its signers, and the caller's slots with the consent text and their fields |
| `GET` | `/requests/{id}/document` | The current revision; the browser hashes these bytes and sends the hash back |
| `POST` | `/requests/{id}/signers/{signerId}/view` | Record that the signer opened the document |
| `POST` | `/requests/{id}/signers/{signerId}/sign` | Sign (`202`, sealed by the worker); needs a fresh login |
| `POST` | `/requests/{id}/signers/{signerId}/decline` | Decline, with a reason |
| `GET` | `/requests/{id}/artifacts/{kind}` | Download `final`, `evidence-pdf` or `evidence-json` |
| `GET` | `/requests/{id}/events` | The evidence log in order, with its chain recomputed (follow-up) |
| `POST` | `/requests/{id}/remind` | Remind the signers whose turn it is, at most once a day (follow-up) |
| `POST` | `/requests/{id}/cancel` | Cancel the request (follow-up) |
| `GET` | `/admin/requests` | The active community's requests, newest first (managers and admins, module on) |

Follow-up routes are for the managers and admins of the request's community. A
request the caller may not see answers `404`.

Health endpoints are served under `/health`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/liveness` | Process is alive |
| `GET` | `/health/readiness` | Dependencies (CRM database, NATS) are reachable |
| `GET` | `/health/health` | Alias of readiness |

Interactive OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) are enabled only
when `ENV=local`.

### Authentication

The service performs no login of its own. In the OptimCE platform a
[KrakenD](https://www.krakend.io/) gateway and [Keycloak](https://www.keycloak.org/)
authenticate the request and inject identity headers (`x-user-id`,
`x-user-orgs`, and for this service `x-auth-time`, `x-auth-sid` and
`x-auth-acr`). The service trusts them, so it must only be reachable through the
gateway.

The sign call is a **step-up**: the caller's last interactive login must be at
most `STEP_UP_MAX_AGE_SECONDS` old (300 by default). A refreshed token keeps its
`auth_time`, so only a real browser login moves it.

## Getting Started

### Prerequisites

- Docker and Docker Compose (recommended), **or** Python 3.12 for standalone
  local development

### Running via the OptimCE Stack (recommended)

This service depends on NATS, MinIO, PostgreSQL, document-generation, and a seal
certificate with a timestamp authority. The simplest way to run it with
everything wired together is the development monorepo:

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

The stack runs `signature` (the API, on host port `8009`), `signature-worker`,
and `signature-test-pki`, a disposable test CA, TSA, OCSP and CRL (`devpki/`):

```bash
curl http://localhost:8009/health/readiness
```

### Running Standalone

```bash
git clone https://github.com/OptimCE/signature.git
cd signature
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env.local
```

Apply the local schema, then start the API and, in another terminal, the worker:

```bash
psql -d signature_local -f scripts/sql/schema.sql
uvicorn main:app --reload
python -m worker.main
```

They still need reachable NATS, MinIO, and PostgreSQL instances.

## Configuration

Settings are read from `.env.<ENV>` and the environment (`core/config.py`);
`.env.exemple` is the template for local development.

| Group | Variables |
|---|---|
| Core | `ENV`, `ALLOW_ORIGIN`, `APP_BASE_URL` |
| CRM database | `CRM_DATABASE_URL`, `CRM_DB_*` |
| Local database | `LOCAL_DATABASE_URL`, `LOCAL_DB_*` |
| Messaging | `NATS_URL`, `DOCGEN_REQUEST_SUBJECT`, `DOCGEN_RESULT_SUBJECT`, `DOCGEN_PRESIGN_TTL` |
| Storage | `STORAGE_*`, `OUTPUT_BUCKET`, `TEMPLATES_BUCKET`, `EVIDENCE_TEMPLATE_URI` |
| Seal (worker only) | `SEAL_PKCS12_PATH`, `SEAL_PKCS12_PASSPHRASE`, `SEAL_CA_CHAIN_PATHS`, `SEAL_TRUST_ROOT_PATHS`, `TSA_URLS`, `TSA_TIMEOUT_SECONDS` |
| Signing | `STEP_UP_MAX_AGE_SECONDS`, `SWEEP_ENABLED`, `SWEEP_INTERVAL_SECONDS` |
| Realtime (optional) | `REALTIME_ENABLED`, `REALTIME_REDIS_URL` |
| Observability | `LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`, `LOGGING_METRICS_URL` |

Production needs a seal certificate trusted by PDF readers (`SEAL_PKCS12_PATH`,
mounted for the worker only) and a timestamp authority on an EU trusted list
(`TSA_URLS`).

## Database Schema

`scripts/sql/schema.sql` is the source of truth for the local database and is
mirrored in `shared/models/local_models.py`. The CRM database is a separate one:
the service reads members, accounts, roles and the module from it, and writes
only notifications to it.

The local database itself enforces what makes the evidence trustworthy:

- `signature_event` and `signature_revision` are append-only: updates and deletes
  raise.
- `signature_event` is a hash chain that starts from the original document's
  SHA-256. A trigger checks its continuity on insert, and
  `domain/evidence.verify_chain` recomputes the hashes.

## Testing

The test suite uses [pytest](https://docs.pytest.org/). A PostgreSQL container is
started automatically via `pytest-docker`, and a test PKI is served on
`127.0.0.1`: every PDF the suite seals is validated against it.

```bash
pytest             # run the test suite
ruff check .       # lint
ruff format --check .
mypy .             # type checking
```

## Internationalization

API error messages (`locales/`), the consent statement (`consent/`) and the text
stamped on the documents (`domain/wording.py`) exist in **English**, **French**,
**Dutch**, and **German**. The language of API errors is selected from the
request's `Accept-Language` header; a document's language is the `locale` of its
signature request.

## Contributing

Contributions are welcome! Please read the
[contributing guidelines](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md) before opening an issue or pull request.

## Security

To report a security vulnerability, please follow the
[security policy](SECURITY.md) — do not open a public issue.

## License

This project is licensed under the [Apache License 2.0](LICENSE). Third-party
software and material are listed in [NOTICE](NOTICE): pyHanko and
pyhanko-certvalidator (MIT), pypdf (BSD-3-Clause), Pillow (HPND), and, for the
tests and the dev stack only, Certomancer (MIT).
