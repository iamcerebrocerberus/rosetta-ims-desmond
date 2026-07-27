# Rosetta IMS — Backend

FastAPI + SQLAlchemy + SQLite/Postgres-ready backend for the Rosetta Inventory Management System.

Lives at `apps/api/` in the repo. The frontend lives at `apps/web/` and talks to this API over HTTP — see [`../web/src/lib/api.ts`](../web/src/lib/api.ts).

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) | Versioned OpenAPI/Swagger at `/v1/docs` |
| ORM | [SQLAlchemy 2.x](https://www.sqlalchemy.org/) | Declarative models in `models.py` |
| DB (dev) | SQLite | File at `apps/api/ims.db` (gitignored) |
| DB (prod) | SQLite on the droplet today; Postgres-ready | Set `DATABASE_URL` env var to cut over |
| Auth | JWT (HS256) + legacy API key gate | `auth.py` router, middleware in `main.py` |
| OCR / extraction | Claude Haiku via Anthropic SDK | `services/extraction_service.py` |
| Catalogue orchestration | Prefect 3.3.x | `orchestration/catalogue_flows.py`; worker is an optional Docker Compose profile |
| Deployment | DigitalOcean droplet | Docker Compose + Caddy; GitHub Actions syncs `apps/api/` and restarts the containers |

---

## Local development

### Prerequisites
- Python 3.13+ (project tested on 3.14)
- Recommended: use a local venv at `apps/api/venv/`

### Setup

```powershell
cd apps/api
python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows PowerShell
# OR: source venv/bin/activate (macOS/Linux)
pip install -r requirements.txt
```

### Run

```powershell
.\venv\Scripts\python -m uvicorn main:app --reload --port 8001
```

API v1 is now at `http://localhost:8001/v1`. Swagger UI at `http://localhost:8001/v1/docs`.

### Run queued catalogue ingestion locally

The v2 catalogue submission endpoint only creates a durable queued run. To have
a local worker claim queued runs and execute the machine ingestion stages:

```bash
cd apps/api
python -m orchestration.catalogue_dispatch --batch-size 10
python -m orchestration.catalogue_dispatch --loop --interval-seconds 30 --batch-size 10
```

The Prefect path stops at pending human review. It does not approve, apply
supplier commercial state, publish serving snapshots, or replace the v1
synchronous import route.

### Run both backend + frontend together

From the **project root**:

```powershell
.\start.ps1
```

Starts the backend on `:8001` and the frontend on `:3001`.

---

## Environment variables

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | optional | `sqlite:///./ims.db` | SQLAlchemy connection string. Set to Postgres URL in prod. |
| `ALLOWED_ORIGINS` | optional | `http://localhost:3001,http://localhost:3000` | Comma-separated CORS allowlist |
| `IMS_API_KEY` | optional | (empty) | Legacy API key gate. If unset, only JWT auth is required. If set, requests must include either `X-API-Key: <key>` or a Bearer JWT. |
| `JWT_SECRET` | yes (prod) | `dev-only-secret-change-me` | HS256 signing key for JWT tokens (see `routers/auth.py`) |
| `ANTHROPIC_API_KEY` | yes for OCR | (empty) | Used by `services/extraction_service.py` to call Claude Haiku |
| `CATALOGUE_UPLOAD_DIR` | optional | `/data/catalogue_uploads` | Durable upload root used by v2 catalogue submission and Prefect source verification |
| `CATALOGUE_SUBMISSION_MAX_BYTES` | optional | `26214400` | Maximum upload size accepted by the v2 catalogue submission endpoint |
| `CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES` | optional | `26214400` | Maximum source size the Prefect orchestration loader will verify/read |
| `CATALOGUE_DISPATCH_BATCH_SIZE` | optional | `10` | Bounded queued-run batch size for the catalogue worker profile |
| `CATALOGUE_DISPATCH_INTERVAL_SECONDS` | optional | `30` | Sleep interval for the catalogue worker dispatcher loop |
| `PREFECT_API_URL` | optional | `http://prefect-server:4200/api` in Docker | Prefect API URL used by the catalogue worker |
| `PREFECT_UI_BIND` | optional | `127.0.0.1` | Host interface for the Prefect UI port. Use `0.0.0.0` only behind a private network or authenticated proxy |
| `PREFECT_UI_PORT` | optional | `4200` | Host port for the Prefect UI |
| `PREFECT_UI_API_URL` | optional | `http://localhost:4200/api` | Browser-visible Prefect API URL used by the UI |
| `RESEND_API_KEY` | yes for access-request emails | (empty) | Used by `services/email_service.py` to send /tech-stack access-request emails. Free tier at [resend.com](https://resend.com) gives 100 emails/day. If unset, requests are still recorded in the DB; only the email is skipped. |
| `EMAIL_FROM` | optional | `Rosetta IMS <onboarding@resend.dev>` | Sender for transactional emails. Switch to a verified `algogroup.io` sender once DNS is configured on Resend. |
| `ADMIN_EMAIL` | optional | `chris@algogroup.io` | Who receives the /tech-stack access-request emails. Requestor is cc'd. |

Use `.env.local` or `.env` files (gitignored) for local secrets. In production,
runtime values live in `/root/rosetta-ims/backend/.env` on the DigitalOcean
droplet and are deliberately preserved by the deploy workflow.

```bash
ssh root@178.128.127.5
cd /root/rosetta-ims/backend
nano .env
docker compose up -d --build api caddy prefect-server
```

To run the catalogue dispatcher on the droplet and view Prefect locally:

```bash
docker compose --profile catalogue-worker up -d catalogue-worker
docker compose logs -f catalogue-worker
# Prefect UI: http://localhost:4200 when running locally, or SSH tunnel the
# droplet's localhost-bound port before opening it from a browser.
```

---

## Database

### Schema
See [`SCHEMA.md`](./SCHEMA.md) for the full ER diagram and table-by-table notes.

### Migrations
Additive schema reconciliation lives in `database.py` → `run_migrations()`.
It runs automatically on every app start via `main.py`:

```python
models.Base.metadata.create_all(bind=database.engine)
database.run_migrations(database.engine)
```

The reconciler is idempotent and additive: it creates model-declared columns
and indexes that are missing from existing tables. It never drops, truncates,
renames, or rebuilds a table. If a populated table needs a required column
without a safe server default, startup stops with an actionable error rather
than inventing a backfill. Structural or destructive changes require an
explicit, reviewed migration outside application startup.

**To add a new column or table:**
1. Update the SQLAlchemy model in `models.py` for current v1 runtime tables, or `apps/api/v2/models/` for additive v2 foundations
2. For a nullable/defaulted additive field, `run_migrations()` will reconcile
   it automatically. For required fields or structural changes, add an
   explicit reviewed migration and backfill.
3. Restart the API — migration runs on next startup

For complex migrations (renames, data backfills), promote to [Alembic](https://alembic.sqlalchemy.org/). Not needed yet.

### Seeding
- `seed_default_users()` in `database.py` creates two default users on first run (`seph` / `team`) if the `users` table is empty
- `seed.py` and `seed_from_sheet.py` are one-off scripts for SKU data seeding from Google Sheets

---

## Project layout

```
apps/api/
├── main.py                  # FastAPI app, router wiring, CORS, auth middleware
├── database.py              # Engine, session, migrations, user seeding
├── models.py                # SQLAlchemy ORM — current v1 runtime/compatibility schema
├── v2/models/               # Additive v2 persistence foundations registered by import v2.models
├── orchestration/            # Prefect catalogue ingestion flows, tasks and queued-run dispatcher
├── dependencies.py          # FastAPI dependency injection (get_db, etc.)
├── seed.py                  # Initial SKU seed (legacy — kept for reference)
├── seed_from_sheet.py       # Pull SKUs from Google Sheet on demand
├── requirements.txt
├── Dockerfile               # API container image
├── docker-compose.yml       # api + caddy + prefect UI + optional postgres/catalogue-worker profiles
├── Caddyfile                # HTTPS reverse proxy for the droplet
├── ims.db                   # SQLite (gitignored)
│
├── routers/                 # HTTP routes, grouped by API version
│   ├── v1/                  # current production API mounted at /v1
│   │   ├── __init__.py      # registers v1 routers
│   │   ├── auth.py          # /v1/auth/login, /v1/auth/me — JWT issuance
│   │   ├── products.py      # /v1/products, /v1/products/{sku}, /v1/products/summary
│   │   ├── pricing.py       # /v1/pricing matrix endpoints
│   │   ├── suppliers.py     # /v1/suppliers CRUD
│   │   ├── catalogues.py    # /v1/catalogues OCR ingestion + review
│   │   ├── stock.py         # /v1/stock CSV import + adjustments
│   │   └── sync.py          # /v1/sync Google Sheet pull
│   └── v2/                  # auth + inventory API mounted at /v2
│       └── __init__.py      # mirrors inventory routes; adds queued catalogue submission only
│
├── services/                # Business logic — pure Python, no HTTP
│   ├── extraction_service.py    # OCR pipeline (Claude Haiku)
│   ├── catalogue_submission.py  # v2 durable submission service
│   ├── catalogue_pipeline_stages.py      # framework-neutral catalogue stage services
│   ├── catalogue_pipeline_persistence.py # Pydantic/SQLAlchemy mapping helpers
│   ├── pricing_service.py       # GP calculations, margin checks
│   ├── sheet_sync.py            # Google Sheet → IMS sync
│   └── sku_service.py           # SKU manipulation
│
└── scripts/                 # One-off jobs / utilities
    └── gen_ssot_spec_csv.py     # Generate ssot-spec.csv from spec source
```

**Pattern:** routers do HTTP. Services do logic. Models do schema. Don't mix.

---

## API contract

The frontend talks to the backend over HTTP via a single abstraction file: [`../web/src/lib/api.ts`](../web/src/lib/api.ts).

Response shapes are typed in [`../web/src/lib/types.ts`](../web/src/lib/types.ts). When you change a JSON response shape, update both:

1. The Pydantic model / dict structure in the FastAPI route handler
2. The matching TypeScript interface in `lib/types.ts`

### Auto-generating types

FastAPI exposes the current API schema at `/v1/openapi.json`, and the v2 schema at `/v2/openapi.json`. The v2 schema includes inventory/admin-support routes plus the queued catalogue submission boundary; synchronous catalogue import and reparse remain v1-only. The generated TypeScript file at [`../web/src/lib/api/generated.ts`](../web/src/lib/api/generated.ts) is a fully-typed mirror of the frontend's configured API version — checked into the repo so audit-readers can browse without running anything.

To regenerate after backend changes, **three options**:

```powershell
# Option 1 — backend running locally on :8001
pnpm types

# Option 2 — hit prod (after the next deploy makes /v1/openapi.json public)
pnpm types

# Option 3 — offline (no server, just Python + venv)
cd apps/api
.\venv\Scripts\python.exe -c "import json; from main import api_v1; print(json.dumps(api_v1.openapi()))" > openapi.json
cd ../web
npx openapi-typescript ../api/openapi.json -o src/lib/api/generated.ts
```

The hand-written [`lib/types.ts`](../web/src/lib/types.ts) and the auto-generated `generated.ts` coexist during the transition. Existing pages still import from `types.ts`; new code can import from `generated.ts` like:

```typescript
import type { components } from '@/lib/api/generated'
type Product = components['schemas']['Product']
```

Eventually `types.ts` can be deleted in favour of the generated file.

---

## Adding a new endpoint

1. Decide which API version it belongs in (`routers/v1/` for current API, `routers/v2/` for next-version contracts)
2. Add the route handler — use `Depends(get_db)` for DB session and `Depends(get_current_user)` for auth
3. If the route returns a new shape, add a Pydantic response model in the same file (FastAPI uses it for OpenAPI)
4. If created a new router file, register it in that version's `__init__.py` with `target.include_router(...)`
5. Update `apps/web/src/lib/api.ts` with a calling function and `apps/web/src/lib/types.ts` with the response type — OR re-run `pnpm types` to refresh auto types

---

## Adding a new table (e.g., `purchase_orders`)

1. Add the SQLAlchemy model to `models.py`
2. Add `CREATE TABLE IF NOT EXISTS purchase_orders (...)` to `run_migrations()` in `database.py`
3. Create a router in `routers/v1/purchase_orders.py` or `routers/v2/purchase_orders.py` with the relevant endpoints
4. Register the router in that version's `__init__.py`
5. Restart the API — migration runs

For example: when migrating the Biz Ops tab into Rosetta IMS, this is the table that holds per-PO records. It would FK to `products.id` and `suppliers.id`.

---

## Deployment

### DigitalOcean droplet (production)

The backend runs on `root@178.128.127.5` from `/root/rosetta-ims/backend`.
The public API is `https://178.128.127.5.nip.io`; Swagger is at
`https://178.128.127.5.nip.io/v1/docs`.

**Auto-deploy** is wired up via GitHub Actions:
[`/.github/workflows/deploy-api-droplet.yml`](../../.github/workflows/deploy-api-droplet.yml).
Every push to `main` that touches `apps/api/**` syncs `apps/api/` to the droplet,
then runs `docker compose up -d --build api caddy`.

Required GitHub Actions secret:

```text
DROPLET_SSH_PRIVATE_KEY
```

This is a deploy-only SSH key whose public key is installed in
`/root/.ssh/authorized_keys` on the droplet.

**Manual deploy / diagnostics:**

```bash
ssh root@178.128.127.5
cd /root/rosetta-ims/backend
docker compose up -d --build api caddy
docker compose logs -f api
docker compose ps
```

### Database in prod
- SQLite currently lives at `/root/rosetta-ims/backend/data/ims.db`
- A Postgres container profile exists in `docker-compose.yml`; cut over by setting `DATABASE_URL` and starting the `postgres` profile

### Vercel (frontend)
Frontend auto-deploys on every push to `main` via Vercel's GitHub integration. The frontend reads `VITE_API_URL` for the backend origin and appends `/v1` through its shared API config.

---

## Auth

Two auth mechanisms run in parallel for transition reasons. **JWT is the path forward.**

### JWT (recommended)
- `POST /v1/auth/login` or `/v2/auth/login` with `{username, password}` returns `{access_token, user}`
- Subsequent requests include `Authorization: Bearer <token>`
- Token validated per-endpoint via `Depends(get_current_user)` in `dependencies.py`
- Default users seeded on first run: `seph` (admin), `team` (data_entry)

### Legacy API key (transitional)
- Gated globally in the `require_api_key` middleware in `main.py`
- Skipped entirely if `IMS_API_KEY` env var is unset (dev mode)
- Pass via `X-API-Key: <key>` header

`/health`, `/v1/auth/login`, and `/v2/auth/login` are exempt from both gates. Legacy root `/auth/login` remains available as a schema-hidden compatibility alias.

---

## Frequently changed files

| When you want to... | Edit... |
|---|---|
| Add a new database column | `models.py` + `database.py` (`run_migrations`) |
| Add a new endpoint | `routers/*.py` (+ register in `main.py` if new file) |
| Change business logic | `services/*.py` |
| Change CORS or middleware | `main.py` |
| Add a new env var | `main.py` (read it with `os.environ.get`) + this README |

## What lives outside this directory

| Concern | Location |
|---|---|
| Frontend UI | `../web/` |
| API client | `../web/src/lib/api.ts` |
| TypeScript types | `../web/src/lib/types.ts` |
| Static page content (v7 spec, AM walkthrough data) | `../web/src/data/` |
| Project-wide CLAUDE.md (BMAD workflow) | `../CLAUDE.md` |
| Planning artifacts | `../_bmad-output/` |
