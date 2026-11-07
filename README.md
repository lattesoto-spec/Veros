# CareMin backend

Flask JSON API for care-minute monitoring in Australian residential aged care. This repository remains connected to **lattesoto-spec/Veros** and the existing backend Vercel project. The static React application now lives in the separate sibling **caremin-frontend** repository.

## Responsibilities

- This repository owns authentication, tenancy, PostgreSQL, retained evidence, Xberg extraction, import mapping, eligibility, calculations and report generation.
- The frontend owns pages, styles, branding and interaction. It calls `/api/*` through its own server-side proxy; it has no database, storage, session or AI secrets.
- There are no server-rendered pages, Node build, frontend assets or frontend runtime dependencies here.

## Local development

Requires Python 3.12 and Docker for PostgreSQL.

```sh
docker compose up -d
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
# Set SECRET_KEY and MFA_ENCRYPTION_KEY in .env first.
python -m flask --app app init-db
python -m flask --app app bootstrap-org --name Local --admin-email you@example.com --password 'a-long-dev-password'
python app.py
```

The API listens on `http://127.0.0.1:8080`. `/healthz` is the readiness endpoint. Start the separate frontend with `npm ci` and `npm run dev` in `../caremin-frontend`, then open `http://127.0.0.1:5173`.

`python -m flask --app app seed-demo --password 'a-long-demo-password'` creates a separate deterministic Demo organisation offline. `--superuser` on `bootstrap-org` creates a platform operator; operators must explicitly enter a provider's support workspace.

## Verification

```sh
python -m pytest -q
python tests/test_mapping_engine.py
python -m pyflakes app.py build.py carelog
```

Unit tests use disposable SQLite fixtures only. PostgreSQL integration tests require `TEST_DATABASE_URL` pointing to a dedicated empty local database ending in `_test`; they never read production credentials. Production supports PostgreSQL only.

GitHub Actions runs the full suite with PostgreSQL, mapping fixtures and static checks. For the separate frontend's real-browser journey, create a disposable local PostgreSQL database named **caremin_browser_test**, then run:

```sh
CAREMIN_E2E_DATABASE_URL='postgresql+psycopg://caremin:caremin@127.0.0.1:5433/caremin_browser_test' python tests/e2e_server.py
```

This test-only server **resets that database on every start** and refuses other database names or non-local hosts. It serves the real Flask API on port 8088 with synthetic accounts, MFA and CSRF enabled. It is excluded from deployment. See the frontend README for its browser test command. Production builds remain independent of the sibling repository.

## Structure

- `app.py`, `build.py`: API entrypoint and additive deployment migrations.
- `carelog/api/`: Flask routes and explicit JSON serialization.
- `carelog/services/`: tenant-scoped workspace and evidence helpers.
- `carelog/auth.py`, `models.py`, `database.py`, `storage.py`: access, persistence and source storage.
- `carelog/domain/`: arithmetic, eligibility, resident-day rules and report builders.
- `carelog/ingestion/`: Xberg/structured extraction, format mapping, validation and atomic import jobs.
- `sample_data/`: downloadable examples and deterministic fixture generators used by tests.

See [two-repository migration](docs/repository-split.md), [deployment](docs/deployment.md), [architecture and API contract](docs/architecture.md) and [access roles](docs/access.md).

See [product changes in UI order](docs/product-changes.md) for the current monitoring, target evidence, import review and provider traceability behavior. The full feature inventory is `FEATURES.md` in the independent frontend repository.

## Scope

Only eligible actual worked time counts; rostered/unverified shifts and clinical care episodes remain separate. Resident-day ledgers are supported, with admission/discharge estimates used when ledger evidence is absent. Human review of completeness, eligibility and configured targets remains necessary. This prototype is not represented as GPMS-ready.

No email recovery, notifications, SSO, scheduled file polling, automatic retention or browser-direct large uploads are implemented. Deletion is permanent. Split-duty allocation depends on source evidence.
