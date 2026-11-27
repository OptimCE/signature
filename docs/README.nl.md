<p align="center">
  <img src="logo.svg" alt="OptimCE-logo" width="160">
</p>

# OptimCE — Handtekening

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/nl/)
[![Licentie](https://img.shields.io/badge/Licentie-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-43a047.svg)](README.nl.md)

**Signature** is de microservice voor elektronische handtekeningen van het
OptimCE-platform. Leden en beheerders ondertekenen in de toepassing de pdf's die
het platform heeft aangemaakt: elke revisie wordt verzegeld met het certificaat
van het platform, en elke stap wordt bewaard in een bewijslogboek waaraan alleen
kan worden toegevoegd en dat uitmondt in een verzegeld bewijsrapport.

OptimCE is een opensourceplatform voor het beheer van
hernieuwbare-energiegemeenschappen, gebouwd voor de Belgische context van
energiedeling. Meer over het project vindt u op
[www.optimce.be](https://www.optimce.be/nl/). Deze service draait normaal gezien
als onderdeel van het volledige platform: zie de
[ontwikkelings-monorepo](https://github.com/OptimCE/monorepo), die alle
OptimCE-services samenbrengt en de Docker Compose-omgeving levert om ze samen uit
te voeren.

## Werking

De service bestaat uit twee uitvoerbare onderdelen die uit dezelfde code worden
gebouwd:

- **API** (`main:app`) — een
  [FastAPI](https://fastapi.tiangolo.com/)-HTTP-toepassing: de lijst van te
  ondertekenen documenten, het document, de aanroepen om te ondertekenen en te
  weigeren, en de opvolging voor managers en beheerders.
- **Zegelworker** (`python -m worker.main`) — de NATS-consumers die de pdf's
  voorbereiden, ondertekenen en afronden (pyHanko), plus de rondes voor verval en
  herplaatsing van taken. Het is het enige onderdeel dat de privésleutel van het
  zegel bezit.

Een aanvraag doorloopt de service als volgt:

1. Een domein (vandaag de administratieve documenten) publiceert
   `signature.request.create` voor een document dat het heeft aangemaakt, te
   ondertekenen door een of meer leden of beheerders, parallel of op volgorde.
2. **Voorbereiding** — de worker maakt elk formulierveld dat geen enkele
   ondertekenaar invult vast, voegt de velden van de ondertekenaars toe,
   certificeert het document en schrijft revisie 1. De ondertekenaars worden
   uitgenodigd: parallel allemaal, op volgorde alleen de eerste.
3. **Ondertekening** — de ondertekenaar tekent vanuit de lijst "Te ondertekenen" van de
   toepassing, na een recente aanmelding: getypt of getekend, met de
   toestemmingsverklaring en de velden die het domein heeft opgegeven. De API
   antwoordt `202` en de worker verzegelt de volgende revisie.
4. **Afronding** — zodra iedereen heeft getekend, voegt de worker de
   validatiegegevens en een documenttijdstempel toe aan het eindbestand, vraagt
   hij het bewijsrapport aan document-generation en verzegelt hij het.
5. Het domein verneemt elke stap via `signature.event.<source>.<type>`.

Elke revisie wordt verzegeld met het certificaat van het platform (PAdES-B-T) en
het eindbestand is PAdES-B-LTA. Het bewijsrapport (pdf en JSON, verzegeld) bevat
elke gebeurtenis van een logboek met hashketen waaraan alleen kan worden
toegevoegd.

## Juridisch niveau

Een **eenvoudige elektronische handtekening** (eIDAS). De cryptografische
ondertekenaar is het platform; de identiteit van elke persoon wordt bewezen door
de authenticatie en door het bewijslogboek.

## NATS-contract

De berichten reizen in de `Event`-envelop van `core/queue/helper.py`; de modellen
van de aanvragen staan in `domain/payloads.py`.

| Subject | Richting | Doel |
|---|---|---|
| `signature.request.create` | domein → service | De ondertekening van een document vragen; idempotent op (`source_service`, `idempotency_key`) |
| `signature.request.cancel` | domein → service | Een aanvraag annuleren |
| `signature.event.<source>.<type>` | service → domein | `accepted`, `prepared`, `signer_signed`, `completed`, `declined`, `cancelled`, `expired` of `failed` |
| `signature.job.>` | intern | De taken `prepare`, `sign` en `complete` van de worker |
| `signature.dlq.>` | intern | Berichten die na hun laatste levering opzij zijn gezet |
| `docgen.request` / `docgen.result.signature` | service ↔ document-generation | De weergave van het bewijsrapport |

Een aanvraag die de service niet kan aanvaarden (module uitgeschakeld, document
buiten het voorvoegsel van het domein of afwezig, lid onbekend of zonder account,
ongeldige plaats) krijgt `failed` als antwoord en er wordt niets opgeslagen.

## Repositorystructuur

| Pad | Beschrijving |
|---|---|
| `api/` | HTTP-laag — routes van de ondertekenaars en de opvolging, lijst van de gemeenschap, health; headers van de gateway en controle van de recente aanmelding |
| `service/` | Gebruiksscenario's die API en worker delen — aanvragen, taken, bewijsrapport, acties van de ondertekenaars, weergaven, opruimronde |
| `crypto/` | Het zegel (pyHanko, pypdf) — pdf-velden, weergaven, ondertekening en afronding, certificaten en tijdstempelautoriteiten |
| `domain/` | Zuivere logica — bewijsketen, plaatsen, NATS-contract, teksten in vier talen |
| `ports/` | Adapters naar buiten — leesacties op het CRM, NATS-bus, objectopslag |
| `worker/` | NATS-consumers, planner en startpunt |
| `consent/` | De toestemmingsverklaring, per versie en taal; een nieuwe tekst is een nieuwe versie, nooit een wijziging |
| `document-templates/` | De sjabloon van het bewijsrapport, per versie, weergegeven door document-generation |
| `devpki/` | Een wegwerp-PKI met tijdstempelautoriteit voor de tests en de ontwikkelstack; nooit in een productie-image |
| `shared/` | Constanten, foutencatalogus, ORM-modellen |
| `core/` | Transversale infrastructuur — configuratie, database, wachtrij, opslag, beveiliging, middleware, i18n, tracing, metrieken, logging |
| `tests/` | Testsuite (pytest) |
| `locales/` | Vertaalde API-foutmeldingen (en, fr, nl, de) |
| `scripts/` | Hulpmiddelen — OpenAPI-export en SQL-schema |

## API

In het platform zijn deze routes via de gateway bereikbaar onder
`/api/signature`.

| Methode | Pad | Doel |
|---|---|---|
| `GET` | `/me/requests` | De handtekeningen van de aanroeper, `pending` of `done`, over al zijn leden en gemeenschappen |
| `GET` | `/requests/{id}` | Een aanvraag, haar ondertekenaars en de plaatsen van de aanroeper met de toestemmingstekst en hun velden |
| `GET` | `/requests/{id}/document` | De huidige revisie; de browser hasht deze bytes en stuurt de hash terug |
| `POST` | `/requests/{id}/signers/{signerId}/view` | Vastleggen dat de ondertekenaar het document heeft geopend |
| `POST` | `/requests/{id}/signers/{signerId}/sign` | Ondertekenen (`202`, verzegeld door de worker); vereist een recente aanmelding |
| `POST` | `/requests/{id}/signers/{signerId}/decline` | Weigeren, met een reden |
| `GET` | `/requests/{id}/artifacts/{kind}` | `final`, `evidence-pdf` of `evidence-json` downloaden |
| `GET` | `/requests/{id}/events` | Het bewijslogboek op volgorde, met herberekende keten (opvolging) |
| `POST` | `/requests/{id}/remind` | De ondertekenaars die aan de beurt zijn herinneren, hoogstens één keer per dag (opvolging) |
| `POST` | `/requests/{id}/cancel` | De aanvraag annuleren (opvolging) |
| `GET` | `/admin/requests` | De aanvragen van de actieve gemeenschap, nieuwste eerst (managers en beheerders, module actief) |

De routes voor opvolging zijn voor de managers en beheerders van de gemeenschap
van de aanvraag. Een aanvraag die de aanroeper niet mag zien, antwoordt `404`.

Health-eindpunten worden aangeboden onder `/health`:

| Methode | Pad | Doel |
|---|---|---|
| `GET` | `/health/liveness` | Het proces leeft |
| `GET` | `/health/readiness` | De afhankelijkheden (CRM-database, NATS) zijn bereikbaar |
| `GET` | `/health/health` | Alias van readiness |

De interactieve OpenAPI-documentatie (`/docs`, `/redoc`, `/openapi.json`) is
alleen ingeschakeld wanneer `ENV=local`.

### Authenticatie

De service voert geen eigen aanmelding uit. In het OptimCE-platform
authenticeren een [KrakenD](https://www.krakend.io/)-gateway en
[Keycloak](https://www.keycloak.org/) het verzoek en voegen ze
identiteitsheaders toe (`x-user-id`, `x-user-orgs` en voor deze service
`x-auth-time`, `x-auth-sid` en `x-auth-acr`). De service vertrouwt ze en mag dus
alleen via de gateway bereikbaar zijn.

De aanroep om te ondertekenen is een **step-up**: de laatste interactieve
aanmelding van de aanroeper mag hoogstens `STEP_UP_MAX_AGE_SECONDS` oud zijn
(standaard 300). Een vernieuwd token behoudt zijn `auth_time`; alleen een echte
aanmelding in de browser werkt het bij.

## Aan de slag

### Vereisten

- Docker en Docker Compose (aanbevolen), **of** Python 3.12 voor zelfstandige
  lokale ontwikkeling

### Uitvoeren via de OptimCE-stack (aanbevolen)

Deze service is afhankelijk van NATS, MinIO, PostgreSQL, document-generation, en
een zegelcertificaat met een tijdstempelautoriteit. De eenvoudigste manier om hem
met al zijn afhankelijkheden uit te voeren is de ontwikkelings-monorepo:

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

De stack start `signature` (de API, op hostpoort `8009`), `signature-worker` en
`signature-test-pki`, een wegwerptestomgeving met CA, TSA, OCSP en CRL
(`devpki/`):

```bash
curl http://localhost:8009/health/readiness
```

### Zelfstandig uitvoeren

```bash
git clone https://github.com/OptimCE/signature.git
cd signature
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env.local
```

Pas het lokale schema toe en start dan de API en, in een andere terminal, de
worker:

```bash
psql -d signature_local -f scripts/sql/schema.sql
uvicorn main:app --reload
python -m worker.main
```

Ze hebben nog steeds bereikbare instanties van NATS, MinIO en PostgreSQL nodig.

## Configuratie

De instellingen worden gelezen uit `.env.<ENV>` en de omgeving
(`core/config.py`); `.env.exemple` is de sjabloon voor lokale ontwikkeling.

| Groep | Variabelen |
|---|---|
| Algemeen | `ENV`, `ALLOW_ORIGIN`, `APP_BASE_URL` |
| CRM-database | `CRM_DATABASE_URL`, `CRM_DB_*` |
| Lokale database | `LOCAL_DATABASE_URL`, `LOCAL_DB_*` |
| Berichtenverkeer | `NATS_URL`, `DOCGEN_REQUEST_SUBJECT`, `DOCGEN_RESULT_SUBJECT`, `DOCGEN_PRESIGN_TTL` |
| Opslag | `STORAGE_*`, `OUTPUT_BUCKET`, `TEMPLATES_BUCKET`, `EVIDENCE_TEMPLATE_URI` |
| Zegel (alleen worker) | `SEAL_PKCS12_PATH`, `SEAL_PKCS12_PASSPHRASE`, `SEAL_CA_CHAIN_PATHS`, `SEAL_TRUST_ROOT_PATHS`, `TSA_URLS`, `TSA_TIMEOUT_SECONDS` |
| Ondertekening | `STEP_UP_MAX_AGE_SECONDS`, `SWEEP_ENABLED`, `SWEEP_INTERVAL_SECONDS` |
| Realtime (optioneel) | `REALTIME_ENABLED`, `REALTIME_REDIS_URL` |
| Observability | `LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`, `LOGGING_METRICS_URL` |

Productie vereist een zegelcertificaat dat pdf-lezers vertrouwen
(`SEAL_PKCS12_PATH`, alleen voor de worker gekoppeld) en een
tijdstempelautoriteit op een vertrouwenslijst van de EU (`TSA_URLS`).

## Databaseschema

`scripts/sql/schema.sql` is leidend voor de lokale database en wordt gespiegeld
in `shared/models/local_models.py`. De CRM-database staat daar los van: de service
leest er leden, accounts, rollen en de module uit, en schrijft er alleen meldingen
in.

De lokale database dwingt zelf af wat het bewijs betrouwbaar maakt:

- Aan `signature_event` en `signature_revision` kan alleen worden toegevoegd:
  wijzigingen en verwijderingen geven een fout.
- `signature_event` is een hashketen die begint bij de SHA-256 van het
  oorspronkelijke document. Een trigger controleert de continuïteit bij het
  invoegen, en `domain/evidence.verify_chain` herberekent de hashes.

## Testen

De testsuite gebruikt [pytest](https://docs.pytest.org/). Een
PostgreSQL-container wordt automatisch gestart via `pytest-docker`, en een test-PKI
wordt aangeboden op `127.0.0.1`: elke pdf die de suite verzegelt, wordt ermee
gevalideerd.

```bash
pytest             # de testsuite uitvoeren
ruff check .       # linting
ruff format --check .
mypy .             # typecontrole
```

## Internationalisatie

De API-foutmeldingen (`locales/`), de toestemmingsverklaring (`consent/`) en de
teksten die op de documenten worden aangebracht (`domain/wording.py`) bestaan in
het **Engels**, **Frans**, **Nederlands** en **Duits**. De taal van de API-fouten
wordt bepaald op basis van de `Accept-Language`-header van het verzoek; de taal van
een document is de `locale` van zijn handtekeningaanvraag.

## Bijdragen

Bijdragen zijn welkom! Lees de
[bijdragerichtlijnen](../CONTRIBUTING.md) en onze
[Gedragscode](../CODE_OF_CONDUCT.md) voordat u een issue of pull request opent.

## Beveiliging

Om een beveiligingslek te melden, volg het
[beveiligingsbeleid](../SECURITY.md) — open geen openbaar issue.

## Licentie

Dit project is gelicentieerd onder de [Apache-licentie 2.0](../LICENSE). Software
en materiaal van derden staan in [NOTICE](../NOTICE): pyHanko en
pyhanko-certvalidator (MIT), pypdf (BSD-3-Clause), Pillow (HPND) en, alleen voor de
tests en de ontwikkelstack, Certomancer (MIT).
