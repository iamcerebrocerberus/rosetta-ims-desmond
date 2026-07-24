"""PR-A guard: the additive ordering-term columns on product_suppliers.

Proves the schema change is purely additive and inert:
  * the six new fields exist and are NULLABLE with no default
  * an existing-style ProductSupplier row loads with them all NULL
  * get_unit_cost() behaviour is unchanged (still basic_cost / units_per_pack)
  * re-running seed_category_rules() neither errors nor rewrites existing rows

Runnable directly (`python tests/test_ordering_fields_schema.py`) or under pytest.
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

NEW_FIELDS = [
    "order_increment_qty", "order_increment_uom", "minimum_order_qty",
    "minimum_order_uom", "minimum_order_source", "pricing_note",
]


def _cols():
    return {c["name"]: c for c in sa.inspect(database.engine).get_columns("product_suppliers")}


def _mk_link(session, code, sku, **ps_kwargs):
    s = models.Supplier(code=code, name=code, created_at="2026-01-01")
    session.add(s); session.flush()
    p = models.Product(sku_code=sku, name="x", category="Food", status="ACTIVE",
                       storage_rule="any", created_at="2026-01-01T00:00:00",
                       updated_at="2026-01-01T00:00:00")
    session.add(p); session.flush()
    ps = models.ProductSupplier(product_id=p.id, supplier_id=s.id, cost_source="manual",
                                pack_source="manual", updated_at="2026-01-01T00:00:00", **ps_kwargs)
    session.add(ps); session.commit()
    return ps.id


def test_new_fields_exist_and_are_nullable_with_no_default():
    cols = _cols()
    for f in NEW_FIELDS:
        assert f in cols, f"{f} missing from product_suppliers"
        assert cols[f]["nullable"] is True, f"{f} must be nullable"
        assert cols[f].get("default") is None, f"{f} must have no default"
    # the fix's read path is untouched
    assert "units_per_pack" in cols and "basic_cost" in cols


def test_row_loads_with_new_fields_null():
    d = database.SessionLocal()
    try:
        pid = _mk_link(d, "PRA1", "PRA-1", basic_cost=100.0, units_per_pack=4)
        got = d.get(models.ProductSupplier, pid)
        for f in NEW_FIELDS:
            assert getattr(got, f) is None, f"{f} should default to None (no behavioural default)"
        assert got.basic_cost == 100.0 and got.units_per_pack == 4
    finally:
        d.close()


def test_get_unit_cost_behaviour_unchanged():
    assert get_unit_cost(models.ProductSupplier(basic_cost=100.0, units_per_pack=4)) == 25.0
    assert get_unit_cost(models.ProductSupplier(basic_cost=100.0, units_per_pack=1)) == 100.0
    assert get_unit_cost(models.ProductSupplier(basic_cost=100.0, units_per_pack=None)) == 100.0
    assert get_unit_cost(models.ProductSupplier(basic_cost=None, units_per_pack=4)) is None


def test_rerun_migration_is_idempotent_and_no_rewrite():
    d = database.SessionLocal()
    try:
        pid = _mk_link(d, "PRA2", "PRA-2", basic_cost=42.0, units_per_pack=12)
    finally:
        d.close()
    database.seed_category_rules(database.engine)   # must not raise, must not touch existing rows
    d = database.SessionLocal()
    try:
        got = d.get(models.ProductSupplier, pid)
        assert got.basic_cost == 42.0 and got.units_per_pack == 12   # unchanged (no backfill)
        assert got.order_increment_qty is None and got.minimum_order_qty is None
        assert got.minimum_order_source is None and got.pricing_note is None
    finally:
        d.close()


if __name__ == "__main__":
    test_new_fields_exist_and_are_nullable_with_no_default()
    test_row_loads_with_new_fields_null()
    test_get_unit_cost_behaviour_unchanged()
    test_rerun_migration_is_idempotent_and_no_rewrite()
    print("✅ PR-A schema: 6 fields nullable/no-default, rows load, get_unit_cost unchanged, "
          "migration idempotent with no data rewrite")
