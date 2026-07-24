"""Guard for the units_per_pack WEIGHT-MIS-READ cleanup (throwaway temp DB — never prod).

Proves: preview is read-only; apply sets units_per_pack=1 on exactly the target rows and NOTHING else
(basic_cost, cost_source, order_increment_qty, minimum_order_qty, Product.min_purchase_qty all
untouched); pricing_note documents the weight mis-read; a `supplier_cost.units_per_pack_correction`
AuditLog row is written per change; a target in an unexpected state (wrong current upp / uom is a
weight unit / the weight token no longer in the name) aborts with ZERO writes; wrong
--expected-fix-count aborts; a second apply is idempotent; rollback restores literal old values.
Runs with and without PYTHONPATH.
"""
import os
import sys
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BACKEND_ROOT, "scripts"))
sys.path.insert(0, _BACKEND_ROOT)

import database   # noqa: E402
import models     # noqa: E402
import upp_weight_misread_cleanup as wm   # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

_TMP_CSV = os.path.join(tempfile.mkdtemp(), "wm_applied.csv")


def _aborts(fn, *a, **k) -> bool:
    try:
        fn(*a, **k); return False
    except SystemExit:
        return True


def _token(exp: int) -> str:
    """A kg/g name token whose gram value == exp (evidence the upp is a weight mis-read)."""
    return {1000: "1KG", 2500: "2.5KG", 4000: "4KG"}.get(exp, f"{exp}G")


def _reset_and_seed(overrides=None) -> None:
    """Seed the exact TARGET ps_ids. Each product name carries a kg/g token == its expected upp,
    a non-weight uom ('bag'), and units_per_pack == expected. `overrides[ps_id]` may set
    upp / name / uom to force a blocked state."""
    overrides = overrides or {}
    d = database.SessionLocal()
    try:
        for m in (models.CatalogueItem, models.CatalogueImport, models.ProductSupplier,
                  models.Product, models.SupplierBrand, models.SupplierAlias, models.Supplier,
                  models.AuditLog):
            d.query(m).delete()
        d.commit()
        d.add(models.Supplier(id=14, code="KANGAR", name="Kangaroo Pet", created_at="2026-01-01"))
        d.flush()
        pidn = 6000
        for psid, exp in wm.TARGETS.items():
            ov = overrides.get(psid, {})
            name = ov.get("name", f"Test Air-Dried Food - {_token(exp)}")
            d.add(models.Product(id=pidn, sku_code=f"SKU{psid}", name=name, category="Food",
                                 status="ACTIVE", storage_rule="any", uom=ov.get("uom", "bag"),
                                 min_purchase_qty=ov.get("mpq"),
                                 created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00"))
            d.flush()
            d.add(models.ProductSupplier(id=psid, product_id=pidn, supplier_id=14,
                                         supplier_sku=f"SS {psid}", basic_cost=100.0 + psid,
                                         units_per_pack=ov.get("upp", exp), cost_source="catalogue",
                                         cost_source_ref="catalogue_import:1", pack_source="catalogue",
                                         updated_at="2026-01-01T00:00:00"))
            pidn += 1
        d.commit()
    finally:
        d.close()


def _load(psid):
    d = database.SessionLocal()
    try:
        ps = d.get(models.ProductSupplier, psid)
        return {c: getattr(ps, c) for c in ("units_per_pack", "basic_cost", "order_increment_qty",
                "minimum_order_qty", "pricing_note", "cost_source")}
    finally:
        d.close()


def _snapshot():
    d = database.SessionLocal()
    try:
        return {ps.id: (ps.units_per_pack, ps.basic_cost, ps.order_increment_qty, ps.minimum_order_qty,
                        ps.pricing_note) for ps in d.query(models.ProductSupplier).all()}
    finally:
        d.close()


_N = len(wm.TARGETS)


def test_preview_read_only_and_all_ready():
    _reset_and_seed()
    before = _snapshot()
    rows = wm.preview()
    assert {r["state"] for r in rows} == {"READY"} and len(rows) == _N
    assert _snapshot() == before                      # read-only


def test_apply_sets_upp_1_only_and_preserves_everything_else():
    _reset_and_seed()
    res = wm.apply(operator="tester", expected_fix_count=_N, out=_TMP_CSV)
    assert res["changed"] == _N
    for psid in wm.TARGETS:
        r = _load(psid)
        assert r["units_per_pack"] == 1                        # corrected
        assert r["order_increment_qty"] is None                # NEVER set
        assert r["minimum_order_qty"] is None                  # NEVER set
        assert r["cost_source"] == "catalogue"                 # untouched
        assert r["basic_cost"] == 100.0 + psid                 # untouched
        _pn = (r["pricing_note"] or "").lower()
        assert r["pricing_note"] and "weight" in _pn and "mis-read" in _pn
    d = database.SessionLocal()
    try:
        assert d.query(models.AuditLog).filter_by(action="supplier_cost.units_per_pack_correction").count() == _N
    finally:
        d.close()


def test_current_upp_mismatch_aborts_with_zero_writes():
    _reset_and_seed(overrides={513: {"upp": 3999}})           # 513 not in expected state
    before = _snapshot()
    assert _aborts(wm.apply, operator="t", expected_fix_count=_N)
    assert _snapshot() == before


def test_weight_uom_target_aborts():
    _reset_and_seed(overrides={239: {"uom": "g"}})            # sold-by-weight uom -> BLOCKED
    before = _snapshot()
    assert _aborts(wm.apply, operator="t", expected_fix_count=_N)
    assert _snapshot() == before


def test_evidence_gone_aborts():
    _reset_and_seed(overrides={1062: {"name": "Amacin Eye & Ear Ointment"}})   # no weight token
    before = _snapshot()
    assert _aborts(wm.apply, operator="t", expected_fix_count=_N)
    assert _snapshot() == before


def test_wrong_expected_count_aborts():
    _reset_and_seed()
    before = _snapshot()
    assert _aborts(wm.apply, operator="t", expected_fix_count=_N - 1)
    assert _snapshot() == before


def test_second_apply_is_idempotent():
    _reset_and_seed()
    wm.apply(operator="t", expected_fix_count=_N)
    after_first = _snapshot()
    res = wm.apply(operator="t", expected_fix_count=_N)        # all DONE now
    assert res["changed"] == 0
    assert _snapshot() == after_first


def test_rollback_restores_literal_old_values():
    _reset_and_seed()
    before = {p: _load(p)["units_per_pack"] for p in wm.TARGETS}
    wm.apply(operator="t", expected_fix_count=_N, out=_TMP_CSV)
    wm.rollback_from_csv(_TMP_CSV, operator="t")
    for psid, old_upp in before.items():
        r = _load(psid)
        assert r["units_per_pack"] == old_upp                 # grams value restored
        assert r["pricing_note"] is None                      # note reverted to NULL


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn(); print(f"  ok  {name}")
    print("units_per_pack weight-mis-read cleanup: all behaviors verified (temp DB, no prod)")
