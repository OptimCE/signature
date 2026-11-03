# signature — working notes for Claude

OptimCE's electronic signature annexe (FastAPI API + NATS seal worker, async
SQLAlchemy 2.0, pyHanko). Members and admins sign PDFs the platform
generated, inside the app: a platform seal on every revision (PAdES-B-T),
PAdES-B-LTA on the final file, a hash-chained evidence log and a sealed
evidence report. The spec is `docs/specs/signature.md` in the monorepo (read
its « Changes from the 01/11 draft »).

## Layout
- `api/signer/` — every signer and follow-up route (`/me/requests`,
  `/requests/{id}`, `/document`, `…/signers/{sid}/view|sign|decline`,
  `/artifacts/{kind}`, `/events`, `/remind`, `/cancel`); `api/admin/` — the
  active community's list (`/admin/requests`, MANAGER, module on);
  `api/deps.py` — the caller from the gateway's headers, the step-up check.
- `service/` — the use cases, shared by the API and the worker: `requests`
  (a domain's create/cancel), `jobs` (prepare, sign, complete), `evidence_report`
  (evidence.json, the docgen render, its result), `signer_actions`, `views`,
  `sweep` (expiry, requeue), `notify`, `log` (the evidence log), `failure`.
- `crypto/` — the seal (pyHanko, pypdf): `pdf_fields` (the preparation's
  rewrite: flatten, fields, /Lock, the signature page, anchors), `appearance`
  (filling a field in the signing increment), `seal` (prepare, sign, complete,
  seal_evidence, inspect), `pki` (the seal, the TSA list, validation context).
- `domain/` — pure: `evidence` (canonical JSON, hash, `verify_chain`), `slots`,
  `payloads` (the NATS contract), `wording` (consent, reasons, stamps; 4 locales).
- `ports/` — `crm_read` (raw SELECTs on the CRM), `bus` (NATS, + RecordingBus),
  `storage` (S3, + MemoryStore).
- `worker/` — `dispatcher` (3 durables, the ack/nak/DLQ matrix), `scheduler`, `main`.
- `devpki/` — a disposable PKI + TSA/OCSP/CRL (Certomancer) for the tests and
  the dev stack's `signature-test-pki`; never in a production image.
- `consent/<version>/<locale>.json` — the statement per capacity; a new wording
  is a new version folder, never an edit (its digest is in the evidence). New
  requests take `CONSENT_VERSION` (v2 since 11-03: v1's three sentences plus
  `ON_BEHALF`, « au nom et pour le compte de … », for another account linked to
  the member). `signer_actions.consent_for` picks the sentence, the same on the
  screen and at the sign call; a v1 request keeps « en mon nom propre ».
- `document-templates/signature/evidence/v2/` — the evidence report (docgen
  jinja-html). A change is a new version folder (docgen caches by URI);
  v2 (11-03) added « Signé par » (`signed_by_name`). v1 stays for old URIs.

## The flow
1. A domain publishes `signature.request.create` (Event envelope, `domain/payloads.py`).
   `handle_create` refuses what a domain can get wrong (module off, document
   outside its prefix or missing, member unknown or without an account, admin
   slot not an ADMIN, bad slot): `failed` event, nothing stored. Same
   `(source_service, idempotency_key)` → the same request, `accepted` again.
2. `run_prepare`: flatten every widget no signer fills (see below), add the
   signers' fields and /Lock, certify (DocMDP 2), revision 1; invite (all in
   PARALLEL, the first in SEQUENTIAL).
3. The sign call (API): step-up, authorization NOW (linked to the member, or
   still ADMIN), the browser's hash ∈ this request's revisions ≥ 1, consent,
   fields, the PNG re-encoded; the signer's name frozen (`signed_by_name`: the
   member's own name when its account signs, else the account's); slot →
   SIGNING; job queued; 202.
4. `run_sign`: fill + sign in one increment, revision n+1; next signer invited
   or COMPLETING. The stamp, /Name and /Reason name that person, « pour
   <member> » when they sign for someone else (`wording.signed_for`), never
   the slot's `display_name` alone (the member: « ACME pour ACME »).
5. `run_complete`: DSS for every signature + document timestamp (B-LTA),
   `final.pdf`; `evidence.json` (after `verify_chain`); docgen renders the
   report; `handle_docgen_result` seals it (B-LT), COMPLETED, `completed` event.

## Invariants (DB-enforced)
- `signature_event` and `signature_revision` are append-only (`forbid_mutation`).
- The event chain: `hash = sha256(prev_hash || canonical JSON)`, first from the
  original's SHA-256; a trigger checks seq + prev_hash continuity on insert,
  `domain/evidence.verify_chain` recomputes (only Python can: jsonb reorders).
  Append only through `service/log.append_event`, holding the request row lock.
- Multi-branch CHECKs are wrapped in `COALESCE(…, FALSE)` (a NULL CHECK passes).

## Traps (found the hard way)
- **DocMDP 2 lets anyone fill ANY field after certification**, so docgen's
  pre-filled values would stay editable; a FieldMDP lock on the certification
  gave pyHanko false positives on the CWaPE form. Hence the flattening.
- docgen's `pdf-form` output sets `NeedAppearances`; preparation turns it off
  (a signed file whose viewer regenerates appearances shows warnings).
- Appearances are drawn with our own Helvetica and normalised rectangles:
  WeasyPrint's subset fonts and reversed rects drew nothing.
- Field partial names may not contain periods (`OptimCE-Signer-1-place`).
- pyHanko's sync API runs its own event loop: call `crypto` through
  `asyncio.to_thread`, never from a coroutine (tests too).
- `require_feature` scopes its read with the id `resolve_internal_community`
  sets: list them in that order.
- The worker image has no fastapi and no `api/`; the API image has no pyHanko
  and no `crypto/` (`tests/test_worker_import_graph.py` pins both).
- `updated_at` is a trigger's: to test "an hour later", move `now`, not the column.

## Conventions
- Ids are the CRM's INTs (`id_community`, `id_member`, `app_user.id`); a user
  is also their Keycloak `sub`. Cross-DB refs are plain columns, never FKs.
- Errors 2500–2599, all four locales, message ends with `(code: N)`.
- Every publish happens after the commit that decided it; the requeue pass
  (`service/sweep.py`) recovers a lost one. CRM notifications commit before
  the local transaction (email deduped by `data`).
- Routes modules must NOT `from __future__ import annotations`.

## Verify
`cd signature` then `ENV=test DOCKER_CONTEXT=desktop-linux .venv/Scripts/python.exe -m pytest -q`
(Postgres on 5433 via pytest-docker; a test PKI on 127.0.0.1) ·
`.venv/Scripts/python.exe -m ruff check .` · `... -m ruff format --check .` ·
`... -m mypy .` (tests included, as CI).
