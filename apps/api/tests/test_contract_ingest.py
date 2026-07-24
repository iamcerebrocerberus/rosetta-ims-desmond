"""Supplier-source contract ingestion wiring.

Extraction is monkeypatched, but contract selection/enforcement uses the real
Pydantic-backed supplier-source runtime adapter.
"""

import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import pytest        # noqa: E402
import database      # noqa: E402
import models        # noqa: E402
import main          # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402
from dependencies import require_user               # noqa: E402
from services import extraction_service, tagging_service  # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


class _Admin:
    id, username, display_name, role = 9, "onboarder", "On Boarder", "admin"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    prev = main.app.dependency_overrides.get(require_user)
    main.app.dependency_overrides[require_user] = lambda: _Admin()
    monkeypatch.setattr(
        tagging_service,
        "suggest_tags",
        lambda items: [{"tags": [], "category": None, "subcategory": None} for _ in items],
    )
    yield
    if prev is None:
        main.app.dependency_overrides.pop(require_user, None)
    else:
        main.app.dependency_overrides[require_user] = prev


_client = TestClient(main.app)


def _supplier(sid, code, name):
    d = database.SessionLocal()
    try:
        if not d.get(models.Supplier, sid):
            d.add(models.Supplier(id=sid, code=code, name=name, created_at="2026-01-01"))
            d.commit()
    finally:
        d.close()


def _items(import_id):
    d = database.SessionLocal()
    try:
        return {it.supplier_sku: it for it in d.query(models.CatalogueItem).filter_by(import_id=import_id).all()}
    finally:
        d.close()


def test_supported_source_contract_import_guides_enforces_and_flags(monkeypatch):
    _supplier(14, "HILLS", "Hill's")

    def fake_extract(content, filename, content_type, contract=None):
        assert contract is not None
        assert contract.slug == "hills.price_list.v1"
        assert "SUPPLIER SOURCE CONTRACT" in contract.prompt_section()
        return (
            [
                {
                    "supplier_sku": "10447",
                    "description": "Healthy Cuisine",
                    "cost_price": 13.10,
                    "rrp": 18.0,
                    "units_per_pack": 24,
                    "order_increment_qty": 24,
                    "brand": None,
                    "pack_size": "24/2.9 oz",
                },
                {
                    "supplier_sku": "SWAP1",
                    "description": "Swapped row",
                    "cost_price": 25.0,
                    "rrp": 17.6,
                    "units_per_pack": 24,
                    "order_increment_qty": 24,
                    "brand": None,
                },
            ],
            "pdf",
        )

    monkeypatch.setattr(extraction_service, "extract", fake_extract)

    r = _client.post(
        "/catalogues/import",
        data={"supplier_id": "14"},
        files={"file": ("hills.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contract"] == "hills.price_list.v1@v1"
    assert body["contract_flags"] == 1

    it = _items(body["import_id"])
    assert it["10447"].units_per_pack == 1
    assert it["10447"].brand == "Hill's"
    assert it["10447"].ai_category == "Food"
    assert it["10447"].weight_grams == round(2.9 * 28.3495)
    assert it["10447"].confidence_detail is None
    assert "HILLS_COST_NOT_BELOW_RRP" in it["SWAP1"].confidence_detail


def test_reparse_derivation_applies_supported_source_contract():
    from services import reparse_service

    item = models.CatalogueItem(
        supplier_id=14,
        raw_description="Hill's Can 2.8oz",
        pack_size="24/2.9 oz",
        uom="can",
        units_per_pack=24,
        cost_price=13.1,
        rrp=18.0,
        supplier_sku="10447",
        species="cat",
        weight_grams=None,
    )

    out = reparse_service.derive(item)

    assert out["units_per_pack"] == 1
    assert out["brand"] == "Hill's"
    assert out["category"] == "Food"
    assert out["weight_grams"] == round(2.9 * 28.3495)


def test_reparse_derive_unchanged_for_uncontracted_supplier():
    from services import reparse_service

    item = models.CatalogueItem(
        supplier_id=77,
        raw_description="Widget",
        pack_size="1",
        uom="unit",
        units_per_pack=5,
        cost_price=10.0,
        brand="Acme",
    )

    out = reparse_service.derive(item)

    assert out.get("brand") == "Acme"


def test_reparse_contracted_bypasses_manual_cost_and_pack_gates():
    from types import SimpleNamespace
    from services import reparse_service as rp

    ps = SimpleNamespace(
        cost_source="manual",
        pack_source="manual",
        units_per_pack=1,
        order_increment_qty=None,
        minimum_order_qty=None,
        uom_verified_at=None,
    )
    clean = SimpleNamespace(cost_price=13.1, rrp=18.0)
    cand = {"cost_price": 13.1, "units_per_pack": 1}

    assert rp._candidate(cand, "cost_price", True, 25.0, ps, clean, contracted=False) == 25.0
    assert rp._candidate(cand, "units_per_pack", True, 24, ps, clean, contracted=False) == 24
    assert rp._candidate(cand, "cost_price", True, 25.0, ps, clean, contracted=True) == 13.1
    assert rp._candidate(cand, "units_per_pack", True, 24, ps, clean, contracted=True) == 1


def test_reparse_swap_guard_holds_even_when_source_contracted():
    from types import SimpleNamespace
    from services import reparse_service as rp

    ps = SimpleNamespace(
        cost_source="manual",
        pack_source="manual",
        units_per_pack=1,
        order_increment_qty=None,
        minimum_order_qty=None,
        uom_verified_at=None,
    )
    swapped = SimpleNamespace(cost_price=25.0, rrp=17.6)
    cand = {"cost_price": 25.0}

    assert rp._candidate(cand, "cost_price", True, 16.7, ps, swapped, contracted=True) == 16.7


def test_contract_drift_flags_stale_source_contract(monkeypatch):
    _supplier(14, "HILLS", "Hill's")

    def fake_extract(content, filename, content_type, contract=None):
        assert contract is not None and contract.slug == "hills.price_list.v1"
        return (
            [
                {
                    "supplier_sku": f"S{i}",
                    "description": "x",
                    "cost_price": 100.0,
                    "rrp": 50.0,
                    "units_per_pack": 1,
                    "order_increment_qty": 1,
                }
                for i in range(6)
            ],
            "pdf",
        )

    monkeypatch.setattr(extraction_service, "extract", fake_extract)

    r = _client.post(
        "/catalogues/import",
        data={"supplier_id": "14"},
        files={"file": ("hills.pdf", b"%PDF", "application/pdf")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contract_flags"] == 6
    assert body["contract_stale"] is True


def test_uncontracted_import_is_unchanged(monkeypatch):
    _supplier(77, "GEN", "Generic Supplier")

    def fake_extract(content, filename, content_type, contract=None):
        assert contract is None
        return ([{"supplier_sku": "G1", "description": "Widget", "cost_price": 10.0, "units_per_pack": 5}], "pdf")

    monkeypatch.setattr(extraction_service, "extract", fake_extract)

    r = _client.post(
        "/catalogues/import",
        data={"supplier_id": "77"},
        files={"file": ("g.pdf", b"%PDF fake", "application/pdf")},
    )

    assert r.status_code == 200, r.text
    assert r.json()["contract"] is None
    assert r.json()["contract_flags"] == 0
    assert _items(r.json()["import_id"])["G1"].units_per_pack == 5
