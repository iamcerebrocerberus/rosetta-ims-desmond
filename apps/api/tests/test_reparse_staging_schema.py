"""RP-2.1 guard: the re-parse staging tables (reparse_batch + reparse_change).

Proves the tables exist with the architecture §2.3 shape, that a batch + its change rows round-trip via
the ORM relationship, and that the migration is additive/idempotent and inert for existing data.
Runnable under pytest (or `python -m tests.test_reparse_staging_schema` from backend/).
"""
import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import sqlalchemy as sa       # noqa: E402
import database               # noqa: E402
import models                 # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

BATCH_COLS = {"id", "scope_type", "scope_ref", "parser_version", "mode", "status",
              "item_count", "changed_count", "created_at", "created_by"}
CHANGE_COLS = {"id", "batch_id", "catalogue_item_id", "product_id", "field", "old_value", "new_value",
               "affects_cost", "eff_cost_before", "eff_cost_after", "status", "confirmed_by", "confirmed_at"}


def test_tables_exist_with_expected_columns():
    insp = sa.inspect(database.engine)
    tables = set(insp.get_table_names())
    assert "reparse_batch" in tables and "reparse_change" in tables
    assert {c["name"] for c in insp.get_columns("reparse_batch")} >= BATCH_COLS
    assert {c["name"] for c in insp.get_columns("reparse_change")} >= CHANGE_COLS


def test_batch_and_change_roundtrip():
    d = database.SessionLocal()
    try:
        # a catalogue_item to point the change at
        imp = models.CatalogueImport(filename="f.pdf", format="pdf", imported_at="2026-01-01T00:00:00",
                                     status="review", item_count=1)
        d.add(imp); d.flush()
        item = models.CatalogueItem(import_id=imp.id, raw_description="Antiseptic 5L", uom="ml",
                                    units_per_pack=5000, review_status="pending", skipped=0,
                                    created_at="2026-01-01T00:00:00")
        d.add(item); d.flush()

        batch = models.ReparseBatch(scope_type="supplier", scope_ref="14", parser_version="v2",
                                    mode="text", status="open", item_count=1, changed_count=1,
                                    created_at="2026-01-01T00:00:00", created_by="tester")
        d.add(batch); d.flush()
        change = models.ReparseChange(batch_id=batch.id, catalogue_item_id=item.id, field="units_per_pack",
                                      old_value="5000", new_value="1", affects_cost=1,
                                      eff_cost_before=0.14, eff_cost_after=690.0, status="pending")
        d.add(change); d.commit()

        got = d.get(models.ReparseBatch, batch.id)
        assert got.status == "open" and len(got.changes) == 1
        ch = got.changes[0]
        assert ch.field == "units_per_pack" and ch.old_value == "5000" and ch.new_value == "1"
        assert ch.affects_cost == 1 and ch.eff_cost_after == 690.0 and ch.status == "pending"
        assert ch.batch.id == batch.id                      # back-reference
        assert ch.confirmed_by is None and ch.product_id is None
    finally:
        d.close()


def test_defaults_applied():
    d = database.SessionLocal()
    try:
        b = models.ReparseBatch(scope_type="item", scope_ref="SKU-1", created_at="2026-01-01T00:00:00")
        d.add(b); d.flush()
        c = models.ReparseChange(batch_id=b.id, catalogue_item_id=1, field="brand",
                                 old_value=None, new_value="Hill's")
        d.add(c); d.commit()
        assert d.get(models.ReparseBatch, b.id).mode == "text" and d.get(models.ReparseBatch, b.id).status == "open"
        cc = d.get(models.ReparseChange, c.id)
        assert cc.status == "pending" and cc.affects_cost == 0
    finally:
        d.close()


def test_rerun_migration_idempotent():
    database.seed_category_rules(database.engine)   # must not raise (tables already exist)
    insp = sa.inspect(database.engine)
    assert "reparse_batch" in insp.get_table_names() and "reparse_change" in insp.get_table_names()


if __name__ == "__main__":
    test_tables_exist_with_expected_columns()
    test_batch_and_change_roundtrip()
    test_defaults_applied()
    test_rerun_migration_idempotent()
    print("RP-2.1 staging: reparse_batch + reparse_change exist, batch/change round-trip, "
          "defaults applied, migration idempotent")
