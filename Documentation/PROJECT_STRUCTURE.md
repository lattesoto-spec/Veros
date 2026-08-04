# Project structure

```
app.py                      Entrypoint. Vercel looks for `app` here; gunicorn runs app:app
vercel.json                 Function region (syd1) and duration
Dockerfile / fly.toml       Fly deployment, kept as a working fallback
requirements.txt

carelog/
  app.py                    Application factory, routes, CLI commands
  models.py                 SQLAlchemy models
  auth.py                   Authentication, roles, the tenant boundary
  storage.py                Object storage for retained audit evidence
  templates/                Jinja templates
  domain/                   Calculation and output building — no Flask, no request state
    care_minutes.py           daily/range statistics
    compliance.py             targets, forecasts, gap detection, CALC_VERSION
    exports.py                CSV and Excel builders
    reports.py                PDF builders
  ingestion/                Universal import
    reader.py                 CSV/Excel/ZIP -> sheets
    structured.py             JSON/JSONL/SQL/SQLite -> sheets
    inspect.py                column type inference
    fingerprint.py            structural fingerprint of an upload
    analyzer.py               asks Claude for a mapping spec
    mapping.py                executes a spec deterministically
    quality.py                data-quality warnings
    pipeline.py               read -> fingerprint -> learn/reuse -> map -> persist
    jobs.py                   background import jobs (thread or self-invoke)
  integrations/             Outbound connections to roster platforms
    registry.py, sync.py

public/                     Served by the CDN on Vercel; style.css lives here
sample_data/                Downloadable samples; test_data/ holds generated fixtures
docker-compose.yml          Local Postgres for development and tests
.env.example                Copy to .env for local development
scripts/
  setup_vercel.sh           Guided first-time deployment
  migrate_to_postgres.py    One-off rescue: reads an old SQLite file into Postgres
  seed.py                   Generates the sample CSVs
tests/
  test_mapping_engine.py    Offline: mapping specs across all 7 fixture formats
  test_ingestion.py         End-to-end, needs ANTHROPIC_API_KEY
Documentation/
```

## Why it is split this way

**`domain/` imports nothing from the web layer.** Care-minute arithmetic and
compliance rules are the part that must be auditable and testable in isolation,
so they never touch `request`, `session` or Flask at all.

**`ingestion/` is a pipeline of small stages** that each do one thing to a
uniform `Sheet` structure. Any new file format is a new reader; nothing
downstream changes. That is the product's differentiator, so it is kept
separate from everything else.

**`app.py` at the root is a three-line shim.** Vercel discovers a Flask `app`
at specific entrypoints, and the Dockerfile runs `gunicorn app:app`. Keeping
the shim means the package can be reorganized without touching either.

**Scripts and tests are out of the deployed bundle.** `.vercelignore` excludes
`scripts/`, `tests/`, `Documentation/` and every `*.db`, so none of it ships to
production.

## Things deliberately kept

- **`vero/vero.db`** — an older database holding 1,180 shifts, 85 residents and
  43 staff. It is gitignored, so it exists in no other copy. It is not
  referenced by any code. Delete it only once you are certain it is not the
  only copy of that data.
- **`caremin.db`** — empty local development database, gitignored.
- **`government_template/`** — the AN-ACC performance statement spreadsheet the
  reports are modelled on. Reference material, not code.
- **`scripts/seed.py`** — generates `sample_data/residents.csv` and
  `shifts.csv`, which the `/samples/` route serves to users who want to try the
  importer without real data.

## Removed in the reorganization

`AppSetting` and its `get_setting`/`set_setting` helpers (dead once the
Anthropic key moved to the environment), the legacy `/upload` redirect stub and
its four call sites, an unused `_parse_date` helper, and unused imports across
seven modules. `pyflakes` reports the tree clean.
