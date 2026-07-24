"""End-to-end coverage for the Supplier Terms editor reform (routers/products.py).

Guards the cost-basis + ordering-terms rules the Manage Suppliers modal relies on:
  * effective_unit_cost = basic_cost / cost-basis units (never divided by MOQ / order multiple / min-sellable)
  * ordering terms (order increment / minimum order + UOM, source, pricing note) round-trip through the API
  * placeholder junk (#N/A, N/A, nan, blank) is never persisted — it becomes NULL
  * a qty set without its UOM is rejected (400), on both POST and PATCH
  * PATCH is partial: a "make primary" call that omits cost/ordering fields never wipes them
  * a blank value on PATCH clears the field

Runnable directly (`python tests/test_supplier_terms_ui.py`) or under pytest.
"""
import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import pytest   # noqa: E402
import database  # noqa: E402
import models    # noqa: E402
import main      # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dependencies import require_user       # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


class _FakeAdmin:
    id, username, display_name, role = 7, "editor", "Ed Itor", "admin"


# Scope the auth override to THIS module's tests only. A permanent module-level assignment on the
# shared main.app would clobber other suites' overrides (they run against the same app instance).
@pytest.fixture(autouse=True)
def _as_admin():
    prev = main.app.dependency_overrides.get(require_user)
    main.app.dependency_overrides[require_user] = lambda: _FakeAdmin()
    yield
    if prev is None:
        main.app.dependency_overrides.pop(require_user, None)
    else:
        main.app.dependency_overrides[require_user] = prev


_client = TestClient(main.app)


def _seed(sku, supcode):
    d = database.SessionLocal()
    try:
        s = models.Supplier(code=supcode, name=f"{supcode} Ltd", created_at="2026-01-01")
        d.add(s); d.flush()
        p = models.Product(sku_code=sku, name="Test Cans 400g", category="Food", status="ACTIVE",
                           storage_rule="any", uom="can",
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.commit()
        return sku, s.id
    finally:
        d.close()


def _supplier(sku, sid):
    r = _client.get(f"/products/{sku}/suppliers")
    assert r.status_code == 200, r.text
    return next(s for s in r.json()["suppliers"] if s["supplier_id"] == sid)


def test_add_persists_ordering_terms_and_effective_cost():
    sku, sid = _seed("STU-1", "STUA")
    r = _client.post(f"/products/{sku}/suppliers", json={
        "supplier_id": sid, "supplier_sku": "AC-400",
        "basic_cost": 240.0, "units_per_pack": 24,
        "order_increment_qty": 24, "order_increment_uom": "can",
        "minimum_order_qty": 48, "minimum_order_uom": "can",
        "minimum_order_source": "catalogue", "pricing_note": "Price is per case of 24 cans",
    })
    assert r.status_code == 200, r.text
    s = _supplier(sku, sid)
    assert s["basic_cost"] == 240.0 and s["units_per_pack"] == 24
    assert s["effective_unit_cost"] == 10.0            # 240 / 24 cost-basis units — NOT / MOQ (48) or increment (24)
    assert s["order_increment_qty"] == 24 and s["order_increment_uom"] == "can"
    assert s["minimum_order_qty"] == 48 and s["minimum_order_uom"] == "can"
    assert s["minimum_order_source"] == "catalogue"
    assert s["pricing_note"] == "Price is per case of 24 cans"


def test_effective_cost_is_basic_cost_when_basis_units_one():
    sku, sid = _seed("STU-2", "STUB")
    _client.post(f"/products/{sku}/suppliers", json={"supplier_id": sid, "basic_cost": 88.0, "units_per_pack": 1})
    assert _supplier(sku, sid)["effective_unit_cost"] == 88.0


def test_placeholders_never_persist():
    sku, sid = _seed("STU-3", "STUC")
    r = _client.post(f"/products/{sku}/suppliers", json={
        "supplier_id": sid, "supplier_sku": "#N/A", "basic_cost": 50.0, "units_per_pack": 1,
        "minimum_order_source": "N/A", "pricing_note": "nan",
    })
    assert r.status_code == 200, r.text
    s = _supplier(sku, sid)
    assert s["supplier_sku"] is None
    assert s["minimum_order_source"] is None
    assert s["pricing_note"] is None


def test_qty_without_uom_rejected_on_add():
    sku, sid = _seed("STU-4", "STUD")
    r = _client.post(f"/products/{sku}/suppliers", json={
        "supplier_id": sid, "basic_cost": 10.0, "units_per_pack": 1,
        "order_increment_qty": 6,   # no order_increment_uom
    })
    assert r.status_code == 400
    assert "UOM" in r.json()["detail"]


def test_qty_without_uom_rejected_on_patch():
    sku, sid = _seed("STU-5", "STUE")
    _client.post(f"/products/{sku}/suppliers", json={"supplier_id": sid, "basic_cost": 10.0, "units_per_pack": 1})
    ps_id = _supplier(sku, sid)["id"]
    r = _client.patch(f"/products/{sku}/suppliers/{ps_id}", json={"minimum_order_qty": 12})
    assert r.status_code == 400
    assert "UOM" in r.json()["detail"]


def test_patch_is_partial_make_primary_keeps_ordering_terms():
    sku, sid = _seed("STU-6", "STUF")
    _client.post(f"/products/{sku}/suppliers", json={
        "supplier_id": sid, "basic_cost": 120.0, "units_per_pack": 12,
        "order_increment_qty": 12, "order_increment_uom": "can", "pricing_note": "case of 12",
    })
    ps_id = _supplier(sku, sid)["id"]
    # a make-primary call sends only is_primary — it must not wipe the ordering terms/cost
    r = _client.patch(f"/products/{sku}/suppliers/{ps_id}", json={"is_primary": True})
    assert r.status_code == 200, r.text
    s = _supplier(sku, sid)
    assert s["order_increment_qty"] == 12 and s["order_increment_uom"] == "can"
    assert s["pricing_note"] == "case of 12"
    assert s["effective_unit_cost"] == 10.0            # 120 / 12 preserved


def test_patch_clears_field_with_blank():
    sku, sid = _seed("STU-7", "STUG")
    _client.post(f"/products/{sku}/suppliers", json={
        "supplier_id": sid, "basic_cost": 60.0, "units_per_pack": 6, "pricing_note": "temp note",
    })
    ps_id = _supplier(sku, sid)["id"]
    r = _client.patch(f"/products/{sku}/suppliers/{ps_id}", json={"pricing_note": ""})
    assert r.status_code == 200, r.text
    assert _supplier(sku, sid)["pricing_note"] is None


if __name__ == "__main__":
    main.app.dependency_overrides[require_user] = lambda: _FakeAdmin()   # fixtures don't run standalone
    for fn in [test_add_persists_ordering_terms_and_effective_cost,
               test_effective_cost_is_basic_cost_when_basis_units_one,
               test_placeholders_never_persist, test_qty_without_uom_rejected_on_add,
               test_qty_without_uom_rejected_on_patch,
               test_patch_is_partial_make_primary_keeps_ordering_terms,
               test_patch_clears_field_with_blank]:
        fn()
    print("supplier-terms UI contract OK: effective cost = basic / cost-basis units, ordering terms "
          "round-trip, placeholders drop to NULL, qty-without-UOM rejected, PATCH is partial")
