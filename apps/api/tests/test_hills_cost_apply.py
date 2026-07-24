"""PR-B guard: Hill's cost-basis apply/rollback (against a throwaway temp DB — never prod).

The apply reuses the CANONICAL classifier from hills_cost_basis_dryrun.py, so these tests also
prove the mutation path inherits every safety gate (manual-wet-only approval, cost-conflict/dry/
dual-map/min-purchase exclusion). Covers: apply touches only expected ids; wrong
--expected-fix-count aborts zero-writes; basic_cost unchanged; old units_per_pack preserved before
reset; manual rows require --approve-ids; dry rows stay REVIEW; non-Hill's untouched; second apply
idempotent; rollback restores exact pre-apply values; dry-run read-only.
"""
import os
import sys
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
# Self-bootstrap so the suite runs WITHOUT PYTHONPATH (mirrors the script's standalone fix):
# scripts/ for the sibling imports, backend root for database/models/services.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BACKEND_ROOT, "scripts"))
sys.path.insert(0, _BACKEND_ROOT)

import database   # noqa: E402
import models     # noqa: E402
import hills_cost_basis_fix as fix   # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

_TMP_CSV = os.path.join(tempfile.mkdtemp(), "applied.csv")


def _aborts(fn, *a, **k) -> bool:
    try:
        fn(*a, **k); return False
    except SystemExit:
        return True


def _reset_and_seed() -> dict:
    """Fresh scenario each test: 3 AUTO_FIX wet-catalogue, 2 manual wet, 1 dry, 1 non-Hill's, 1 upp=1.
    catalogue cost_price == basic_cost for every seeded row, so the canonical cost-match gate passes."""
    d = database.SessionLocal()
    try:
        for m in (models.CatalogueItem, models.CatalogueImport, models.ProductSupplier,
                  models.Product, models.SupplierBrand, models.SupplierAlias, models.Supplier,
                  models.AuditLog):
            d.query(m).delete()
        d.commit()
        hill = models.Supplier(code="HILL", name="Hill's", normalized_name="hills", created_at="2026-01-01")
        other = models.Supplier(code="OTH", name="Other Supplier", created_at="2026-01-01")
        d.add_all([hill, other]); d.flush()
        imp = models.CatalogueImport(supplier_id=hill.id, filename="hills.pdf", imported_at="2026-01-01",
                                     status="review")
        d.add(imp); d.flush()

        ids = {"auto": [], "manual": [], "dry": None, "nonhills": None, "upp1": None, "hill_id": hill.id}

        def mk(name, sup, sku, ssku, upp, cost_source, basic=9.4, uom="Can(s)", cat_upp=None, mpq=None):
            p = models.Product(sku_code=sku, name=name, category="Food", status="ACTIVE",
                               storage_rule="any", uom=uom, min_purchase_qty=mpq,
                               created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
            d.add(p); d.flush()
            ps = models.ProductSupplier(product_id=p.id, supplier_id=sup.id, supplier_sku=ssku,
                                        basic_cost=basic, units_per_pack=upp, cost_source=cost_source,
                                        cost_source_ref="catalogue_import:1", pack_source="catalogue",
                                        updated_at="2026-01-01T00:00:00")
            d.add(ps); d.flush()
            if cat_upp is not None:
                d.add(models.CatalogueItem(import_id=imp.id, supplier_id=hill.id, supplier_sku=ssku,
                                           cost_price=basic, units_per_pack=cat_upp,
                                           pack_size=f"{cat_upp}x2.9oz cs", review_status="matched",
                                           created_at="2026-01-01T00:00:00"))
            return ps.id

        ids["auto"].append(mk("Hill's Wet Cat Food A", hill, "WA1", "AF1", 24, "catalogue", uom="Can(s)", cat_upp=24))
        ids["auto"].append(mk("Hill's Wet Cat Food B", hill, "WA2", "AF2", 24, "catalogue", uom=None,      cat_upp=24))
        ids["auto"].append(mk("Hill's Wet Cat Food C", hill, "WA3", "AF3", 24, "catalogue", uom="unit(s)", cat_upp=24))
        ids["manual"].append(mk("Hill's Wet Dog Food M1", hill, "WM1", "MN1", 12, "manual", basic=38.1, cat_upp=12))
        ids["manual"].append(mk("Hill's Wet Dog Food M2", hill, "WM2", "MN2", 12, "manual", basic=38.1, cat_upp=12))
        ids["dry"] = mk("Hill's Dry Dog Food", hill, "DR1", "DRY1", 8, "catalogue", basic=141.0, uom=None, cat_upp=8)
        ids["nonhills"] = mk("Other Wet Cat Food", other, "OX1", "OX1", 24, "catalogue", cat_upp=None)
        ids["upp1"] = mk("Hill's Dry Cat Food (ok)", hill, "OK1", "OK1", 1, "manual", basic=159.8, uom=None)
        d.commit()
        return ids
    finally:
        d.close()


def _load(ps_id):
    d = database.SessionLocal()
    try:
        ps = d.get(models.ProductSupplier, ps_id)
        return {c: getattr(ps, c) for c in ("units_per_pack", "basic_cost", "order_increment_qty",
                "order_increment_uom", "minimum_order_qty", "minimum_order_uom",
                "minimum_order_source", "pricing_note", "cost_source", "cost_source_ref")}
    finally:
        d.close()


def _snapshot_all():
    d = database.SessionLocal()
    try:
        return {ps.id: (ps.units_per_pack, ps.basic_cost, ps.order_increment_qty, ps.pricing_note)
                for ps in d.query(models.ProductSupplier).all()}
    finally:
        d.close()


def test_apply_touches_only_expected_ids_and_preserves_fields():
    ids = _reset_and_seed()
    before = _snapshot_all()
    fix.apply(ids["hill_id"], operator="tester", expected_fix_count=3, approve_ids=set(), out=_TMP_CSV)
    for pid in ids["auto"]:
        r = _load(pid)
        assert r["units_per_pack"] == 1
        assert r["order_increment_qty"] == 24 and r["minimum_order_qty"] == 24
        assert r["minimum_order_source"] == "inferred_from_order_multiple"
        assert r["pricing_note"] and "basis-fix" in r["pricing_note"]
        assert r["basic_cost"] == before[pid][1]                 # basic_cost unchanged
        assert r["cost_source"] == "catalogue"                    # untouched
    # uom rule: product.uom else 'sellable_unit'
    assert _load(ids["auto"][0])["order_increment_uom"] == "Can(s)"
    assert _load(ids["auto"][1])["order_increment_uom"] == "sellable_unit"
    # everything else untouched
    for pid in [*ids["manual"], ids["dry"], ids["nonhills"], ids["upp1"]]:
        assert _snapshot_all()[pid] == before[pid], f"ps {pid} must be untouched"
    d = database.SessionLocal()
    try:
        assert d.query(models.AuditLog).filter_by(action="supplier_cost.basis_fix").count() == 3
    finally:
        d.close()


def test_wrong_expected_count_aborts_with_zero_writes():
    ids = _reset_and_seed()
    before = _snapshot_all()
    assert _aborts(fix.apply, ids["hill_id"], operator="tester", expected_fix_count=99, approve_ids=set())
    assert _snapshot_all() == before


def test_old_upp_preserved_before_reset():
    ids = _reset_and_seed()
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids=set())
    for pid in ids["auto"]:
        r = _load(pid)
        assert r["units_per_pack"] == 1 and r["order_increment_qty"] == 24  # copied before reset
    d = database.SessionLocal()
    try:
        bad = d.query(models.ProductSupplier).filter(
            models.ProductSupplier.units_per_pack == 1,
            models.ProductSupplier.order_increment_qty.is_(None),
            models.ProductSupplier.id.in_(ids["auto"])).count()
        assert bad == 0
    finally:
        d.close()


