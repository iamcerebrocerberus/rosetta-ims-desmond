"""End-to-end for the catalogue re-parse endpoints (routers/reparse.py + reparse_service).

Covers: staging a diff (supplier / item / import scope), the cost preview, confirm applying the fix to
the right target (catalogue_item for pending, ProductSupplier for a committed SKU), the staleness
re-verify guard, placeholder scrub, and discard. Nothing writes live data until confirm.
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
    id, username, display_name, role = 5, "onboarder", "On Boarder", "admin"


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
SID = 51


def _reset():
    d = database.SessionLocal()
    try:
        for m in (models.ReparseChange, models.ReparseBatch, models.CatalogueItem, models.CatalogueImport,
                  models.ProductSupplier, models.Product, models.Supplier, models.AuditLog):
            d.query(m).delete()
        d.commit()
        d.add(models.Supplier(id=SID, code="KANGAR", name="Kangaroo Pet", created_at="2026-01-01"))
        d.add(models.CatalogueImport(id=1, filename="pl.pdf", format="pdf",
                                     imported_at="2026-01-01T00:00:00", status="review", item_count=2))
        d.commit()
    finally:
        d.close()


def _seed_committed(sku="RP-4KG", upp=4000, name="Air-Dried Dog Food 4kg", basic=1592.0):
    """A matched SKU: Product + ProductSupplier(upp) + a CatalogueItem pointing at it."""
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code=sku, name=name, category="Food", status="ACTIVE", storage_rule="any",
                           uom="bag", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="SD-1", basic_cost=basic,
                                     units_per_pack=upp, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        item = models.CatalogueItem(import_id=1, supplier_id=SID, raw_description=name, pack_size="4kg",
                                    uom="bag", units_per_pack=upp, supplier_sku="SD-1",
                                    matched_product_id=p.id, review_status="matched", skipped=0,
                                    created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        return p.sku_code, item.id
    finally:
        d.close()


def _seed_mismatch():
    """The Lignocaine case: a committed SKU whose live link took the WRONG supplier row — cost 403 / pack 20
    / sku LI4607 — while the catalogue item actually captured cost 75 / pack 1 / sku LI4600."""
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="LIGNO-100", name="Lignocaine 2% Injection 100ml", category="Medicine",
                           status="ACTIVE", storage_rule="any", uom="bottle",
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="LI4607", basic_cost=403.0,
                                     units_per_pack=20, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        item = models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Lignocaine 2% (20mg/ml) Injection",
                                    pack_size="100ml", uom="bottle", units_per_pack=1, cost_price=75.0,
                                    supplier_sku="LI4600", matched_product_id=p.id, review_status="matched",
                                    skipped=0, created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        return p.sku_code, item.id
    finally:
        d.close()


def _seed_per_unit_priced(pack_source="manual"):
    """Hill's case: the catalogue price is PER UNIT and the '24' is an order multiple (a case), not the
    cost-dividing pack size. The live SKU correctly models units_per_pack=1 with order_increment_qty=24,
    so the effective unit cost is 25.2 — not 25.2 / 24."""
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="HILLS-ONC", name="Hill's ONC Care Stew 2.9oz (min 24)", category="Food",
                           status="ACTIVE", storage_rule="any", uom="can",
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="607665", basic_cost=25.2,
                                     units_per_pack=1, pack_source=pack_source, order_increment_qty=24,
                                     minimum_order_qty=24, cost_source="manual", updated_at="2026-01-01T00:00:00"))
        item = models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Hill's ONC Care Chicken Stew",
                                    pack_size="24/2.9 oz", uom="can", units_per_pack=24, cost_price=25.2,
                                    supplier_sku="607665", matched_product_id=p.id, review_status="matched",
                                    skipped=0, created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        return p.sku_code, item.id
    finally:
        d.close()


def _seed_pending(desc="Artero Shampoo 5L", ps="5L", uom="ml", upp=5000, cost=690.0, supplier_sku="AC-1"):
    d = database.SessionLocal()
    try:
        item = models.CatalogueItem(import_id=1, supplier_id=SID, raw_description=desc, pack_size=ps, uom=uom,
                                    units_per_pack=upp, cost_price=cost, supplier_sku=supplier_sku,
                                    review_status="pending", skipped=0, created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        return item.id
    finally:
        d.close()


def _seed_supplier2(sid2=52, desc="Vet Kibble 2kg", upp=2000, cost=300.0):
    """A second supplier with its own pending item — for the 'switch supplier, keep both open' case."""
    d = database.SessionLocal()
    try:
        d.add(models.Supplier(id=sid2, code="ACME", name="Acme Vet", created_at="2026-01-01"))
        item = models.CatalogueItem(import_id=1, supplier_id=sid2, raw_description=desc, pack_size="2kg",
                                    uom="bag", units_per_pack=upp, cost_price=cost, supplier_sku="AV-1",
                                    review_status="pending", skipped=0, created_at="2026-01-01T00:00:00")
        d.add(item); d.commit()
        return sid2, item.id
    finally:
        d.close()


def _ps_upp(sku):
    d = database.SessionLocal()
    try:
        p = d.query(models.Product).filter_by(sku_code=sku).first()
        return d.query(models.ProductSupplier).filter_by(product_id=p.id).first().units_per_pack
    finally:
        d.close()


def _item_upp(iid):
    d = database.SessionLocal()
    try:
        return d.get(models.CatalogueItem, iid).units_per_pack
    finally:
        d.close()


def test_supplier_reparse_stages_diff_without_writing():
    _reset(); sku, cid = _seed_committed(); pid = _seed_pending()
    r = _client.post(f"/catalogues/reparse/supplier/{SID}")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["scope_type"] == "supplier" and b["status"] == "open"
    upp_changes = [c for c in b["changes"] if c["field"] == "units_per_pack"]
    assert len(upp_changes) == 2                       # the 4kg (committed) + the 5L (pending)
    by_old = {c["old_value"]: c for c in upp_changes}
    assert by_old["4000"]["new_value"] == "1" and by_old["4000"]["committed"] is True
    assert by_old["5000"]["new_value"] == "1" and by_old["5000"]["committed"] is False
    # every change carries its source catalogue file name
    assert all(c["source_file"] == "pl.pdf" for c in b["changes"])
    # cost preview present on the committed one: 1592/4000 -> 1592
    assert by_old["4000"]["affects_cost"] and round(by_old["4000"]["eff_cost_before"], 2) == 0.40
    assert by_old["4000"]["eff_cost_after"] == 1592.0
    # NOTHING applied yet
    assert _ps_upp(sku) == 4000 and _item_upp(pid) == 5000


def test_confirm_applies_to_correct_target_and_audits():
    _reset(); sku, cid = _seed_committed(); pid = _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    r = _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={"change_ids": []})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] == 2 and out["skipped"] == 0 and out["status"] == "applied"
    assert _ps_upp(sku) == 1          # committed → ProductSupplier updated
    assert _item_upp(pid) == 1        # pending → catalogue_item updated
    d = database.SessionLocal()
    try:
        assert d.query(models.AuditLog).filter_by(action="catalogue.reparse_apply").count() == 2
        # basic_cost untouched; pack_source flipped to manual on the committed link
        ps = d.query(models.ProductSupplier).filter_by(supplier_id=SID).first()
        assert ps.basic_cost == 1592.0 and ps.pack_source == "manual"
    finally:
        d.close()


def test_confirm_skips_stale_row():
    _reset(); pid = _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    # someone edits the item between staging and confirm
    d = database.SessionLocal()
    try:
        d.get(models.CatalogueItem, pid).units_per_pack = 4999
        d.commit()
    finally:
        d.close()
    out = _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={}).json()
    assert out["applied"] == 0 and out["skipped"] == 1
    assert _item_upp(pid) == 4999      # NOT overwritten
    assert [c for c in out["changes"]][0]["status"] == "stale"


def test_placeholder_scrub_is_a_change():
    _reset(); pid = _seed_pending(supplier_sku="#N/A", upp=1)   # upp=1 so the only diff is the scrub
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    scrub = [c for c in b["changes"] if c["field"] == "supplier_sku"]
    assert len(scrub) == 1 and scrub[0]["old_value"] == "#N/A" and scrub[0]["new_value"] is None
    _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={})
    d = database.SessionLocal()
    try:
        assert d.get(models.CatalogueItem, pid).supplier_sku is None
    finally:
        d.close()


def test_item_scope_by_sku():
    _reset(); sku, cid = _seed_committed()
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    assert b["scope_type"] == "item"
    assert any(c["field"] == "units_per_pack" and c["old_value"] == "4000" for c in b["changes"])


def test_discard_makes_no_writes():
    _reset(); pid = _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    r = _client.post(f"/catalogues/reparse/{b['id']}/discard")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _item_upp(pid) == 5000
    got = _client.get(f"/catalogues/reparse/{b['id']}").json()
    assert got["status"] == "discarded"
    assert all(c["status"] == "rejected" for c in got["changes"])


def test_items_payload_shows_all_fields_grouped():
    _reset(); pid = _seed_pending()   # Artero 5L, upp 5000 (changed), supplier_sku "AC-1" (clean)
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert len(b["items"]) == 1
    it = b["items"][0]
    assert it["catalogue_item_id"] == pid and it["source_file"] == "pl.pdf" and it["committed"] is False
    # the FULL captured field set is present, grouped — not just the changed one
    assert len(it["fields"]) == 17
    assert {f["group"] for f in it["fields"]} == {"Pricing", "Identity", "Pack & quantity", "Classification"}
    by = {f["field"]: f for f in it["fields"]}
    # the one changed field carries current/reparsed + change_id + cost preview
    upp = by["units_per_pack"]
    assert upp["changed"] and upp["current"] == "5000" and upp["reparsed"] == "1"
    assert upp["change_id"] is not None and upp["affects_cost"] is True
    # an unchanged field is shown for context (changed=False, no change_id)
    assert by["uom"]["changed"] is False and by["uom"]["change_id"] is None and by["uom"]["current"] == "ml"
    # per-item confirm targets exactly the pending changes
    assert it["change_ids"] == [upp["change_id"]] and it["changed_count"] == 1


def test_items_committed_card_reads_live_values():
    _reset(); sku, cid = _seed_committed()   # committed SKU: ps.units_per_pack=4000, product.uom="bag"
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    it = b["items"][0]
    assert it["committed"] is True and it["sku_code"] == sku
    by = {f["field"]: f for f in it["fields"]}
    # cost/pack current reads the LIVE ProductSupplier; reparsed is the corrected value
    assert by["units_per_pack"]["current"] == "4000" and by["units_per_pack"]["reparsed"] == "1"
    # a product-sourced context field reads the live Product value, unchanged
    assert by["uom"]["current"] == "bag" and by["uom"]["changed"] is False


def test_latest_returns_most_recent_open_batch():
    _reset()
    assert _client.get("/catalogues/reparse/latest").json()["batch"] is None   # none yet
    _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    latest = _client.get("/catalogues/reparse/latest").json()["batch"]
    assert latest is not None and latest["id"] == b["id"] and latest["status"] == "open"


def test_reparse_again_supersedes_same_sku():
    _reset(); _seed_pending()
    b1 = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    b2 = _client.post(f"/catalogues/reparse/supplier/{SID}").json()   # same SKU re-parsed again → newest wins
    assert b2["id"] != b1["id"]
    got1 = _client.get(f"/catalogues/reparse/{b1['id']}").json()
    assert got1["status"] == "superseded" and all(c["status"] == "superseded" for c in got1["changes"])
    assert _client.get(f"/catalogues/reparse/{b2['id']}").json()["status"] == "open"
    # only the newest is live in the inbox
    assert [b["id"] for b in _client.get("/catalogues/reparse/open").json()["batches"]] == [b2["id"]]


def test_switch_supplier_keeps_both_open():
    _reset(); _seed_pending()                       # supplier SID has a pending item
    sid2, _ = _seed_supplier2()                     # a different supplier with its own pending item
    b1 = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    b2 = _client.post(f"/catalogues/reparse/supplier/{sid2}").json()   # switch — must NOT discard b1
    assert _client.get(f"/catalogues/reparse/{b1['id']}").json()["status"] == "open"
    assert _client.get(f"/catalogues/reparse/{b2['id']}").json()["status"] == "open"
    ids = {b["id"] for b in _client.get("/catalogues/reparse/open").json()["batches"]}
    assert ids == {b1["id"], b2["id"]}              # both resumable, not just the latest
    only1 = _client.get(f"/catalogues/reparse/open?supplier={SID}").json()["batches"]   # filter by supplier
    assert [b["id"] for b in only1] == [b1["id"]] and only1[0]["supplier_name"] == "Kangaroo Pet"


def test_open_search_finds_reparsed_sku():
    _reset(); pid = _seed_pending(desc="Artero Shampoo 5L")
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    hits = _client.get("/catalogues/reparse/open?q=artero").json()["items"]
    assert len(hits) == 1 and hits[0]["catalogue_item_id"] == pid and hits[0]["batch_id"] == b["id"]
    assert _client.get("/catalogues/reparse/open?q=zzzznope").json()["items"] == []   # a miss returns nothing


def test_committed_recaptures_cost_and_pack():
    _reset(); sku, cid = _seed_mismatch()
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    by = {c["field"]: c for c in b["changes"]}
    # the fix: cost is recaptured now (was the bug), flagged cost-affecting with an effective-cost preview
    assert "cost_price" in by, f"cost not recaptured; fields={list(by)}"
    assert by["cost_price"]["old_value"] == "403.0" and by["cost_price"]["new_value"] == "75.0"
    assert by["cost_price"]["affects_cost"] is True
    assert round(by["cost_price"]["eff_cost_before"], 2) == 20.15   # 403 / 20
    assert by["cost_price"]["eff_cost_after"] == 75.0               # 75 / 1
    # pack + supplier_sku recaptured alongside it
    assert by["units_per_pack"]["old_value"] == "20" and by["units_per_pack"]["new_value"] == "1"
    assert by["supplier_sku"]["old_value"] == "LI4607" and by["supplier_sku"]["new_value"] == "LI4600"
    # nothing written yet (review-gated)
    d = database.SessionLocal()
    try:
        ps = d.query(models.ProductSupplier).filter_by(supplier_id=SID).first()
        assert ps.basic_cost == 403.0 and ps.units_per_pack == 20 and ps.supplier_sku == "LI4607"
    finally:
        d.close()


def test_confirm_recaptured_cost_writes_basic_cost_manual():
    _reset(); sku, cid = _seed_mismatch()
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    out = _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={}).json()
    assert out["status"] == "applied"
    d = database.SessionLocal()
    try:
        ps = d.query(models.ProductSupplier).filter_by(supplier_id=SID).first()
        assert ps.basic_cost == 75.0 and ps.units_per_pack == 1 and ps.supplier_sku == "LI4600"
        assert ps.cost_source == "manual"          # confirmed cost is protected from Sheet re-sync
        # a supplier-only write must bump the parent product so the live inventory delta-feed surfaces it
        prod = d.query(models.Product).filter_by(sku_code=sku).first()
        assert prod.updated_at != "2026-01-01T00:00:00"
        assert d.query(models.AuditLog).filter_by(action="catalogue.reparse_apply").count() >= 3
    finally:
        d.close()


def test_committed_empty_capture_never_nulls_live_cost():
    # catalogue item captured NO cost (cost_price None) → re-parse must not propose nulling the live 403
    _reset(); sku, cid = _seed_committed(sku="X1", upp=20, name="Widget", basic=403.0)
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    assert not any(c["field"] == "cost_price" for c in b["changes"])   # no spurious cost scrub


def test_per_unit_price_not_divided_by_order_multiple():
    # the catalogue '24' is an order multiple, not the pack size — re-parse must NOT override the manual
    # units_per_pack=1 (that would wrongly divide the per-unit 25.2 by 24)
    _reset(); sku, cid = _seed_per_unit_priced(pack_source="manual")
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    fields = {c["field"] for c in b["changes"]}
    assert "units_per_pack" not in fields, f"units_per_pack wrongly recaptured from an order multiple: {fields}"
    assert "cost_price" not in fields          # 25.2 == 25.2 → no spurious cost change, no /24


def test_order_multiple_signal_gates_even_without_manual_pack():
    # even a catalogue-sourced pack is protected when the catalogue count == the recorded order multiple
    _reset(); sku, cid = _seed_per_unit_priced(pack_source="catalogue")
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    assert "units_per_pack" not in {c["field"] for c in b["changes"]}


def test_no_phantom_pending_field_without_a_change():
    # a matched item with NO supplier link: its units_per_pack can't be staged (no ProductSupplier),
    # but the raw snapshot differs — it must NOT surface as a phantom "pending" ("Nothing to confirm").
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="PH-1", name="Shampoo", category="Food", status="ACTIVE",
                           storage_rule="any", uom="ml", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Shampoo 5L", pack_size="5L",
                                   uom="ml", units_per_pack=5000, ai_category="Pet Hygiene",
                                   matched_product_id=p.id, review_status="matched", skipped=0,
                                   created_at="2026-01-01T00:00:00"))   # NO ProductSupplier for (p, SID)
        d.commit()
    finally:
        d.close()
    it = _client.post("/catalogues/reparse/item/PH-1").json()["items"][0]
    upp = next(f for f in it["fields"] if f["field"] == "units_per_pack")
    assert upp["changed"] is False and upp["change_id"] is None      # unstageable → not a phantom change
    assert all(f["change_id"] is not None for f in it["fields"] if f["changed"])   # every 'changed' is confirmable
    assert it["change_ids"] == [f["change_id"] for f in it["fields"] if f["field"] == "category"]


def test_manual_cost_not_recaptured():
    # live cost is manual — re-parse must not propose overriding it with a (possibly stale) catalogue cost
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="MC-1", name="Manual Cost SKU", category="Food", status="ACTIVE",
                           storage_rule="any", uom="unit", rrp=25.0,
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="M1", basic_cost=17.6,
                                     units_per_pack=1, cost_source="manual", pack_source="manual",
                                     updated_at="2026-01-01T00:00:00"))
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Manual Cost SKU", pack_size="1",
                                   uom="unit", units_per_pack=1, cost_price=14.5, rrp=19.6, supplier_sku="M1",
                                   matched_product_id=p.id, review_status="matched", skipped=0,
                                   created_at="2026-01-01T00:00:00"))
        d.commit()
    finally:
        d.close()
    assert "cost_price" not in {c["field"] for c in _client.post("/catalogues/reparse/item/MC-1").json()["changes"]}


def test_cost_rrp_swap_not_recaptured():
    # a catalogue row with cost > rrp has its columns swapped — neither cost nor rrp is recaptured
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="SW-1", name="Swap SKU", category="Food", status="ACTIVE",
                           storage_rule="any", uom="unit", rrp=24.0,
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="S1", basic_cost=16.1,
                                     units_per_pack=1, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))   # catalogue cost → not cost-protected
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Swap SKU", pack_size="1",
                                   uom="unit", units_per_pack=1, cost_price=24.0, rrp=16.1, supplier_sku="S1",
                                   matched_product_id=p.id, review_status="matched", skipped=0,
                                   created_at="2026-01-01T00:00:00"))   # swapped: cost 24.0 > rrp 16.1
        d.commit()
    finally:
        d.close()
    fields = {c["field"] for c in _client.post("/catalogues/reparse/item/SW-1").json()["changes"]}
    assert "cost_price" not in fields and "rrp" not in fields


def test_captures_order_multiple_into_order_increment_qty():
    # per-unit pack (manual) + catalogue pack count 24 → capture the order multiple, not a pack change
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="OM-1", name="Case SKU", category="Food", status="ACTIVE",
                           storage_rule="any", uom="can", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="O1", basic_cost=25.2,
                                     units_per_pack=1, pack_source="manual", cost_source="manual",
                                     updated_at="2026-01-01T00:00:00"))   # order_increment_qty UNSET
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Case SKU", pack_size="24 cans",
                                   uom="can", units_per_pack=24, cost_price=25.2, supplier_sku="O1",
                                   matched_product_id=p.id, review_status="matched", skipped=0,
                                   created_at="2026-01-01T00:00:00"))
        d.commit()
    finally:
        d.close()
    b = _client.post("/catalogues/reparse/item/OM-1").json()
    by = {c["field"]: c for c in b["changes"]}
    assert "units_per_pack" not in by                                   # pack not touched (manual, per-unit)
    assert by["order_increment_qty"]["old_value"] is None and by["order_increment_qty"]["new_value"] == "24"
    _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={})     # confirm → captures the term + provenance
    d = database.SessionLocal()
    try:
        ps = d.query(models.ProductSupplier).filter_by(supplier_id=SID).first()
        assert ps.order_increment_qty == 24 and ps.order_increment_uom == "can"
        assert ps.minimum_order_source == "inferred_from_order_multiple"
    finally:
        d.close()


def test_reparse_dedupes_to_latest_catalogue_row_per_sku():
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="DUP-1", name="Dup SKU", category="Food", status="ACTIVE",
                           storage_rule="any", uom="unit", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="D1", basic_cost=10.0,
                                     units_per_pack=1, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        # two catalogue rows for the SAME product across imports — the newer row (cost 12) must win
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Dup SKU", uom="unit",
                                   units_per_pack=1, cost_price=8.0, supplier_sku="D1", matched_product_id=p.id,
                                   review_status="matched", skipped=0, created_at="2026-01-01T00:00:00"))
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Dup SKU", uom="unit",
                                   units_per_pack=1, cost_price=12.0, supplier_sku="D1", matched_product_id=p.id,
                                   review_status="matched", skipped=0, created_at="2026-02-01T00:00:00"))
        d.commit()
    finally:
        d.close()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert len(b["items"]) == 1                                    # one card for the SKU, not two
    by = {f["field"]: f for f in b["items"][0]["fields"]}
    assert by["cost_price"]["reparsed"] == "12.0"                  # the newer row won (not the older 8.0)


def test_over_matched_product_picks_its_own_row_not_the_newest():
    # a product over-matched with another SKU's row (the Hill's bug): dedup must pick the row whose
    # supplier_sku == the live link (its own row), NOT the newest (a different product's row).
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="OWN-1", name="c/d Urinary", category="Food", status="ACTIVE",
                           storage_rule="any", uom="can", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="3386", basic_cost=17.6,
                                     units_per_pack=1, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="c/d Urinary", uom="can",
                                   units_per_pack=1, cost_price=18.0, supplier_sku="3386", matched_product_id=p.id,
                                   review_status="matched", skipped=0, created_at="2026-01-01T00:00:00"))   # OWN, older, cost 18.0
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="i/d Adult", uom="can",
                                   units_per_pack=1, cost_price=41.0, supplier_sku="3389", matched_product_id=p.id,
                                   review_status="matched", skipped=0, created_at="2026-03-01T00:00:00"))   # OTHER, newer, cost 41.0
        d.commit()
    finally:
        d.close()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert len(b["items"]) == 1
    by = {f["field"]: f for f in b["items"][0]["fields"]}
    assert by["supplier_sku"]["changed"] is False                  # own row 3386 chosen → no false sku change
    cost = next(c for c in b["changes"] if c["field"] == "cost_price")
    assert cost["new_value"] == "18.0"                             # cost from the OWN row (18.0), NOT the 3389 row (41.0)


def test_single_mismatched_row_by_name_is_skipped():
    # a product with ONE matched row whose supplier_sku differs from the live link AND whose name is a
    # different product (c/d vs u/d) → re-parse must NOT change the live sku; the SKU is skipped.
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="NM-1", name="Hill's Prescription Diet Canine c/d Urinary Care 1.5kg",
                           category="Food", status="ACTIVE", storage_rule="any", uom="bag",
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="3386", basic_cost=17.6,
                                     units_per_pack=1, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Hill's Prescription Diet u/d Urinary Care 1.5kg",
                                   uom="bag", units_per_pack=1, cost_price=41.0, supplier_sku="7001",
                                   matched_product_id=p.id, review_status="matched", skipped=0,
                                   created_at="2026-02-01T00:00:00"))
        d.commit()
    finally:
        d.close()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert b["items"] == [] and b["ambiguous_skipped"] == 1        # name says different product → skipped


def test_single_row_name_matches_trusts_the_sku_fix():
    # ONE matched row, sku differs from a WRONG live link, but the NAME matches the product → this is the
    # legit stale-sku fix (Lignocaine shape) → re-parse proposes the supplier_sku correction.
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="NM-2", name="Hill's Prescription Diet Canine h/d Heart Care 1.5kg",
                           category="Food", status="ACTIVE", storage_rule="any", uom="bag",
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="10075HG", basic_cost=40.0,
                                     units_per_pack=1, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Hill's Prescription Diet h/d Heart Care 1.5kg",
                                   uom="bag", units_per_pack=1, cost_price=40.0, supplier_sku="10079HG",
                                   matched_product_id=p.id, review_status="matched", skipped=0,
                                   created_at="2026-02-01T00:00:00"))
        d.commit()
    finally:
        d.close()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert len(b["items"]) == 1
    sku = next(c for c in b["changes"] if c["field"] == "supplier_sku")
    assert sku["old_value"] == "10075HG" and sku["new_value"] == "10079HG"   # name matches → sku fix proposed


def test_ambiguous_over_match_is_skipped():
    # matched rows are several DIFFERENT skus, none == the live link → re-parse can't tell which is the
    # product's own row, so it skips the SKU entirely rather than corrupt it.
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="AMB-1", name="c/d Urinary Multi", category="Food", status="ACTIVE",
                           storage_rule="any", uom="can", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.ProductSupplier(product_id=p.id, supplier_id=SID, supplier_sku="3386", basic_cost=17.6,
                                     units_per_pack=1, cost_source="catalogue", pack_source="catalogue",
                                     updated_at="2026-01-01T00:00:00"))
        for sku, cost, ts in [("3389", 41.0, "2026-02-01"), ("7001", 38.1, "2026-03-01"), ("3384", 41.0, "2026-01-15")]:
            d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description=f"other {sku}", uom="can",
                                       units_per_pack=1, cost_price=cost, supplier_sku=sku, matched_product_id=p.id,
                                       review_status="matched", skipped=0, created_at=f"{ts}T00:00:00"))
        d.commit()
    finally:
        d.close()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert b["items"] == [] and b["ambiguous_skipped"] == 1        # skipped, not corrupted


def test_batch_labels_supplier_by_name():
    _reset(); _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    assert b["supplier_name"] == "Kangaroo Pet"          # header reads the name, not "Supplier #51"


def test_item_scope_has_no_supplier_name():
    _reset(); sku, _ = _seed_committed()
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    assert b["supplier_name"] is None                    # item scope labels off the product name


def test_empty_scope_404():
    _reset()
    assert _client.post(f"/catalogues/reparse/supplier/{SID}").status_code == 404


# ── Inline field edit (RP-5): correct a value before confirm ────────────────
def test_field_carries_editable_flag():
    _reset(); _seed_pending()
    it = _client.post(f"/catalogues/reparse/supplier/{SID}").json()["items"][0]
    by = {f["field"]: f for f in it["fields"]}
    assert by["cost_price"]["editable"] is True and by["brand"]["editable"] is True
    assert by["pack_size"]["editable"] is False        # item-only field → nothing to write it to


def test_edit_creates_change_and_confirm_writes_it():
    _reset(); pid = _seed_pending()                    # brand is None → an unchanged field
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    r = _client.put(f"/catalogues/reparse/{b['id']}/field",
                    json={"catalogue_item_id": pid, "field": "brand", "value": "Artero"})
    assert r.status_code == 200, r.text
    it = r.json()["item"]
    brand = next(f for f in it["fields"] if f["field"] == "brand")
    assert brand["changed"] is True and brand["reparsed"] == "Artero" and brand["change_id"] is not None
    assert brand["change_id"] in it["change_ids"]      # now confirmable
    _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={"change_ids": [brand["change_id"]]})
    d = database.SessionLocal()
    try:
        assert d.get(models.CatalogueItem, pid).brand == "Artero"   # the edited value is what got written
    finally:
        d.close()


def test_edit_updates_then_clears_a_change():
    _reset(); pid = _seed_pending()                    # upp 5000 → 1 (a staged change)
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    # correct the proposed value 1 → 2
    it = _client.put(f"/catalogues/reparse/{b['id']}/field",
                     json={"catalogue_item_id": pid, "field": "units_per_pack", "value": "2"}).json()["item"]
    assert next(f for f in it["fields"] if f["field"] == "units_per_pack")["reparsed"] == "2"
    # edit back to the current live value (5000) → the change is dropped (nothing to save)
    it2 = _client.put(f"/catalogues/reparse/{b['id']}/field",
                      json={"catalogue_item_id": pid, "field": "units_per_pack", "value": "5000"}).json()["item"]
    upp = next(f for f in it2["fields"] if f["field"] == "units_per_pack")
    assert upp["changed"] is False and upp["change_id"] is None and upp["change_id"] not in it2["change_ids"]


def test_edit_cost_refreshes_eff_preview():
    _reset(); sku, cid = _seed_committed()             # ps basic 1592 / upp 4000; batch proposes upp 4000→1
    b = _client.post(f"/catalogues/reparse/item/{sku}").json()
    it = _client.put(f"/catalogues/reparse/{b['id']}/field",
                     json={"catalogue_item_id": cid, "field": "cost_price", "value": "800"}).json()["item"]
    cost = next(f for f in it["fields"] if f["field"] == "cost_price")
    assert cost["changed"] is True and cost["reparsed"] == "800.0" and cost["affects_cost"] is True
    # preview reflects the edited cost AND the pending upp change (4000→1): 800 / 1 = 800
    assert cost["eff_cost_after"] == 800.0


def test_edit_non_editable_field_rejected():
    _reset(); pid = _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    r = _client.put(f"/catalogues/reparse/{b['id']}/field",
                    json={"catalogue_item_id": pid, "field": "pack_size", "value": "9L"})
    assert r.status_code == 400        # item-only, not recapturable → can't be saved to a SKU


def test_edit_onboards_missing_supplier_link():
    # a matched SKU with NO supplier link (a real gap): editing a supplier-level field is ALLOWED, and
    # confirming it ONBOARDS the link from the captured values instead of erroring 'no supplier link'.
    _reset()
    d = database.SessionLocal()
    try:
        p = models.Product(sku_code="ONB-1", name="Disp. Needle 25G", category="Not-For-Sale",
                           status="ACTIVE", storage_rule="any", uom="Box(es)",
                           created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00")
        d.add(p); d.flush()
        d.add(models.CatalogueItem(import_id=1, supplier_id=SID, raw_description="Disp. Needle 25G",
                                   pack_size="100 pcs/box", uom="box", units_per_pack=98, cost_price=41.0,
                                   supplier_sku="121272", matched_product_id=p.id, review_status="matched",
                                   skipped=0, created_at="2026-01-01T00:00:00"))   # NO ProductSupplier for (p, SID)
        d.commit()
    finally:
        d.close()
    b = _client.post("/catalogues/reparse/item/ONB-1").json()    # appears via the uom change (Box(es)->box)
    iid = b["items"][0]["catalogue_item_id"]
    r = _client.put(f"/catalogues/reparse/{b['id']}/field",
                    json={"catalogue_item_id": iid, "field": "units_per_pack", "value": "100"})
    assert r.status_code == 200, r.text                          # editing is allowed, not blocked
    upp = next(f for f in r.json()["item"]["fields"] if f["field"] == "units_per_pack")
    assert upp["changed"] is True and upp["reparsed"] == "100"
    _client.post(f"/catalogues/reparse/{b['id']}/confirm", json={"change_ids": [upp["change_id"]]})
    d = database.SessionLocal()
    try:
        p = d.query(models.Product).filter_by(sku_code="ONB-1").first()
        ps = d.query(models.ProductSupplier).filter_by(product_id=p.id, supplier_id=SID).first()
        assert ps is not None                                    # link onboarded
        assert ps.units_per_pack == 100                          # the edited pack
        assert ps.basic_cost == 41.0 and ps.supplier_sku == "121272"   # captured cost + sku
    finally:
        d.close()


def test_edit_rejected_on_closed_batch():
    _reset(); pid = _seed_pending()
    b = _client.post(f"/catalogues/reparse/supplier/{SID}").json()
    _client.post(f"/catalogues/reparse/{b['id']}/discard")
    r = _client.put(f"/catalogues/reparse/{b['id']}/field",
                    json={"catalogue_item_id": pid, "field": "brand", "value": "X"})
    assert r.status_code == 400        # closed re-parse → no edits


if __name__ == "__main__":
    main.app.dependency_overrides[require_user] = lambda: _FakeAdmin()
    for n, f in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        f(); print(f"  ok  {n}")
    print("reparse API: stage/confirm/stale/scrub/item-scope/discard verified")
