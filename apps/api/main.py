"""Rosetta IMS API — unified FastAPI app.

One canonical, unversioned API surface. The former ``/v1`` and ``/v2``
namespaces are retained only as hidden, deprecated aliases of the same
routers so pre-unification clients keep working until the frontend cutover;
they respond with a ``Deprecation`` header and will be removed afterwards.

Auto-deployed to the DigitalOcean Docker host on pushes to main that touch
apps/api/**. See .github/workflows/deploy-api-droplet.yml.
"""
import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse

import models
import database
from routers import include_routers

# Schema comes entirely from the models — build fresh, no migrations.
models.Base.metadata.create_all(bind=database.engine)
database.seed_default_users(database.engine)
database.seed_category_rules(database.engine)

# Config-driven transformation engine (Phase A): seed the registry + default config version so
# the engine reproduces the previously hard-coded formulas. Idempotent; behaviour-neutral.
from services import transform_engine
transform_engine.seed_defaults(database.engine)

DEPRECATED_ALIAS_PREFIXES = ("/v1", "/v2")

# orjson as the default serializer for every endpoint that returns a dict/list — ~5-8x faster
# than stdlib json across the whole API (catalogue queues, audit, suppliers, clients, …).
app = FastAPI(
    title="Rosetta IMS API",
    version="1.0.0",
    default_response_class=ORJSONResponse,
)
include_routers(app)

# Deprecated version aliases: the SAME routers mounted under /v1 and /v2,
# hidden from the canonical schema. Removed at frontend cutover.
alias_app = FastAPI(
    title="Rosetta IMS API (deprecated version alias)",
    version="1.0.0",
    default_response_class=ORJSONResponse,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
include_routers(alias_app, include_in_schema=False)

_default_origins = "http://localhost:3001,http://localhost:3000"
# Explicit allowlist (env-overridable); trim blanks/whitespace so a stray space can't
# silently void an entry.
_allowed = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]
# Always allow this project's managed frontend domains — Cloudflare (Workers/Pages: pages.dev,
# workers.dev) and, during the migration, the legacy Vercel domains — regardless of the
# ALLOWED_ORIGINS secret, so a missing/incorrect secret can't take prod offline. Matches e.g.
# rosetta-ims.pages.dev, rosetta-ims-*.workers.dev, rosetta-ims.vercel.app. A custom domain
# (e.g. app.algogroup.io) goes in the ALLOWED_ORIGINS env var.
_frontend_regex = r"https://rosetta-ims[a-z0-9-]*\.(pages\.dev|workers\.dev|vercel\.app)"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed,
    allow_origin_regex=_frontend_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)

_API_KEY = os.environ.get("IMS_API_KEY", "")

_PUBLIC_PATHS = {"/health", "/auth/login", "/auth/accept-invite",
                 "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def _strip_alias_prefix(path: str) -> str:
    for prefix in DEPRECATED_ALIAS_PREFIXES:
        if path == prefix:
            return "/"
        if path.startswith(f"{prefix}/"):
            return path[len(prefix):]
    return path


def _is_public_path(path: str) -> bool:
    unversioned = _strip_alias_prefix(path)
    return unversioned in _PUBLIC_PATHS or unversioned.startswith("/auth/invite/")


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    # CORS preflight (OPTIONS) must pass through without auth so browser gets CORS headers
    if request.method == "OPTIONS":
        return await call_next(request)
    # Public routes — login + invite onboarding (the invited user has no session yet)
    if _is_public_path(request.url.path):
        return await _finish(request, call_next)
    # Legacy API key gate — skipped if IMS_API_KEY not set (dev mode)
    if not _API_KEY:
        return await _finish(request, call_next)
    if request.headers.get("X-API-Key") == _API_KEY:
        return await _finish(request, call_next)
    # Also accept Bearer JWT — JWT auth is validated per-endpoint via Depends(get_current_user)
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return await _finish(request, call_next)
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


async def _finish(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(DEPRECATED_ALIAS_PREFIXES):
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = f'<{_strip_alias_prefix(request.url.path)}>; rel="successor-version"'
    return response


@app.get("/", include_in_schema=False)
def api_index():
    return {"name": "Rosetta IMS API", "docs": "/docs", "openapi": "/openapi.json"}


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "version": "1.0.0"}


for _prefix in DEPRECATED_ALIAS_PREFIXES:
    app.mount(_prefix, alias_app)