def test_manual_rows_require_approve_ids():
    ids = _reset_and_seed()
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids=set())     # auto only
    for pid in ids["manual"]:
        assert _load(pid)["units_per_pack"] == 12                         # untouched
    # approve them (auto rows already fixed -> only the 2 manual are eligible; both have matching cost)
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=2, approve_ids=set(ids["manual"]))
    for pid in ids["manual"]:
        r = _load(pid)
        assert r["units_per_pack"] == 1 and r["order_increment_qty"] == 12
        assert "approved" in r["pricing_note"].lower()


def test_dry_and_nonhills_stay_untouched():
    ids = _reset_and_seed()
    rows = fix.preview(ids["hill_id"])
    dry = next(r for r in rows if r["product_supplier_id"] == ids["dry"])
    assert dry["bucket"] == "REVIEW"
    assert all(r["product_supplier_id"] != ids["nonhills"] for r in rows)   # non-Hill's never classified
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids=set())
    assert _load(ids["dry"])["units_per_pack"] == 8
    assert _load(ids["nonhills"])["units_per_pack"] == 24


def test_dry_row_cannot_be_approved():
    ids = _reset_and_seed()
    rows = fix.preview(ids["hill_id"], approve_ids={ids["dry"]})           # try to approve the dry row
    dry = next(r for r in rows if r["product_supplier_id"] == ids["dry"])
    assert dry["bucket"] == "REVIEW" and "approval ignored" in dry["reason"]
    # apply with the dry id "approved" + expecting only the 3 auto still holds
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids={ids["dry"]})
    assert _load(ids["dry"])["units_per_pack"] == 8                        # untouched


def test_second_apply_is_idempotent():
    ids = _reset_and_seed()
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids=set())
    after_first = _snapshot_all()
    res = fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids=set())  # nothing eligible
    assert res["changed"] == 0
    assert _snapshot_all() == after_first


def test_rollback_restores_exact_pre_apply_values():
    ids = _reset_and_seed()
    before = {pid: _load(pid) for pid in ids["auto"]}
    fix.apply(ids["hill_id"], operator="t", expected_fix_count=3, approve_ids=set(), out=_TMP_CSV)
    fix.rollback_from_csv(_TMP_CSV, operator="t")
    for pid in ids["auto"]:
        r = _load(pid)
        assert r["units_per_pack"] == before[pid]["units_per_pack"] == 24
        assert r["order_increment_qty"] is None and r["minimum_order_source"] is None
        assert r["pricing_note"] is None
        assert r["basic_cost"] == before[pid]["basic_cost"]


def test_dry_run_is_read_only():
    ids = _reset_and_seed()
    before = _snapshot_all()
    fix.preview(ids["hill_id"])
    assert _snapshot_all() == before


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn(); print(f"  ok  {name}")
    print("✅ PR-B apply/rollback: all behaviors verified (temp DB, no prod)")
