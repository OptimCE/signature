<p align="center">
  <img src="logo.svg" alt="OptimCE-Logo" width="160">
</p>

# OptimCE — Signatur

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/de/)
[![Lizenz](https://img.shields.io/badge/Lizenz-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-43a047.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

**Signature** ist der Microservice für elektronische Signaturen der
OptimCE-Plattform. Mitglieder und Administratoren unterzeichnen die von der
Plattform erzeugten PDF-Dateien direkt in der Anwendung: Jede Revision wird mit
dem Zertifikat der Plattform versiegelt, und jeder Schritt wird in einem reinen
Anfüge-Protokoll festgehalten, das in einem versiegelten Nachweisbericht endet.

OptimCE ist eine Open-Source-Plattform zur Verwaltung von
Erneuerbare-Energie-Gemeinschaften, konzipiert für den belgischen Kontext des
Energieteilens. Mehr über das Projekt erfahren Sie auf
[www.optimce.be](https://www.optimce.be/de/). Dieser Dienst wird normalerweise als
Teil der Gesamtplattform betrieben: siehe das
[Entwicklungs-Monorepo](https://github.com/OptimCE/monorepo), das alle
OptimCE-Dienste bündelt und die Docker-Compose-Umgebung bereitstellt, um sie
gemeinsam auszuführen.

## Funktionsweise

Der Dienst besteht aus zwei aus demselben Code erstellten Anwendungen:

- **API** (`main:app`) — eine
  [FastAPI](https://fastapi.tiangolo.com/)-HTTP-Anwendung: die Liste der zu
  unterzeichnenden Dokumente, das Dokument, die Aufrufe zum Unterzeichnen und
  Ablehnen sowie die Nachverfolgung für Verwalter und Administratoren.
- **Siegel-Worker** (`python -m worker.main`) — die NATS-Consumer, die die
  PDF-Dateien vorbereiten, unterzeichnen und abschließen (pyHanko), sowie die
  Läufe für Ablauf und erneute Einreihung. Er ist die einzige Anwendung, die den
  privaten Schlüssel des Siegels besitzt.

Eine Anfrage durchläuft den Dienst wie folgt:

1. Eine Domäne (derzeit die Verwaltungsdokumente) veröffentlicht
   `signature.request.create` für ein von ihr erzeugtes Dokument, das von einem
   oder mehreren Mitgliedern oder Administratoren parallel oder der Reihe nach
   unterzeichnet werden soll.
2. **Vorbereitung** — der Worker reduziert alle Formularfelder, die kein
   Unterzeichner ausfüllt, auf festen Inhalt, fügt die Felder der Unterzeichner
   hinzu, zertifiziert das Dokument und schreibt Revision 1. Die Unterzeichner
   werden eingeladen: parallel alle, der Reihe nach nur der erste.
3. **Unterzeichnung** — der Unterzeichner signiert in der Liste „Zu unterschreiben“ der
   Anwendung nach einer frischen Anmeldung: getippt oder gezeichnet, mit der
   Einwilligungserklärung und den von der Domäne angegebenen Feldern. Die API
   antwortet mit `202`, und der Worker versiegelt die nächste Revision.
4. **Abschluss** — sobald alle unterzeichnet haben, ergänzt der Worker die
   endgültige Datei um die Validierungsdaten und einen Dokument-Zeitstempel,
   fordert bei document-generation den Nachweisbericht an und versiegelt ihn.
5. Die Domäne erfährt jeden Schritt über `signature.event.<source>.<type>`.

Jede Revision wird mit dem Zertifikat der Plattform versiegelt (PAdES-B-T), die
endgültige Datei ist PAdES-B-LTA. Der Nachweisbericht (PDF und JSON, versiegelt)
enthält jedes Ereignis eines reinen Anfüge-Protokolls mit Hash-Verkettung.

## Rechtliches Niveau

Eine **einfache elektronische Signatur** (eIDAS). Kryptografischer Unterzeichner
ist die Plattform; die Identität jeder Person wird durch ihre Authentifizierung
und das Nachweisprotokoll belegt.

## NATS-Vertrag

Die Nachrichten werden im `Event`-Umschlag aus `core/queue/helper.py`
übertragen; die Modelle der Anfragen stehen in `domain/payloads.py`.

| Subject | Richtung | Zweck |
|---|---|---|
| `signature.request.create` | Domäne → Dienst | Die Unterzeichnung eines Dokuments anfordern; idempotent über (`source_service`, `idempotency_key`) |
| `signature.request.cancel` | Domäne → Dienst | Eine Anfrage abbrechen |
| `signature.event.<source>.<type>` | Dienst → Domäne | `accepted`, `prepared`, `signer_signed`, `completed`, `declined`, `cancelled`, `expired` oder `failed` |
| `signature.job.>` | intern | Die Aufträge `prepare`, `sign` und `complete` des Workers |
| `signature.dlq.>` | intern | Nach der letzten Zustellung beiseitegelegte Nachrichten |
| `docgen.request` / `docgen.result.signature` | Dienst ↔ document-generation | Die Erzeugung des Nachweisberichts |

Eine Anfrage, die der Dienst nicht annehmen kann (Modul deaktiviert, Dokument
außerhalb des Präfixes der Domäne oder nicht vorhanden, Mitglied unbekannt oder
ohne Konto, ungültiger Platz), wird mit `failed` beantwortet; gespeichert wird
nichts.

## Repository-Struktur

| Pfad | Beschreibung |
|---|---|
| `api/` | HTTP-Schicht — Routen der Unterzeichner und der Nachverfolgung, Liste der Gemeinschaft, Health; Header des Gateways und Prüfung der frischen Anmeldung |
| `service/` | Von API und Worker geteilte Anwendungsfälle — Anfragen, Aufträge, Nachweisbericht, Aktionen der Unterzeichner, Ansichten, Bereinigungslauf |
| `crypto/` | Das Siegel (pyHanko, pypdf) — PDF-Felder, Darstellungen, Unterzeichnung und Abschluss, Zertifikate und Zeitstempeldienste |
| `domain/` | Reine Logik — Nachweiskette, Plätze, NATS-Vertrag, Texte in vier Sprachen |
| `ports/` | Adapter nach außen — Lesezugriffe auf das CRM, NATS-Bus, Objektspeicher |
| `worker/` | NATS-Consumer, Scheduler und Einstiegspunkt |
| `consent/` | Die Einwilligungserklärung, nach Version und Sprache; ein neuer Wortlaut ist eine neue Version, nie eine Änderung |
| `document-templates/` | Die Vorlage des Nachweisberichts, nach Version, gerendert von document-generation |
| `devpki/` | Eine Wegwerf-PKI mit Zeitstempeldienst für die Tests und den Entwicklungs-Stack; nie in einem Produktions-Image |
| `shared/` | Konstanten, Fehlerkatalog, ORM-Modelle |
| `core/` | Querschnittsinfrastruktur — Konfiguration, Datenbank, Queue, Speicher, Sicherheit, Middleware, i18n, Tracing, Metriken, Logging |
| `tests/` | Testsuite (pytest) |
| `locales/` | Übersetzte API-Fehlermeldungen (en, fr, nl, de) |
| `scripts/` | Werkzeuge — OpenAPI-Export und SQL-Schema |

## API

In der Plattform sind diese Routen über das Gateway unter `/api/signature`
erreichbar.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/me/requests` | Die Unterschriften des Aufrufers, `pending` oder `done`, über alle seine Mitglieder und Gemeinschaften |
| `GET` | `/requests/{id}` | Eine Anfrage, ihre Unterzeichner und die Plätze des Aufrufers mit Einwilligungstext und Feldern |
| `GET` | `/requests/{id}/document` | Die aktuelle Revision; der Browser hasht diese Bytes und sendet den Hash zurück |
| `POST` | `/requests/{id}/signers/{signerId}/view` | Festhalten, dass der Unterzeichner das Dokument geöffnet hat |
| `POST` | `/requests/{id}/signers/{signerId}/sign` | Unterzeichnen (`202`, vom Worker versiegelt); erfordert eine frische Anmeldung |
| `POST` | `/requests/{id}/signers/{signerId}/decline` | Mit Begründung ablehnen |
| `GET` | `/requests/{id}/artifacts/{kind}` | `final`, `evidence-pdf` oder `evidence-json` herunterladen |
| `GET` | `/requests/{id}/events` | Das Nachweisprotokoll der Reihe nach, mit neu berechneter Kette (Nachverfolgung) |
| `POST` | `/requests/{id}/remind` | Die Unterzeichner erinnern, die an der Reihe sind, höchstens einmal am Tag (Nachverfolgung) |
| `POST` | `/requests/{id}/cancel` | Die Anfrage abbrechen (Nachverfolgung) |
| `GET` | `/admin/requests` | Die Anfragen der aktiven Gemeinschaft, neueste zuerst (Verwalter und Administratoren, Modul aktiv) |

Die Routen der Nachverfolgung sind den Verwaltern und Administratoren der
Gemeinschaft der Anfrage vorbehalten. Eine Anfrage, die der Aufrufer nicht sehen
darf, antwortet mit `404`.

Health-Endpunkte werden unter `/health` bereitgestellt:

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/health/liveness` | Der Prozess läuft |
| `GET` | `/health/readiness` | Die Abhängigkeiten (CRM-Datenbank, NATS) sind erreichbar |
| `GET` | `/health/health` | Alias für readiness |

Die interaktive OpenAPI-Dokumentation (`/docs`, `/redoc`, `/openapi.json`) ist
nur aktiviert, wenn `ENV=local`.

### Authentifizierung

Der Dienst führt keine eigene Anmeldung durch. In der OptimCE-Plattform
authentifizieren ein [KrakenD](https://www.krakend.io/)-Gateway und
[Keycloak](https://www.keycloak.org/) die Anfrage und fügen Identitäts-Header
hinzu (`x-user-id`, `x-user-orgs` und für diesen Dienst `x-auth-time`,
`x-auth-sid` und `x-auth-acr`). Der Dienst vertraut ihnen; er darf daher nur über
das Gateway erreichbar sein.

Der Aufruf zum Unterzeichnen ist ein **Step-up**: Die letzte interaktive
Anmeldung des Aufrufers darf höchstens `STEP_UP_MAX_AGE_SECONDS` zurückliegen
(standardmäßig 300). Ein erneuertes Token behält sein `auth_time`; nur eine echte
Anmeldung im Browser aktualisiert es.

## Erste Schritte

### Voraussetzungen

- Docker und Docker Compose (empfohlen), **oder** Python 3.12 für eigenständige
  lokale Entwicklung

### Ausführung über den OptimCE-Stack (empfohlen)

Dieser Dienst benötigt NATS, MinIO, PostgreSQL, document-generation sowie ein
Siegelzertifikat mit einem Zeitstempeldienst. Am einfachsten führen Sie ihn mit
all seinen Abhängigkeiten über das Entwicklungs-Monorepo aus:

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

Der Stack startet `signature` (die API, auf Host-Port `8009`), `signature-worker`
und `signature-test-pki`, eine Wegwerf-Testumgebung mit CA, TSA, OCSP und CRL
(`devpki/`):

```bash
curl http://localhost:8009/health/readiness
```

### Eigenständige Ausführung

```bash
git clone https://github.com/OptimCE/signature.git
cd signature
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env.local
```

Wenden Sie das lokale Schema an und starten Sie dann die API und in einem zweiten
Terminal den Worker:

```bash
psql -d signature_local -f scripts/sql/schema.sql
uvicorn main:app --reload
python -m worker.main
```

Beide benötigen weiterhin erreichbare Instanzen von NATS, MinIO und PostgreSQL.

## Konfiguration

Die Einstellungen werden aus `.env.<ENV>` und der Umgebung gelesen
(`core/config.py`); `.env.exemple` ist die Vorlage für die lokale Entwicklung.

| Gruppe | Variablen |
|---|---|
| Allgemein | `ENV`, `ALLOW_ORIGIN`, `APP_BASE_URL` |
| CRM-Datenbank | `CRM_DATABASE_URL`, `CRM_DB_*` |
| Lokale Datenbank | `LOCAL_DATABASE_URL`, `LOCAL_DB_*` |
| Messaging | `NATS_URL`, `DOCGEN_REQUEST_SUBJECT`, `DOCGEN_RESULT_SUBJECT`, `DOCGEN_PRESIGN_TTL` |
| Speicher | `STORAGE_*`, `OUTPUT_BUCKET`, `TEMPLATES_BUCKET`, `EVIDENCE_TEMPLATE_URI` |
| Siegel (nur Worker) | `SEAL_PKCS12_PATH`, `SEAL_PKCS12_PASSPHRASE`, `SEAL_CA_CHAIN_PATHS`, `SEAL_TRUST_ROOT_PATHS`, `TSA_URLS`, `TSA_TIMEOUT_SECONDS` |
| Unterzeichnung | `STEP_UP_MAX_AGE_SECONDS`, `SWEEP_ENABLED`, `SWEEP_INTERVAL_SECONDS` |
| Echtzeit (optional) | `REALTIME_ENABLED`, `REALTIME_REDIS_URL` |
| Observability | `LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`, `LOGGING_METRICS_URL` |

Die Produktion benötigt ein Siegelzertifikat, dem PDF-Reader vertrauen
(`SEAL_PKCS12_PATH`, nur für den Worker eingebunden), und einen Zeitstempeldienst
von einer Vertrauensliste der EU (`TSA_URLS`).

## Datenbankschema

`scripts/sql/schema.sql` ist maßgeblich für die lokale Datenbank und wird in
`shared/models/local_models.py` gespiegelt. Die CRM-Datenbank ist davon getrennt:
Der Dienst liest dort Mitglieder, Konten, Rollen und das Modul und schreibt nur
Benachrichtigungen hinein.

Die lokale Datenbank erzwingt selbst, was den Nachweis vertrauenswürdig macht:

- `signature_event` und `signature_revision` sind reine Anfüge-Tabellen:
  Änderungen und Löschungen lösen einen Fehler aus.
- `signature_event` ist eine Hash-Kette, die beim SHA-256 des Originaldokuments
  beginnt. Ein Trigger prüft ihre Kontinuität beim Einfügen, und
  `domain/evidence.verify_chain` berechnet die Hashes neu.

## Tests

Die Testsuite verwendet [pytest](https://docs.pytest.org/). Ein
PostgreSQL-Container wird automatisch über `pytest-docker` gestartet, und eine
Test-PKI wird auf `127.0.0.1` bereitgestellt: Jede von der Suite versiegelte
PDF-Datei wird damit validiert.

```bash
pytest             # Testsuite ausführen
ruff check .       # Linting
ruff format --check .
mypy .             # Typprüfung
```

## Internationalisierung

Die API-Fehlermeldungen (`locales/`), die Einwilligungserklärung (`consent/`) und
die auf die Dokumente aufgebrachten Texte (`domain/wording.py`) gibt es auf
**Englisch**, **Französisch**, **Niederländisch** und **Deutsch**. Die Sprache der
API-Fehler wird aus dem `Accept-Language`-Header der Anfrage bestimmt; die Sprache
eines Dokuments ist die `locale` seiner Signaturanfrage.

## Mitwirken

Beiträge sind willkommen! Bitte lesen Sie die
[Richtlinien für Beiträge](../CONTRIBUTING.md) und unseren
[Verhaltenskodex](../CODE_OF_CONDUCT.md), bevor Sie ein Issue oder einen Pull
Request eröffnen.

## Sicherheit

Um eine Sicherheitslücke zu melden, folgen Sie bitte der
[Sicherheitsrichtlinie](../SECURITY.md) — öffnen Sie kein öffentliches Issue.

## Lizenz

Dieses Projekt steht unter der [Apache-Lizenz 2.0](../LICENSE). Software und
Material Dritter sind in [NOTICE](../NOTICE) aufgeführt: pyHanko und
pyhanko-certvalidator (MIT), pypdf (BSD-3-Clause), Pillow (HPND) und, nur für die
Tests und den Entwicklungs-Stack, Certomancer (MIT).
