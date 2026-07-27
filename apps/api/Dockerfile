FROM python:3.11-slim

# - PYTHONUNBUFFERED: logs stream straight to docker logs (no buffering)
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the layer
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/ims.db

WORKDIR /app

# Install deps first so the layer is cached unless requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (see .dockerignore — venv, local DB, .env, secrets are excluded).
COPY . .

# SQLite database lives on a mounted volume so it survives container rebuilds.
RUN mkdir -p /data

EXPOSE 8080

# Container-level liveness — uses stdlib so no extra packages are needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# create_all + additive run_migrations on startup build/reconcile the schema and seed the default
# admin on a fresh DB, so an empty /data/ims.db is fine for first boot. Worker
# count is overridable via WEB_CONCURRENCY (default 1 — memory-safe; raise on a
# bigger droplet).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8080 --workers ${WEB_CONCURRENCY:-1}"]
