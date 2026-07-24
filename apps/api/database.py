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
_SUPERSEDED_CATALOGUE_TABLES = [
    "catalogue_staging_raw_observations",
    "catalogue_raw_observations",
    "catalogue_staging_items",
]


def drop_superseded_catalogue_tables(engine):
    """Drop catalogue pipeline tables replaced by the current schema so
    create_all rebuilds them fresh. No-op when they do not exist."""
    from sqlalchemy import inspect

    existing = set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        for table in _SUPERSEDED_CATALOGUE_TABLES:
            if table in existing:
                conn.execute(text(f'DROP TABLE IF EXISTS {table}'))
                conn.commit()


# Columns added to the models AFTER a Postgres database was first created from
# create_all(). create_all() never alters existing tables, so without these the
# ORM references columns Postgres doesn't have (the exact drift that broke the
# legacy deployment on 2026-07-23). Idempotent via IF NOT EXISTS; every new
# additive column on an existing table MUST be listed here as well as in the
# SQLite stmts below.
_POSTGRES_ADDITIVE_MIGRATIONS = [
    # Raw-stage completion metadata (file-level only)
    "ALTER TABLE catalogue_source_documents ADD COLUMN IF NOT EXISTS byte_size INTEGER",
    "ALTER TABLE catalogue_source_documents ADD COLUMN IF NOT EXISTS page_count INTEGER",
    "ALTER TABLE catalogue_source_documents ADD COLUMN IF NOT EXISTS raw_stage_status TEXT",
    "ALTER TABLE catalogue_source_documents ADD COLUMN IF NOT EXISTS raw_stage_completed_at TEXT",
]


