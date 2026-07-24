import os
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./ims.db")
_is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

# Connection pool sizing.
# pool_pre_ping recycles dead connections; pool_recycle avoids stale ones.
# For SQLite the database is a local file — opening a connection is cheap and WAL lets
# many readers run concurrently — so we size the pool ABOVE Starlette's sync threadpool
# (~40 workers). Since at most ~40 requests are ever in flight at once, a pool that can
# hold 100 connections can never be the bottleneck. This eliminates the
# "QueuePool limit of size 5 overflow 10 reached" timeouts that occurred when a dashboard
# request burst coincided with a slow import / AI-tagging call holding a connection.
# pool_timeout is kept short so that if the pool were ever saturated a request fails fast
# instead of hanging 30s and tying up its worker thread (which compounds the pile-up).
_pool_kwargs = dict(pool_pre_ping=True, pool_recycle=1800)
if _is_sqlite:
    _pool_kwargs.update(
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_size=50,
        max_overflow=50,   # 100 total — well above the ~40-worker request ceiling
        pool_timeout=10,
    )
engine = create_engine(SQLALCHEMY_DATABASE_URL, **_pool_kwargs)

# SQLite concurrency: WAL lets readers run alongside a writer (so a slow job like
# the sheet push no longer blocks everyone), busy_timeout makes a connection WAIT
# for a lock instead of erroring, synchronous=NORMAL is safe + fast with WAL.
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Catalogue pipeline: build fresh, drop superseded tables ──────────────────
# The catalogue pipeline is built fresh, not data-migrated. Tables superseded by
# the timeline-consistent rename (raw_observation -> extracted_evidence,
# staging_item -> interpreted_claim) are DROPPED before create_all, which then
# builds the current schema. No rename, no data preservation, no migration
# history for these tables — an explicit project choice. Guarded by existence;
# a no-op on fresh databases. Link table first so any FK dependency is gone
# before its referenced tables are dropped.
# The database schema is defined entirely by the SQLAlchemy models
# (Base.metadata.create_all). The catalogue pipeline and legacy tables are built
# fresh, not incrementally migrated — no ALTER/rename migration history is kept.
_CATEGORY_RULE_SEED = [
    ("Medicine", 0.70, "5"), ("Preventative", 0.40, "5"), ("Supplement", 0.40, "5"),
    ("Shampoo", 0.40, "4"), ("Food", 0.35, "1"), ("Not-For-Sale", 0.00, "6"),
    ("Pet Hygiene", 0.40, "4"), ("Cat Litter", 0.35, "4"), ("Others", 0.40, "7"),
]


def seed_category_rules(engine):
    """Idempotent seed of the canonical item categories, GP floors and SKU digits.

    The schema comes from the models; this only ensures the operational category
    list exists. Portable across SQLite and Postgres; never overwrites a
    human-tuned floor.
    """
    with engine.connect() as conn:
        for category, gp_floor, sku_digit in _CATEGORY_RULE_SEED:
            exists = conn.execute(
                text("SELECT 1 FROM category_rules WHERE category = :c"), {"c": category}
            ).first()
            if exists is None:
                conn.execute(
                    text(
                        "INSERT INTO category_rules "
                        "(category, gp_floor, storage_rule, channel_restriction, sku_digit) "
                        "VALUES (:c, :f, 'any', NULL, :d)"
                    ),
                    {"c": category, "f": gp_floor, "d": sku_digit},
                )
                conn.commit()


def seed_default_users(engine):
    """Create default users on first run if the table is empty."""
    from passlib.context import CryptContext
    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    now = datetime.now(timezone.utc).isoformat()

    defaults = [
        # username, display_name, password, role
        ("seph",   "Seph",        "rosetta2024", "admin"),
        ("team",   "Data Team",   "teamims24",   "data_entry"),
    ]

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if count == 0:
            for username, display_name, password, role in defaults:
                conn.execute(text(
                    "INSERT INTO users (username, display_name, password_hash, role, is_active, created_at) "
                    "VALUES (:u, :d, :p, :r, 1, :t)"
                ), {"u": username, "d": display_name, "p": pwd.hash(password), "r": role, "t": now})
            conn.commit()
