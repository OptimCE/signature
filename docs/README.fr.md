<p align="center">
  <img src="logo.svg" alt="Logo OptimCE" width="160">
</p>

# OptimCE — Signature

[![Site web](https://img.shields.io/badge/Site%20web-optimce.be-2e7d32.svg)](https://www.optimce.be)
[![Licence](https://img.shields.io/badge/Licence-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-43a047.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

**Signature** est le microservice de signature électronique de la plateforme
OptimCE. Les membres et les administrateurs signent dans l'application les PDF
que la plateforme a générés : chaque révision est scellée avec le certificat de
la plateforme, et chaque étape est conservée dans un journal de preuve en ajout
seul, qui aboutit à un rapport de preuve scellé.

OptimCE est une plateforme open source de gestion des communautés d'énergie
renouvelable, conçue pour le contexte belge du partage d'énergie. Pour en savoir
plus sur le projet, consultez [www.optimce.be](https://www.optimce.be). Ce
service est normalement exécuté au sein de la plateforme complète : voir le
[monorepo de développement](https://github.com/OptimCE/monorepo), qui agrège
tous les services OptimCE et fournit l'environnement Docker Compose pour les
exécuter ensemble.

## Fonctionnement

Le service se compose de deux exécutables construits à partir du même code :

- **API** (`main:app`) — une application HTTP
  [FastAPI](https://fastapi.tiangolo.com/) : la liste des documents à signer, le
  document, les appels de signature et de refus, et le suivi pour les
  gestionnaires et les administrateurs.
- **Worker de scellement** (`python -m worker.main`) — les consommateurs NATS qui
  préparent, signent et finalisent les PDF (pyHanko), ainsi que les passes
  d'expiration et de relance des tâches. C'est le seul exécutable qui détient la
  clé privée du sceau.

Une demande traverse le service ainsi :

1. Un domaine (aujourd'hui les documents administratifs) publie
   `signature.request.create` pour un document qu'il a généré, à faire signer par
   un ou plusieurs membres ou administrateurs, en parallèle ou dans l'ordre.
2. **Préparation** — le worker aplatit tous les champs de formulaire qu'aucun
   signataire ne remplit, ajoute les champs des signataires, certifie le document
   et écrit la révision 1. Les signataires sont invités : tous en parallèle, le
   premier seulement en séquence.
3. **Signature** — le signataire signe depuis la liste « À signer » de
   l'application, après une connexion récente : nom tapé ou signature dessinée,
   avec la déclaration de consentement et les champs déclarés par le domaine.
   L'API répond `202` et le worker scelle la révision suivante.
4. **Finalisation** — quand tout le monde a signé, le worker ajoute au fichier
   final les données de validation et un horodatage du document, puis demande le
   rapport de preuve à document-generation et le scelle.
5. Le domaine reçoit chaque étape sur `signature.event.<source>.<type>`.

Chaque révision est scellée avec le certificat de la plateforme (PAdES-B-T) et le
fichier final est en PAdES-B-LTA. Le rapport de preuve (PDF et JSON, scellé)
reprend chaque événement d'un journal en ajout seul, chaîné par hachage.

## Niveau juridique

Une **signature électronique simple** (eIDAS). Le signataire cryptographique est
la plateforme ; l'identité de chaque personne est prouvée par son
authentification et par le journal de preuve.

## Contrat NATS

Les corps voyagent dans l'enveloppe `Event` de `core/queue/helper.py` ; les
modèles des demandes sont dans `domain/payloads.py`.

| Sujet | Sens | Objet |
|---|---|---|
| `signature.request.create` | domaine → service | Demander la signature d'un document ; idempotent sur (`source_service`, `idempotency_key`) |
| `signature.request.cancel` | domaine → service | Annuler une demande |
| `signature.event.<source>.<type>` | service → domaine | `accepted`, `prepared`, `signer_signed`, `completed`, `declined`, `cancelled`, `expired` ou `failed` |
| `signature.job.>` | interne | Les tâches `prepare`, `sign` et `complete` du worker |
| `signature.dlq.>` | interne | Les messages mis de côté après leur dernière livraison |
| `docgen.request` / `docgen.result.signature` | service ↔ document-generation | Le rendu du rapport de preuve |

Une demande que le service ne peut pas accepter (module désactivé, document hors
du préfixe du domaine ou absent, membre inconnu ou sans compte, emplacement
invalide) reçoit `failed` et rien n'est enregistré.

## Structure du dépôt

| Chemin | Description |
|---|---|
| `api/` | Couche HTTP — routes des signataires et du suivi, liste de la communauté, santé ; en-têtes de la passerelle et contrôle de la connexion récente |
| `service/` | Cas d'usage partagés par l'API et le worker — demandes, tâches, rapport de preuve, actions des signataires, vues, balayage |
| `crypto/` | Le sceau (pyHanko, pypdf) — champs PDF, apparences, signature et finalisation, certificats et autorités d'horodatage |
| `domain/` | Logique pure — chaîne de preuve, emplacements, contrat NATS, textes en quatre langues |
| `ports/` | Adaptateurs vers l'extérieur — lectures du CRM, bus NATS, stockage objet |
| `worker/` | Consommateurs NATS, planificateur et point d'entrée |
| `consent/` | La déclaration de consentement, par version et par langue ; un nouveau texte est une nouvelle version, jamais une modification |
| `document-templates/` | Le modèle du rapport de preuve, par version, rendu par document-generation |
| `devpki/` | Une PKI et une autorité d'horodatage jetables pour les tests et la stack de développement ; jamais dans une image de production |
| `shared/` | Constantes, catalogue d'erreurs, modèles ORM |
| `core/` | Infrastructure transversale — configuration, base de données, file d'attente, stockage, sécurité, middleware, i18n, traçage, métriques, journalisation |
| `tests/` | Suite de tests (pytest) |
| `locales/` | Messages d'erreur de l'API traduits (en, fr, nl, de) |
| `scripts/` | Utilitaires — export OpenAPI et schéma SQL |

## API

Dans la plateforme, ces routes sont servies par la passerelle sous
`/api/signature`.

| Méthode | Chemin | Objet |
|---|---|---|
| `GET` | `/me/requests` | Les signatures de l'appelant, `pending` ou `done`, pour tous ses membres et toutes ses communautés |
| `GET` | `/requests/{id}` | Une demande, ses signataires, et les emplacements de l'appelant avec le texte de consentement et leurs champs |
| `GET` | `/requests/{id}/document` | La révision en cours ; le navigateur hache ces octets et renvoie l'empreinte |
| `POST` | `/requests/{id}/signers/{signerId}/view` | Noter que le signataire a ouvert le document |
| `POST` | `/requests/{id}/signers/{signerId}/sign` | Signer (`202`, scellé par le worker) ; exige une connexion récente |
| `POST` | `/requests/{id}/signers/{signerId}/decline` | Refuser, avec un motif |
| `GET` | `/requests/{id}/artifacts/{kind}` | Télécharger `final`, `evidence-pdf` ou `evidence-json` |
| `GET` | `/requests/{id}/events` | Le journal de preuve dans l'ordre, chaîne recalculée (suivi) |
| `POST` | `/requests/{id}/remind` | Relancer les signataires dont c'est le tour, au plus une fois par jour (suivi) |
| `POST` | `/requests/{id}/cancel` | Annuler la demande (suivi) |
| `GET` | `/admin/requests` | Les demandes de la communauté active, les plus récentes d'abord (gestionnaires et administrateurs, module activé) |

Les routes de suivi sont réservées aux gestionnaires et administrateurs de la
communauté de la demande. Une demande que l'appelant ne peut pas voir répond
`404`.

Les points d'accès de santé sont servis sous `/health` :

| Méthode | Chemin | Objet |
|---|---|---|
| `GET` | `/health/liveness` | Le processus est vivant |
| `GET` | `/health/readiness` | Les dépendances (base CRM, NATS) sont accessibles |
| `GET` | `/health/health` | Alias de readiness |

La documentation OpenAPI interactive (`/docs`, `/redoc`, `/openapi.json`) n'est
activée que lorsque `ENV=local`.

### Authentification

Le service n'effectue aucune connexion propre. Dans la plateforme OptimCE, une
passerelle [KrakenD](https://www.krakend.io/) et
[Keycloak](https://www.keycloak.org/) authentifient la requête et injectent des
en-têtes d'identité (`x-user-id`, `x-user-orgs` et, pour ce service,
`x-auth-time`, `x-auth-sid` et `x-auth-acr`). Le service leur fait confiance : il
ne doit être accessible que par la passerelle.

L'appel de signature est un **step-up** : la dernière connexion interactive de
l'appelant doit dater d'au plus `STEP_UP_MAX_AGE_SECONDS` (300 par défaut). Un
jeton rafraîchi garde son `auth_time` : seule une vraie connexion dans le
navigateur le met à jour.

## Démarrage

### Prérequis

- Docker et Docker Compose (recommandé), **ou** Python 3.12 pour un
  développement local autonome

### Exécution via la stack OptimCE (recommandé)

Ce service dépend de NATS, MinIO, PostgreSQL, document-generation, et d'un
certificat de sceau avec une autorité d'horodatage. Le plus simple pour
l'exécuter avec toutes ses dépendances est le monorepo de développement :

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

La stack lance `signature` (l'API, sur le port hôte `8009`), `signature-worker`
et `signature-test-pki`, une autorité de certification, d'horodatage, OCSP et CRL
de test, jetable (`devpki/`) :

```bash
curl http://localhost:8009/health/readiness
```

### Exécution autonome

```bash
git clone https://github.com/OptimCE/signature.git
cd signature
python -m venv .venv
# Windows : .venv\Scripts\activate  |  Unix : source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env.local
```

Appliquez le schéma local, puis démarrez l'API et, dans un autre terminal, le
worker :

```bash
psql -d signature_local -f scripts/sql/schema.sql
uvicorn main:app --reload
python -m worker.main
```

Ils ont toujours besoin d'instances accessibles de NATS, MinIO et PostgreSQL.

## Configuration

Les réglages sont lus depuis `.env.<ENV>` et l'environnement (`core/config.py`) ;
`.env.exemple` est le modèle pour le développement local.

| Groupe | Variables |
|---|---|
| Général | `ENV`, `ALLOW_ORIGIN`, `APP_BASE_URL` |
| Base de données CRM | `CRM_DATABASE_URL`, `CRM_DB_*` |
| Base de données locale | `LOCAL_DATABASE_URL`, `LOCAL_DB_*` |
| Messagerie | `NATS_URL`, `DOCGEN_REQUEST_SUBJECT`, `DOCGEN_RESULT_SUBJECT`, `DOCGEN_PRESIGN_TTL` |
| Stockage | `STORAGE_*`, `OUTPUT_BUCKET`, `TEMPLATES_BUCKET`, `EVIDENCE_TEMPLATE_URI` |
| Sceau (worker seul) | `SEAL_PKCS12_PATH`, `SEAL_PKCS12_PASSPHRASE`, `SEAL_CA_CHAIN_PATHS`, `SEAL_TRUST_ROOT_PATHS`, `TSA_URLS`, `TSA_TIMEOUT_SECONDS` |
| Signature | `STEP_UP_MAX_AGE_SECONDS`, `SWEEP_ENABLED`, `SWEEP_INTERVAL_SECONDS` |
| Temps réel (facultatif) | `REALTIME_ENABLED`, `REALTIME_REDIS_URL` |
| Observabilité | `LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`, `LOGGING_METRICS_URL` |

La production exige un certificat de sceau reconnu par les lecteurs PDF
(`SEAL_PKCS12_PATH`, monté pour le worker seul) et une autorité d'horodatage
figurant sur une liste de confiance de l'UE (`TSA_URLS`).

## Schéma de base de données

`scripts/sql/schema.sql` fait foi pour la base locale et se reflète dans
`shared/models/local_models.py`. La base du CRM est distincte : le service y lit
les membres, les comptes, les rôles et le module, et n'y écrit que des
notifications.

La base locale impose elle-même ce qui rend la preuve fiable :

- `signature_event` et `signature_revision` sont en ajout seul : toute mise à
  jour ou suppression lève une erreur.
- `signature_event` est une chaîne de hachage qui part du SHA-256 du document
  d'origine. Un déclencheur vérifie sa continuité à l'insertion, et
  `domain/evidence.verify_chain` recalcule les empreintes.

## Tests

La suite de tests utilise [pytest](https://docs.pytest.org/). Un conteneur
PostgreSQL est démarré automatiquement via `pytest-docker`, et une PKI de test est
servie sur `127.0.0.1` : chaque PDF scellé par la suite est validé avec elle.

```bash
pytest             # exécuter la suite de tests
ruff check .       # linting
ruff format --check .
mypy .             # vérification des types
```

## Internationalisation

Les messages d'erreur de l'API (`locales/`), la déclaration de consentement
(`consent/`) et les textes apposés sur les documents (`domain/wording.py`)
existent en **anglais**, **français**, **néerlandais** et **allemand**. La langue
des erreurs de l'API est choisie à partir de l'en-tête `Accept-Language` de la
requête ; celle d'un document est la `locale` de sa demande de signature.

## Contribuer

Les contributions sont les bienvenues ! Veuillez lire les
[directives de contribution](../CONTRIBUTING.md) et notre
[Code de conduite](../CODE_OF_CONDUCT.md) avant d'ouvrir une issue ou une pull
request.

## Sécurité

Pour signaler une faille de sécurité, veuillez suivre la
[politique de sécurité](../SECURITY.md) — n'ouvrez pas d'issue publique.

## Licence

Ce projet est distribué sous la [licence Apache 2.0](../LICENSE). Les logiciels
et documents tiers sont listés dans [NOTICE](../NOTICE) : pyHanko et
pyhanko-certvalidator (MIT), pypdf (BSD-3-Clause), Pillow (HPND) et, pour les
tests et la stack de développement seulement, Certomancer (MIT).