def run_migrations(engine):
    """Safe ALTER TABLE migrations for SQLite — idempotent, ignores 'column already exists'.

    On Postgres the schema is created from the models via create_all(), which
    only creates missing TABLES — so additive columns on existing tables are
    applied from _POSTGRES_ADDITIVE_MIGRATIONS before returning."""
    if not _is_sqlite:
        with engine.connect() as conn:
            for sql in _POSTGRES_ADDITIVE_MIGRATIONS:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                except Exception:
                    conn.rollback()
        return
    # Register every table (legacy + catalogue pipeline) even in tests/scripts
    # that only imported `database` before calling run_migrations().
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    stmts = [
        # User-account management: account-change + login timestamps
        "ALTER TABLE users ADD COLUMN updated_at TEXT",
        "ALTER TABLE users ADD COLUMN last_login_at TEXT",
        # Invite-by-email onboarding
        "ALTER TABLE users ADD COLUMN email TEXT",
        "ALTER TABLE users ADD COLUMN invite_token TEXT",
        "ALTER TABLE users ADD COLUMN invite_expires_at TEXT",
        "ALTER TABLE users ADD COLUMN invite_accepted_at TEXT",
        "ALTER TABLE users ADD COLUMN invited_by TEXT",
        "ALTER TABLE product_channels ADD COLUMN channel_fee_pct REAL",
        "ALTER TABLE products ADD COLUMN weight_g REAL",
        "ALTER TABLE product_suppliers ADD COLUMN catalogue_cost REAL",
        "ALTER TABLE product_suppliers ADD COLUMN daysmart_cost REAL",
        "ALTER TABLE product_suppliers ADD COLUMN cost_reconciled_at TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN units_per_pack INTEGER",
        "ALTER TABLE product_suppliers ADD COLUMN uom_verified_at TEXT",
        "ALTER TABLE product_channels ADD COLUMN units_per_listing INTEGER",
        # Story 1.5 — cost confidence columns
        "ALTER TABLE product_suppliers ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'manual'",
        "ALTER TABLE product_suppliers ADD COLUMN cost_source_ref TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN cost_updated_at TEXT",
        # UOM person stamp
        "ALTER TABLE product_suppliers ADD COLUMN uom_verified_by TEXT",
        # Sync protection shadow columns — store last Sheet value so locked IMS values survive re-syncs
        "ALTER TABLE product_suppliers ADD COLUMN basic_cost_sheet REAL",
        "ALTER TABLE product_suppliers ADD COLUMN units_per_pack_sheet INTEGER",
        # UOM split: sell unit vs buy packaging unit
        "ALTER TABLE products ADD COLUMN pack_unit TEXT",
        # Manual edit tracking — set only by human PATCH, never by Sheet sync
        "ALTER TABLE products ADD COLUMN last_manual_edit_at TEXT",
        "ALTER TABLE products ADD COLUMN last_manual_edit_by TEXT",
        # Catalogue item pack size — extracted from supplier price list
        "ALTER TABLE catalogue_items ADD COLUMN units_per_pack INTEGER",
        # Min sellable quantity (smallest unit sold, usually 1) — separate from units_per_pack
        # (the pack you buy whole) and from min_purchase_qty (the supplier MOQ in packs).
        "ALTER TABLE catalogue_items ADD COLUMN min_sellable_qty INTEGER",
        "ALTER TABLE products ADD COLUMN min_sellable_qty INTEGER",
        # Catalogue item brand + raw pack-size string (extracted but previously dropped)
        "ALTER TABLE catalogue_items ADD COLUMN brand TEXT",
        "ALTER TABLE catalogue_items ADD COLUMN pack_size TEXT",
        # Catalogue item Max Bulk Buy structured pricing — distinct from bulk_buy_tiers string
        "ALTER TABLE catalogue_items ADD COLUMN max_bulk_buy_cost REAL",
        "ALTER TABLE catalogue_items ADD COLUMN max_bulk_buy_min_qty INTEGER",
        # Pack-size provenance — mirrors cost_source so Sheet re-syncs can't overwrite an
        # OCR-extracted or manually-entered pack size. 'sheet' = one-time seed (overwritable).
        "ALTER TABLE product_suppliers ADD COLUMN pack_source TEXT NOT NULL DEFAULT 'sheet'",
        # Cost-tier split: 'catalogue' is now the human-reviewed OCR flow (top tier) and is
        # protected from Sheet re-syncs. Legacy rows seeded by the OLD sheet sync were also
        # tagged 'catalogue' but carry no 'catalogue_import:' ref — relabel them to the new
        # 'sheet' seed tier so they don't masquerade as reviewed OCR costs. Idempotent.
        "UPDATE product_suppliers SET cost_source='sheet' "
        "WHERE cost_source='catalogue' "
        "AND (cost_source_ref IS NULL OR cost_source_ref NOT LIKE 'catalogue_import:%')",
        # ── Supplier-master fields (vet / non-vet / consolidated import) ──
        "ALTER TABLE suppliers ADD COLUMN normalized_name TEXT",
        "ALTER TABLE suppliers ADD COLUMN segment TEXT",
        "ALTER TABLE suppliers ADD COLUMN type_of_brand TEXT",
        "ALTER TABLE suppliers ADD COLUMN moq_value TEXT",
        "ALTER TABLE suppliers ADD COLUMN moq_specific TEXT",
        "ALTER TABLE suppliers ADD COLUMN credit_term TEXT",
        "ALTER TABLE suppliers ADD COLUMN monthly_rebate TEXT",
        "ALTER TABLE suppliers ADD COLUMN bulk_buy_structure TEXT",
        "ALTER TABLE suppliers ADD COLUMN delivery_time TEXT",
        "ALTER TABLE suppliers ADD COLUMN delivery_charges TEXT",
        "ALTER TABLE suppliers ADD COLUMN warehouse_pickup TEXT",
        "ALTER TABLE suppliers ADD COLUMN order_days TEXT",
        "ALTER TABLE suppliers ADD COLUMN delivery_days TEXT",
        "ALTER TABLE suppliers ADD COLUMN cut_off_time TEXT",
        "ALTER TABLE suppliers ADD COLUMN holidays TEXT",
        "ALTER TABLE suppliers ADD COLUMN key_contact TEXT",
        "ALTER TABLE suppliers ADD COLUMN contact_phone TEXT",
        "ALTER TABLE suppliers ADD COLUMN contact_mobile TEXT",
        "ALTER TABLE suppliers ADD COLUMN bank_details TEXT",
        "ALTER TABLE suppliers ADD COLUMN supply_agreement TEXT",
        "ALTER TABLE suppliers ADD COLUMN other_details TEXT",
        "ALTER TABLE suppliers ADD COLUMN is_active INTEGER DEFAULT 1",
        "ALTER TABLE suppliers ADD COLUMN source TEXT",
        "ALTER TABLE suppliers ADD COLUMN updated_at TEXT",
        "ALTER TABLE suppliers ADD COLUMN raw_json TEXT",
        # ── Catalogue import: supplier detection / resolution (stage-1 confirm) ──
        "ALTER TABLE catalogue_imports ADD COLUMN detected_supplier_name TEXT",
        "ALTER TABLE catalogue_imports ADD COLUMN detected_brands TEXT",
        "ALTER TABLE catalogue_imports ADD COLUMN supplier_confidence REAL",
        "ALTER TABLE catalogue_imports ADD COLUMN supplier_source TEXT",
        "ALTER TABLE catalogue_imports ADD COLUMN supplier_status TEXT",
        # ── AI tagging / categorization (catalogue onboarding) ──
        "ALTER TABLE catalogue_items ADD COLUMN ai_tags TEXT",       # JSON array of suggested tags
        "ALTER TABLE catalogue_items ADD COLUMN ai_category TEXT",   # AI-suggested SKU category
        "ALTER TABLE catalogue_items ADD COLUMN ai_subcategory TEXT",# AI-detected functional/clinical class
        "ALTER TABLE products ADD COLUMN subcategory TEXT",          # AI subcategory (same class)
        "ALTER TABLE category_rules ADD COLUMN sku_digit TEXT",      # data-driven SKU leading digit
        # ── Additional OCR-extracted catalogue fields (v7 OCR-marked columns) ──
        "ALTER TABLE catalogue_items ADD COLUMN species TEXT",
        "ALTER TABLE catalogue_items ADD COLUMN weight_grams REAL",
        "ALTER TABLE catalogue_items ADD COLUMN rrp REAL",
        "ALTER TABLE catalogue_items ADD COLUMN min_purchase_qty INTEGER",
        "ALTER TABLE catalogue_items ADD COLUMN bulk_tiers TEXT",
        # ── Product-level OCR fields (pushed to the v7 SSOT sheet) ──
        "ALTER TABLE products ADD COLUMN species TEXT",
        "ALTER TABLE products ADD COLUMN rrp REAL",
        "ALTER TABLE products ADD COLUMN min_purchase_qty INTEGER",
        # structured bulk tiers (JSON) for the sheet's mbb_tier_structure column
        "ALTER TABLE product_suppliers ADD COLUMN mbb_tiers TEXT",
        # ── Performance indexes ──────────────────────────────────────────────
        # FK indexes for the per-product relationship loads in GET /products
        # (channels, suppliers, stock, velocity) — without these every list build
        # full-scans these tables once per relationship.
        "CREATE INDEX IF NOT EXISTS ix_product_suppliers_product_id ON product_suppliers(product_id)",
        "CREATE INDEX IF NOT EXISTS ix_product_channels_product_id ON product_channels(product_id)",
        "CREATE INDEX IF NOT EXISTS ix_stock_levels_product_id ON stock_levels(product_id)",
        # Per-channel sales velocity (algo-dashboard multichannel sync: clinic + HKTV + Shopify)
        "ALTER TABLE sales_velocity ADD COLUMN weekly_demand_clinic REAL",
        "ALTER TABLE sales_velocity ADD COLUMN weekly_demand_hktv REAL",
        "ALTER TABLE sales_velocity ADD COLUMN weekly_demand_shopify REAL",
        "ALTER TABLE sales_velocity ADD COLUMN trend_json TEXT",
        "CREATE INDEX IF NOT EXISTS ix_sales_velocity_product_id ON sales_velocity(product_id)",
        "CREATE INDEX IF NOT EXISTS ix_product_tags_product_id ON product_tags(product_id)",
        # Product list filters / sort
        "CREATE INDEX IF NOT EXISTS ix_products_status ON products(status)",
        "CREATE INDEX IF NOT EXISTS ix_products_category ON products(category)",
        # Catalogue matching lookups (barcode + supplier-SKU resolution)
        "CREATE INDEX IF NOT EXISTS ix_product_suppliers_barcode ON product_suppliers(barcode)",
        "CREATE INDEX IF NOT EXISTS ix_product_suppliers_sup_sku ON product_suppliers(supplier_id, supplier_sku)",
        # Review queue filter + per-import grouping
        "CREATE INDEX IF NOT EXISTS ix_catalogue_items_review_status ON catalogue_items(review_status)",
        # Pending-queue pagination: filter by status+import, order by confidence
        "CREATE INDEX IF NOT EXISTS ix_catalogue_items_status_import ON catalogue_items(review_status, import_id, confidence_score)",
        # Realtime inventory delta endpoint (/products/changes?since=…)
        "CREATE INDEX IF NOT EXISTS ix_products_updated_at ON products(updated_at)",
        # Per-platform listing status (NULL = not listed there)
        "ALTER TABLE products ADD COLUMN shopify_status TEXT",
        "ALTER TABLE products ADD COLUMN daysmart_status TEXT",
        "ALTER TABLE products ADD COLUMN hktv_status TEXT",
        # Platform-recorded costs (refreshed by the nightly reconciliation)
        "ALTER TABLE products ADD COLUMN shopify_cost REAL",
        "ALTER TABLE products ADD COLUMN daysmart_cost REAL",
        "ALTER TABLE products ADD COLUMN hktv_cost REAL",
        "CREATE INDEX IF NOT EXISTS ix_catalogue_items_import_id ON catalogue_items(import_id)",
        # Structured Max-Bulk-Buy term (one per SKU)
        "ALTER TABLE product_suppliers ADD COLUMN mbb_type TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN mbb_min_amount REAL",
        "ALTER TABLE product_suppliers ADD COLUMN mbb_free_qty INTEGER",
        "ALTER TABLE product_suppliers ADD COLUMN mbb_discount_pct REAL",
        # Legacy rows that already carry an MBB unit cost are the 'unit_cost' type
        "UPDATE product_suppliers SET mbb_type='unit_cost' "
        "WHERE mbb_type IS NULL AND bulk_buy_cost IS NOT NULL AND bulk_buy_cost > 0",
        # ── Relational MBB terms — replaces the flat mbb_* scalars; a SKU x supplier has 0..N.
        #    Backfilled from the old scalars + free text by scripts/backfill_mbb_terms.py. ──
        "CREATE TABLE IF NOT EXISTS mbb_terms ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " product_supplier_id INTEGER NOT NULL,"
        " kind TEXT NOT NULL,"
        " min_qty INTEGER, min_spend REAL,"
        " free_qty INTEGER, discount_pct REAL, unit_cost REAL,"
        " note TEXT, sort_order INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL, updated_at TEXT)",
        "CREATE INDEX IF NOT EXISTS ix_mbb_terms_ps_id ON mbb_terms(product_supplier_id)",
        # ── Retire the flat MBB scalars (now in mbb_terms). IMPORTANT: run scripts/backfill_mbb_terms.py
        #    on any DB with existing MBB data BEFORE these drops, or that data is lost.
        #    Each drop is wrapped in try/except below, so re-runs / already-dropped are no-ops. ──
        "ALTER TABLE product_suppliers DROP COLUMN bulk_buy_cost",
        "ALTER TABLE product_suppliers DROP COLUMN bulk_buy_min_qty",
        "ALTER TABLE product_suppliers DROP COLUMN mbb_terms",
        "ALTER TABLE product_suppliers DROP COLUMN mbb_tiers",
        "ALTER TABLE product_suppliers DROP COLUMN mbb_type",
        "ALTER TABLE product_suppliers DROP COLUMN mbb_min_amount",
        "ALTER TABLE product_suppliers DROP COLUMN mbb_free_qty",
        "ALTER TABLE product_suppliers DROP COLUMN mbb_discount_pct",
        # Retire procurement/reconciliation cost columns — invoice reconciliation lives in the
        # procurement flow; the OCR catalogue cost writes straight to basic_cost (the wholesale cost).
        # NOTE: only product_suppliers.daysmart_cost is dropped — products.daysmart_cost (platform
        # avg cost / daysmart_avg_cost) is a different column and is kept.
        "ALTER TABLE product_suppliers DROP COLUMN catalogue_cost",
        "ALTER TABLE product_suppliers DROP COLUMN daysmart_cost",
        "ALTER TABLE product_suppliers DROP COLUMN cost_reconciled_at",
        # Skip bucket for catalogue review
        "ALTER TABLE catalogue_items ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE catalogue_items ADD COLUMN skipped_at TEXT",
        "ALTER TABLE catalogue_items ADD COLUMN skipped_by TEXT",
        # Product variant (size/volume/flavour) extracted during scanning
        "ALTER TABLE catalogue_items ADD COLUMN variant TEXT",
        # Original (pre-translation) description, kept when a non-English name is translated
        "ALTER TABLE catalogue_items ADD COLUMN original_description TEXT",
        # Display unit for weight (kg|lb) — weight stays canonical in grams; this is how to show/edit it
        "ALTER TABLE catalogue_items ADD COLUMN weight_unit TEXT",
        "ALTER TABLE products ADD COLUMN weight_unit TEXT",
        # Vet vs non-vet SKU segment (backfilled from the supplier's segment)
        "ALTER TABLE products ADD COLUMN segment TEXT",
        # ── Supplier out-of-stock (OOS) tracking — status on the link + a history table ──
        "ALTER TABLE product_suppliers ADD COLUMN stock_status TEXT NOT NULL DEFAULT 'in_stock'",
        "ALTER TABLE product_suppliers ADD COLUMN reported_out_at TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN expected_restock_at TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN stock_confirmed_by TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN stock_note TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN stock_updated_at TEXT",
        "CREATE TABLE IF NOT EXISTS supplier_stock_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " product_supplier_id INTEGER NOT NULL,"
        " out_at TEXT NOT NULL, restock_at TEXT, note TEXT,"
        " created_by TEXT, created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ix_supplier_stock_events_ps ON supplier_stock_events(product_supplier_id)",
        # ── Competitor price scraping — scrape metadata on the existing competitor_prices table ──
        "ALTER TABLE competitor_prices ADD COLUMN platform TEXT",
        "ALTER TABLE competitor_prices ADD COLUMN in_stock INTEGER",
        "ALTER TABLE competitor_prices ADD COLUMN title TEXT",
        "ALTER TABLE competitor_prices ADD COLUMN last_status TEXT",
        "CREATE INDEX IF NOT EXISTS ix_competitor_prices_product ON competitor_prices(product_id)",
        # ── Ordering terms — order multiple / MOQ (additive, nullable, NO backfill, NO default).
        #    Separate from units_per_pack (pack size) and basic_cost; populated by a reviewed
        #    remediation later, not by this migration. minimum_order_source is an app-level enum. ──
        "ALTER TABLE product_suppliers ADD COLUMN order_increment_qty INTEGER",
        "ALTER TABLE product_suppliers ADD COLUMN order_increment_uom TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN minimum_order_qty INTEGER",
        "ALTER TABLE product_suppliers ADD COLUMN minimum_order_uom TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN minimum_order_source TEXT",
        "ALTER TABLE product_suppliers ADD COLUMN pricing_note TEXT",
        # ── Re-parse versioning (RP-1.1) — additive, nullable, NO default, NO backfill.
        #    parser_version/reparsed_at/reparse_source track which parser last produced an item's
        #    fields; catalogue_imports.source_ref links a persisted upload (RP-1.2). ──
        "ALTER TABLE catalogue_items ADD COLUMN parser_version TEXT",
        "ALTER TABLE catalogue_items ADD COLUMN reparsed_at TEXT",
        "ALTER TABLE catalogue_items ADD COLUMN reparse_source TEXT",
        "ALTER TABLE catalogue_imports ADD COLUMN source_ref TEXT",
        # ── Re-parse staging (RP-2.1) — a batch per re-parse run, a change per field diff.
        #    New tables (also defined as models for create_all on fresh DBs). Additive; the diff is
        #    staged here and nothing writes to Product/ProductSupplier until a change is confirmed. ──
        "CREATE TABLE IF NOT EXISTS reparse_batch ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " scope_type TEXT NOT NULL, scope_ref TEXT NOT NULL,"
        " parser_version TEXT, mode TEXT NOT NULL DEFAULT 'text',"
        " status TEXT NOT NULL DEFAULT 'open',"
        " item_count INTEGER, changed_count INTEGER,"
        " created_at TEXT NOT NULL, created_by TEXT)",
        "CREATE TABLE IF NOT EXISTS reparse_change ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " batch_id INTEGER NOT NULL, catalogue_item_id INTEGER NOT NULL, product_id INTEGER,"
        " field TEXT NOT NULL, old_value TEXT, new_value TEXT,"
        " affects_cost INTEGER NOT NULL DEFAULT 0,"
        " eff_cost_before REAL, eff_cost_after REAL,"
        " status TEXT NOT NULL DEFAULT 'pending', confirmed_by TEXT, confirmed_at TEXT)",
        "CREATE INDEX IF NOT EXISTS ix_reparse_change_batch ON reparse_change(batch_id)",
        # ── Catalogue logical persistence foundation ───────────────────────────
        # CIS-104.1 created catalogue_ingestion_runs with an integer id. The
        # logical catalogue pipeline keeps that key and adds stable UUID plus
        # exact supplier-source contract identity. Additive and nullable for
        # existing rows; run_uuid is backfilled below before the unique index.
        "ALTER TABLE catalogue_ingestion_runs ADD COLUMN run_uuid TEXT",
        "ALTER TABLE catalogue_ingestion_runs ADD COLUMN catalogue_source_document_id INTEGER REFERENCES catalogue_source_documents(id)",
        "ALTER TABLE catalogue_ingestion_runs ADD COLUMN supplier_source_contract_id TEXT",
        "ALTER TABLE catalogue_ingestion_runs ADD COLUMN supplier_source_contract_version TEXT",
        "ALTER TABLE catalogue_ingestion_runs ADD COLUMN document_type TEXT",
        "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_document_uuid ON catalogue_ingestion_runs(run_uuid)",
        "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_supplier_contract "
        "ON catalogue_ingestion_runs(supplier_id, supplier_source_contract_id, supplier_source_contract_version)",
        "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_pipeline_source_document "
        "ON catalogue_ingestion_runs(catalogue_source_document_id)",
        # ── Raw-stage completion metadata (file-level only) ────────────────────
        # The raw stage persists what was received and verified: exact size,
        # structural page count for PDFs, and a durable completed/failed marker.
        # Additive and nullable for existing rows.
        "ALTER TABLE catalogue_source_documents ADD COLUMN byte_size INTEGER",
        "ALTER TABLE catalogue_source_documents ADD COLUMN page_count INTEGER",
        "ALTER TABLE catalogue_source_documents ADD COLUMN raw_stage_status TEXT",
        "ALTER TABLE catalogue_source_documents ADD COLUMN raw_stage_completed_at TEXT",
    ]
    with engine.connect() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # column / index already exists

        try:
            rows = conn.execute(text(
                "SELECT id FROM catalogue_ingestion_runs WHERE run_uuid IS NULL OR trim(run_uuid) = ''"
            )).fetchall()
            for row in rows:
                conn.execute(
                    text("UPDATE catalogue_ingestion_runs SET run_uuid = :uuid WHERE id = :id"),
                    {"uuid": str(uuid4()), "id": row[0]},
                )
            conn.commit()
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_catalogue_ingestion_runs_run_uuid "
                "ON catalogue_ingestion_runs(run_uuid)"
            ))
            conn.commit()
        except Exception:
            pass
        try:
            _relax_ingestion_run_started_at(conn)
        except Exception:
            pass

        # catalogue_cost_staging — OCR writes here; humans approve before costs go to product_suppliers
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS catalogue_cost_staging (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id          INTEGER REFERENCES catalogue_imports(id),
                supplier_id        INTEGER REFERENCES suppliers(id),
                raw_supplier_sku   TEXT,
                matched_product_id INTEGER REFERENCES products(id),
                match_confidence   REAL,
                extracted_cost     REAL,
                status             TEXT NOT NULL DEFAULT 'pending',
                reviewed_by        TEXT,
                reviewed_at        TEXT,
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.commit()

        # catalogue_ingestion_runs — CIS-104.1. One row per extraction attempt on a
        # catalogue_imports row; isolated v2 table, not read/written by any v1 code.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS catalogue_ingestion_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                source_document_id  INTEGER NOT NULL REFERENCES catalogue_imports(id),
                run_uuid            TEXT,
                catalogue_source_document_id INTEGER REFERENCES catalogue_source_documents(id),
                supplier_id         INTEGER REFERENCES suppliers(id),
                contract_version    TEXT,
                supplier_source_contract_id TEXT,
                supplier_source_contract_version TEXT,
                document_type       TEXT,
                extractor_name      TEXT NOT NULL,
                extractor_version   TEXT NOT NULL,
                parent_run_id       INTEGER REFERENCES catalogue_ingestion_runs(id),
                status              TEXT NOT NULL DEFAULT 'queued',
                started_at          TEXT,
                completed_at        TEXT,
                items_extracted     INTEGER,
                metrics             TEXT,
                error_summary       TEXT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_document"
            " ON catalogue_ingestion_runs(source_document_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_parent"
            " ON catalogue_ingestion_runs(parent_run_id)"
        ))
        conn.commit()

        # Users table — for JWT auth and edit attribution
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                display_name  TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'data_entry',
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.commit()

        # Access acknowledgements — click-wrap NDA acceptance for /tech-stack GitHub link
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS access_acknowledgements (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                github_username TEXT NOT NULL,
                full_name_typed TEXT NOT NULL,
                email_requestor TEXT,
                terms_version   TEXT NOT NULL DEFAULT 'v1-2026-06',
                ip_address      TEXT,
                accepted_at     TEXT NOT NULL DEFAULT (datetime('now')),
                email_sent_at   TEXT,
                email_send_error TEXT
            )
        """))
        conn.commit()
        # Idempotent ALTERs for installs that pre-date the email columns
        for sql in [
            "ALTER TABLE access_acknowledgements ADD COLUMN email_requestor TEXT",
            "ALTER TABLE access_acknowledgements ADD COLUMN email_sent_at TEXT",
            "ALTER TABLE access_acknowledgements ADD COLUMN email_send_error TEXT",
        ]:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass

        # Item-category list + GP floors. IMS uses ONE operational category list.
        # Retire the short-lived "SKU category" taxonomy (and legacy Toys) so the
        # /categories list shows only the canonical item categories.
        try:
            conn.execute(text(
                "DELETE FROM category_rules WHERE category IN "
                "('Treats','Toys & Accessories','Cleaning & Grooming','Healthcare','Archived','Toys')"))
            conn.commit()
        except Exception:
            pass
        # INSERT OR IGNORE so a re-deploy never clobbers a floor a human has tuned (category is PK).
        # sku_digit is the leading digit of generated SKUs for that category (data-driven so
        # categories can be added without a code change). COALESCE-set it for pre-existing rows.
        for cat, floor, digit in [
            ("Medicine", 0.70, "5"), ("Preventative", 0.40, "5"), ("Supplement", 0.40, "5"),
            ("Shampoo", 0.40, "4"), ("Food", 0.35, "1"), ("Not-For-Sale", 0.00, "6"),
            ("Pet Hygiene", 0.40, "4"), ("Cat Litter", 0.35, "4"), ("Others", 0.40, "7"),
        ]:
            try:
                conn.execute(text(
                    "INSERT OR IGNORE INTO category_rules "
                    "(category, gp_floor, storage_rule, channel_restriction, sku_digit) "
                    "VALUES (:c, :f, 'any', NULL, :d)"
                ), {"c": cat, "f": floor, "d": digit})
                conn.execute(text(
                    "UPDATE category_rules SET sku_digit = :d WHERE category = :c AND (sku_digit IS NULL OR sku_digit = '')"
                ), {"c": cat, "d": digit})
                conn.commit()
            except Exception:
                pass


def _relax_ingestion_run_started_at(conn):
    """Make queued ingestion runs truthful on SQLite by allowing started_at NULL.

    Earlier CIS-104.1 installs created `catalogue_ingestion_runs.started_at` as
    NOT NULL. SQLite cannot alter nullability in place, so this performs a
    narrow table rebuild only when PRAGMA table_info reports that older shape.
    """

    columns = conn.execute(text("PRAGMA table_info(catalogue_ingestion_runs)")).fetchall()
    if not columns:
        return
    column_map = {row[1]: row for row in columns}
    started_at = column_map.get("started_at")
    if started_at is None or int(started_at[3]) == 0:
        return

    required_columns = [
        "id",
        "run_uuid",
        "source_document_id",
        "catalogue_source_document_id",
        "supplier_id",
        "contract_version",
        "supplier_source_contract_id",
        "supplier_source_contract_version",
        "document_type",
        "extractor_name",
        "extractor_version",
        "parent_run_id",
        "status",
        "started_at",
        "completed_at",
        "items_extracted",
        "metrics",
        "error_summary",
        "created_at",
    ]
    existing = set(column_map)
    for name in required_columns:
        if name not in existing:
            if name == "run_uuid":
                conn.execute(text("ALTER TABLE catalogue_ingestion_runs ADD COLUMN run_uuid TEXT"))
            elif name == "catalogue_source_document_id":
                conn.execute(text("ALTER TABLE catalogue_ingestion_runs ADD COLUMN catalogue_source_document_id INTEGER REFERENCES catalogue_source_documents(id)"))
            elif name in {"supplier_source_contract_id", "supplier_source_contract_version", "document_type"}:
                conn.execute(text(f"ALTER TABLE catalogue_ingestion_runs ADD COLUMN {name} TEXT"))
            else:
                return
    conn.commit()

    conn.execute(text("ALTER TABLE catalogue_ingestion_runs RENAME TO catalogue_ingestion_runs__old"))
    conn.execute(text("""
        CREATE TABLE catalogue_ingestion_runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uuid            TEXT,
            source_document_id  INTEGER NOT NULL REFERENCES catalogue_imports(id),
            catalogue_source_document_id INTEGER REFERENCES catalogue_source_documents(id),
            supplier_id         INTEGER REFERENCES suppliers(id),
            contract_version    TEXT,
            supplier_source_contract_id TEXT,
            supplier_source_contract_version TEXT,
            document_type       TEXT,
            extractor_name      TEXT NOT NULL,
            extractor_version   TEXT NOT NULL,
            parent_run_id       INTEGER REFERENCES catalogue_ingestion_runs(id),
            status              TEXT NOT NULL DEFAULT 'queued',
            started_at          TEXT,
            completed_at        TEXT,
            items_extracted     INTEGER,
            metrics             TEXT,
            error_summary       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))
    column_list = ", ".join(required_columns)
    conn.execute(text(
        f"INSERT INTO catalogue_ingestion_runs ({column_list}) "
        f"SELECT {column_list} FROM catalogue_ingestion_runs__old"
    ))
    conn.execute(text("DROP TABLE catalogue_ingestion_runs__old"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_catalogue_ingestion_runs_run_uuid ON catalogue_ingestion_runs(run_uuid)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_document ON catalogue_ingestion_runs(source_document_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ingestion_runs_parent ON catalogue_ingestion_runs(parent_run_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_document_uuid ON catalogue_ingestion_runs(run_uuid)"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_supplier_contract "
        "ON catalogue_ingestion_runs(supplier_id, supplier_source_contract_id, supplier_source_contract_version)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ingestion_runs_pipeline_source_document "
        "ON catalogue_ingestion_runs(catalogue_source_document_id)"
    ))
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
