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
from services import tagging_service  # noqa: E402

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


def test_v1_import_endpoint_is_removed_with_410_tombstone():
    r = _client.post(
        "/catalogues/import",
        data={"supplier_id": "14"},
        files={"file": ("hills.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "ENDPOINT_REMOVED"
    assert "/catalogues/ingestions" in detail["message"]


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


