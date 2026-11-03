# Contributing to OptimCE Signature

Thank you for your interest in contributing! Issues and pull requests are
welcome from everyone. By participating in this project, you agree to abide by
our [Code of Conduct](CODE_OF_CONDUCT.md).

This repository is the **signature** microservice of the OptimCE
platform. It tracks the regulatory dossiers and documents an energy community
must produce and submit, journals every status change, and derives the resulting
regulatory deadlines. It is one of several repositories under the
[OptimCE organization](https://github.com/OptimCE); the full platform is
assembled in the [monorepo](https://github.com/OptimCE/monorepo).

## Setting Up a Development Environment

This service depends on NATS (JetStream), an S3-compatible object store
(MinIO), and PostgreSQL. The easiest way to run it with all of its dependencies
is through the **OptimCE development stack**, which wires everything together
with Docker Compose:

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

In that stack this service runs as `signature` (see the monorepo
README for the full setup).

For working on the service code in isolation, you need **Python 3.12**:

```bash
git clone https://github.com/OptimCE/signature.git
cd signature
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env
```

Apply the local schema and reference seeds, then start the API:

```bash
psql "$LOCAL_DATABASE_URL" -f scripts/sql/schema.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0002_wal_templates.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0003_wal_templates_phase2.sql
uvicorn main:app --reload
```

It still needs reachable NATS, MinIO, and PostgreSQL instances — the monorepo
stack is the simplest way to provide them.

### A note on the schema

There is no migration runner. `scripts/sql/schema.sql` is the source of truth for
the local database and is mirrored by hand in `shared/models/local_models.py`;
when you change one, change the other and add a forward-only file under
`scripts/sql/migrations/`.

## Reporting Bugs and Suggesting Features

Open a
[GitHub issue](https://github.com/OptimCE/signature/issues).
For bugs, include what you did, what you expected, and what happened instead —
logs and reproduction steps help a lot.

For security vulnerabilities, **do not open a public issue**; follow the
[security policy](SECURITY.md) instead.

## Submitting Pull Requests

1. Fork the repository and create a feature branch from `main`.
2. Make your changes. Keep each pull request focused on a single topic.
3. Run the checks below and make sure they pass.
4. Open a pull request against `main`, describing **what** you changed and
   **why**.

### Checks Before Opening a Pull Request

These mirror the continuous integration in `.github/workflows/`:

```bash
pytest            # test suite (spins up PostgreSQL via pytest-docker)
ruff check .      # linting
ruff format --check .
mypy .            # type checking
```

Small documentation fixes are welcome as direct pull requests; for larger
changes, opening an issue first to discuss the approach can save you time.

## Commit Messages

Use short, imperative commit messages, preferably following the
[Conventional Commits](https://www.conventionalcommits.org/) style:

```
feat: add the cessation dossier type
fix: count the submission day as day zero for business-day deadlines
chore: bump workalendar to 17.0.0
docs: document the deadline sweep endpoint
```

## License

This project is licensed under the [Apache License 2.0](LICENSE). By
contributing, you agree that your contributions will be licensed under the same
license.
