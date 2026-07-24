"""Guard for the Phase-0 units_per_pack SIZE-mis-read cleanup (throwaway temp DB — never prod).

Proves: preview is read-only and classifies HIGH vs REVIEW vs not-a-candidate (genuine counts excluded);
manifest-driven apply sets units_per_pack=1 on HIGH rows ONLY and nothing else (basic_cost, order fields,
cost_source untouched); REVIEW rows are held unless --include-review; a manifest row that drifted (upp
changed, or no longer a size-misread) aborts the whole run with ZERO writes; wrong --expected-fix-count
aborts; re-applying a stale manifest aborts (no double-write); rollback restores literal old values.
Runs with and without PYTHONPATH.
"""
import os, sys, tempfile, csv

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BACKEND_ROOT, "scripts"))
sys.path.insert(0, _BACKEND_ROOT)

import database   # noqa: E402
import models     # noqa: E402
import upp_size_misread_cleanup as sz   # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

_DIR = tempfile.mkdtemp()
_MANIFEST = os.path.join(_DIR, "preview.csv")
_APPLIED = os.path.join(_DIR, "applied.csv")

# ps_id -> (name, uom, units_per_pack, expected classification)
SEED = {
    7001: ("Test Antiseptic Solution 5L", "bottle", 5000, "HIGH"),     # volume size-misread
    7002: ("Test Digestive Powder 500g", "tub", 500, "HIGH"),          # weight size-misread
    7003: ("Test Medicated Shampoo 250ML", "pcs", 250, "REVIEW"),      # size-match but count uom
    7004: ("Test Antibiotic 100 tabs/bot", "tablet", 100, None),       # genuine COUNT -> not a candidate
    7005: ("Test Wet Food 24 cans/ctn", "can", 24, None),              # genuine COUNT -> not a candidate
}


def _aborts(fn, *a, **k):
    try:
        fn(*a, **k); return False
    except SystemExit:
        return True


def _reset_and_seed(overrides=None):
    overrides = overrides or {}
    d = database.SessionLocal()
    try:
        for m in (models.CatalogueItem, models.CatalogueImport, models.ProductSupplier,
                  models.Product, models.SupplierBrand, models.SupplierAlias, models.Supplier,
                  models.AuditLog):
            d.query(m).delete()
        d.commit()
        d.add(models.Supplier(id=21, code="TSTSUP", name="Test Supplier", created_at="2026-01-01"))
        d.flush()
        pidn = 7100
        for psid, (name, uom, upp, _) in SEED.items():
            ov = overrides.get(psid, {})
            d.add(models.Product(id=pidn, sku_code=f"SKU{psid}", name=ov.get("name", name), category="Supplement",
                                 status="ACTIVE", storage_rule="any", uom=ov.get("uom", uom),
                                 created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00"))
            d.flush()
            d.add(models.ProductSupplier(id=psid, product_id=pidn, supplier_id=21, supplier_sku=f"SS{psid}",
                                         basic_cost=100.0 + psid, units_per_pack=ov.get("upp", upp),
                                         cost_source="catalogue", cost_source_ref="catalogue_import:1",
                                         pack_source="catalogue", updated_at="2026-01-01T00:00:00"))
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
        return {ps.id: (ps.units_per_pack, ps.basic_cost, ps.pricing_note)
                for ps in d.query(models.ProductSupplier).all()}
    finally:
        d.close()


def test_preview_classifies_and_is_read_only():
    _reset_and_seed()
    before = _snapshot()
    hits = sz.preview(out=_MANIFEST)
    by = {h[0]["id"]: h[2] for h in hits}
    assert by == {7001: "HIGH", 7002: "HIGH", 7003: "REVIEW"}   # counts (7004/7005) excluded
    assert _snapshot() == before                                 # read-only
    rows = {int(r["id"]): r for r in csv.DictReader(open(_MANIFEST))}
    assert rows[7001]["confidence"] == "HIGH" and rows[7003]["confidence"] == "REVIEW"


def test_apply_high_only_sets_upp_1_and_preserves_rest():
    _reset_and_seed()
    sz.preview(out=_MANIFEST)
    res = sz.apply(_MANIFEST, operator="tester", expected_fix_count=2, out=_APPLIED)
    assert res["changed"] == 2
    for psid in (7001, 7002):                       # HIGH -> corrected
        r = _load(psid)
        assert r["units_per_pack"] == 1
        assert r["order_increment_qty"] is None and r["minimum_order_qty"] is None
        assert r["cost_source"] == "catalogue" and r["basic_cost"] == 100.0 + psid
        assert r["pricing_note"] and "mis-read" in r["pricing_note"].lower()
    assert _load(7003)["units_per_pack"] == 250     # REVIEW held
    assert _load(7004)["units_per_pack"] == 100     # genuine count untouched
    assert _load(7005)["units_per_pack"] == 24
    d = database.SessionLocal()
    try:
        assert d.query(models.AuditLog).filter_by(action="supplier_cost.units_per_pack_correction").count() == 2
    finally:
        d.close()


def test_include_review_also_fixes_review_rows():
    _reset_and_seed()
    sz.preview(out=_MANIFEST)
    sz.apply(_MANIFEST, operator="t", expected_fix_count=3, out=_APPLIED, include_review=True)
    assert _load(7003)["units_per_pack"] == 1       # REVIEW now applied


def test_drifted_row_aborts_zero_writes():
    _reset_and_seed()
    sz.preview(out=_MANIFEST)
    _reset_and_seed(overrides={7001: {"upp": 4999}})   # 7001 upp changed since preview
    before = _snapshot()
    assert _aborts(sz.apply, _MANIFEST, operator="t", expected_fix_count=2)
    assert _snapshot() == before


def test_wrong_expected_count_aborts():
    _reset_and_seed()
    sz.preview(out=_MANIFEST)
    before = _snapshot()
    assert _aborts(sz.apply, _MANIFEST, operator="t", expected_fix_count=5)
    assert _snapshot() == before


def test_reapply_stale_manifest_aborts():
    _reset_and_seed()
    sz.preview(out=_MANIFEST)
    sz.apply(_MANIFEST, operator="t", expected_fix_count=2, out=_APPLIED)
    after = _snapshot()
    # same manifest again: rows are now upp=1, so old_units_per_pack no longer matches -> abort, no double write
    assert _aborts(sz.apply, _MANIFEST, operator="t", expected_fix_count=2)
    assert _snapshot() == after


def test_rollback_restores_literal_old_values():
    _reset_and_seed()
    sz.preview(out=_MANIFEST)
    sz.apply(_MANIFEST, operator="t", expected_fix_count=2, out=_APPLIED)
    sz.rollback_from_csv(_APPLIED, operator="t")
    assert _load(7001)["units_per_pack"] == 5000 and _load(7002)["units_per_pack"] == 500
    assert _load(7001)["pricing_note"] is None


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn(); print(f"  ok  {name}")
    print("Phase-0 size-mis-read cleanup: all behaviors verified (temp DB, no prod)")
