import os
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.schema import CreateColumn

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


# ── Additive schema upgrades ─────────────────────────────────────────────────


class UnsafeSchemaMigration(RuntimeError):
    """Raised when a missing column cannot be added without a data decision."""


_CATALOGUE_TABLE_RENAMES = (
    ("catalogue_raw_observations", "catalogue_extracted_evidence"),
    ("catalogue_staging_items", "catalogue_normalized_rows"),
    ("catalogue_staging_raw_observations", "catalogue_normalized_row_evidence"),
)


def run_pre_create_migrations(bind) -> None:
    """Preserve data across the known pipeline terminology table rename.

    This narrow migration must run before ``create_all`` can create the target
    tables. A source table is renamed only when its target does not exist. If
    both exist, the database may contain two histories and startup stops for an
    operator-directed merge instead of selecting or deleting either one.
    """

    existing = set(inspect(bind).get_table_names())
    preparer = bind.dialect.identifier_preparer
    for old_name, new_name in _CATALOGUE_TABLE_RENAMES:
        if old_name in existing and new_name in existing:
            raise UnsafeSchemaMigration(
                f"Both superseded table {old_name} and current table {new_name} exist; "
                "merge them with an explicit migration before startup"
            )

    with bind.begin() as connection:
        for old_name, new_name in _CATALOGUE_TABLE_RENAMES:
            if old_name not in existing:
                continue
            connection.execute(
                text(
                    f"ALTER TABLE {preparer.quote(old_name)} "
                    f"RENAME TO {preparer.quote(new_name)}"
                )
            )
            existing.remove(old_name)
            existing.add(new_name)

        # The interpreted payload was renamed with its table. This is a
        # terminology-only migration; values are preserved byte-for-byte.
        if "catalogue_normalized_rows" in existing:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("catalogue_normalized_rows")
            }
            if "proposed_fields_json" in columns and "normalized_fields_json" in columns:
                raise UnsafeSchemaMigration(
                    "Both proposed_fields_json and normalized_fields_json exist on "
                    "catalogue_normalized_rows; an explicit merge is required"
                )
            if "proposed_fields_json" in columns:
                connection.execute(
                    text(
                        "ALTER TABLE catalogue_normalized_rows "
                        "RENAME COLUMN proposed_fields_json TO normalized_fields_json"
                    )
                )


def run_migrations(bind) -> None:
    """Bring existing tables forward without deleting or rewriting user data.

    ``create_all`` creates new tables but deliberately does not alter tables
    that already exist. This additive reconciler fills that gap for columns and
    indexes represented by the current SQLAlchemy metadata. It never drops,
    renames, truncates, or rebuilds a table. A missing required column without
    a server default is safe on an empty table; on a populated table it stops
    startup with an actionable error rather than guessing a backfill.

    Structural/destructive changes must be implemented as explicit, reviewed
    migrations outside application startup.
    """

    # Callers such as tests and maintenance scripts may import database without
    # importing the model registry first.
    import models  # noqa: F401

    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    preparer = bind.dialect.identifier_preparer

    with bind.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
            table_has_rows: bool | None = None
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                if not column.nullable and column.server_default is None:
                    if table_has_rows is None:
                        table_has_rows = connection.execute(
                            text(f"SELECT 1 FROM {preparer.quote(table.name)} LIMIT 1")
                        ).first() is not None
                    if table_has_rows:
                        raise UnsafeSchemaMigration(
                            f"Cannot add required column {table.name}.{column.name} "
                            "to a populated table without an explicit backfill"
                        )
                definition = str(CreateColumn(column).compile(dialect=bind.dialect))
                connection.execute(
                    text(
                        f"ALTER TABLE {preparer.quote(table.name)} "
                        f"ADD COLUMN {definition}"
                    )
                )

    # Index creation is idempotent and additive. Run it after all missing
    # columns exist so composite indexes can be created safely.
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        for index in table.indexes:
            index.create(bind=bind, checkfirst=True)


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
