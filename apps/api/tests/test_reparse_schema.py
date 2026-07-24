"""RP-1.1 guard: the additive re-parse versioning columns.

Proves the schema change is purely additive and inert:
  * catalogue_items gains parser_version / reparsed_at / reparse_source; catalogue_imports gains
    source_ref — all NULLABLE with no default
  * an existing-style CatalogueImport + CatalogueItem load with them all NULL
  * get_unit_cost() behaviour is unchanged (no cost/pricing surface touched)
  * re-running seed_category_rules() neither errors nor rewrites existing rows

Runnable under pytest (or `python -m tests.test_reparse_schema` from backend/).
"""
import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import sqlalchemy as sa       # noqa: E402
import database               # noqa: E402
import models                 # noqa: E402
from services.pricing_service import get_unit_cost  # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

ITEM_FIELDS = ["parser_version", "reparsed_at", "reparse_source"]
IMPORT_FIELDS = ["source_ref"]


def _cols(table):
    return {c["name"]: c for c in sa.inspect(database.engine).get_columns(table)}


def test_new_columns_exist_nullable_no_default():
    ci = _cols("catalogue_items")
    for f in ITEM_FIELDS:
        assert f in ci, f"{f} missing from catalogue_items"
        assert ci[f]["nullable"] is True, f"{f} must be nullable"
        assert ci[f].get("default") is None, f"{f} must have no default"
    cm = _cols("catalogue_imports")
    for f in IMPORT_FIELDS:
        assert f in cm, f"{f} missing from catalogue_imports"
        assert cm[f]["nullable"] is True and cm[f].get("default") is None
    # untouched retained fields still present
    assert "raw_description" in ci and "pack_size" in ci and "units_per_pack" in ci


def test_rows_load_with_new_fields_null():
    d = database.SessionLocal()
    try:
        imp = models.CatalogueImport(filename="pricelist.pdf", format="pdf",
                                     imported_at="2026-01-01T00:00:00", status="review", item_count=1)
        d.add(imp); d.flush()
        item = models.CatalogueItem(import_id=imp.id, raw_description="Test 5L", uom="ml",
                                    units_per_pack=1, review_status="pending", skipped=0,
                                    created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        got_i = d.get(models.CatalogueItem, item.id)
        for f in ITEM_FIELDS:
            assert getattr(got_i, f) is None, f"{f} should default to None (no behavioural default)"
        assert d.get(models.CatalogueImport, imp.id).source_ref is None
        # the retained text the re-parse will read is intact
        assert got_i.raw_description == "Test 5L" and got_i.units_per_pack == 1
    finally:
        d.close()


def test_get_unit_cost_behaviour_unchanged():
    assert get_unit_cost(models.ProductSupplier(basic_cost=100.0, units_per_pack=4)) == 25.0
    assert get_unit_cost(models.ProductSupplier(basic_cost=100.0, units_per_pack=1)) == 100.0
    assert get_unit_cost(models.ProductSupplier(basic_cost=100.0, units_per_pack=None)) == 100.0
    assert get_unit_cost(models.ProductSupplier(basic_cost=None, units_per_pack=4)) is None


def test_rerun_migration_idempotent_and_no_rewrite():
    d = database.SessionLocal()
    try:
        imp = models.CatalogueImport(filename="f2.pdf", format="pdf",
                                     imported_at="2026-01-01T00:00:00", status="review", item_count=1)
        d.add(imp); d.flush()
        item = models.CatalogueItem(import_id=imp.id, raw_description="Keep me", cost_price=42.0,
                                    review_status="pending", skipped=0, created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        iid = item.id
    finally:
        d.close()
    database.seed_category_rules(database.engine)   # must not raise, must not touch existing rows
    d = database.SessionLocal()
    try:
        got = d.get(models.CatalogueItem, iid)
        assert got.raw_description == "Keep me" and got.cost_price == 42.0   # unchanged
        assert got.parser_version is None and got.reparsed_at is None and got.reparse_source is None
    finally:
        d.close()


if __name__ == "__main__":
    test_new_columns_exist_nullable_no_default()
    test_rows_load_with_new_fields_null()
    test_get_unit_cost_behaviour_unchanged()
    test_rerun_migration_idempotent_and_no_rewrite()
    print("RP-1.1 schema: 4 columns nullable/no-default, rows load NULL, get_unit_cost unchanged, "
          "migration idempotent with no data rewrite")
