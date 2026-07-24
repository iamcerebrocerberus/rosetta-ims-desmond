"""Guards for SF Express logistics attribution and MBB cost-to-hit.

- shopify_logistics reproduces the SF Speedy Express HK-domestic rate card (weight billed rounded
  up to 0.5kg (<=5kg) then 1kg).
- _pack_sell_unit_delivery charges SF once per PARCEL (a pack ships together) and splits it across
  the pack's units; single-unit packs are unchanged.
- _cost_to_hit_mbb returns the real cash outlay to unlock a term, by term kind.
"""
import os
import sys
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_ROOT)

import database                                 # noqa: E402
import models                                   # noqa: E402
from services import pricing_service as P       # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


def test_sf_rates_match_sheet():
    # SF Speedy Express, HK domestic, Limited-Time-Offer key tiers
    assert P.shopify_logistics(500) == 20.0
    assert P.shopify_logistics(600) == 30.0      # 0.6kg -> billed 1kg -> 30
    assert P.shopify_logistics(1000) == 30.0
    assert P.shopify_logistics(2500) == 51.0
    assert P.shopify_logistics(3000) == 58.0     # 3–5kg flat 58
    assert P.shopify_logistics(5000) == 58.0
    assert P.shopify_logistics(6000) == 68.0     # 6–10kg flat 68
    assert P.shopify_logistics(12000) == 78.0    # 11–15kg flat 78
    assert P.shopify_logistics(20000) == 88.0    # 16–20kg flat 88
    assert P.shopify_logistics(21000) == 294.0   # >20kg reverts to per-kg original
    assert P.shopify_logistics(None) == 58.0     # weight not recorded -> default
    assert P.shopify_logistics(0) == 58.0


def test_margin_still_subtracts_per_unit_logistics():
    # The margin formula is unchanged — it subtracts whatever per-sell-unit delivery it's given.
    # 500g -> SF $20; (100 - 40 - 20) / 100 = 0.40
    assert P._channel_margin(100.0, 40.0, None, P.shopify_logistics(500)) == 0.40


def test_sf_spread_over_pack():
    # Single-unit pack: the sell-unit IS the parcel -> unchanged from the old per-unit charge.
    assert P._pack_sell_unit_delivery(500, 1) == 20.0
    assert P._pack_sell_unit_delivery(2500, 1) == 51.0
    assert P._pack_sell_unit_delivery(2500, None) == 51.0
    # Box of 12 pouches @ 85g each: parcel weight 1020g -> SF $37, split over 12 = ~$3.08/pouch
    # (the fix: not $30+ charged to a single $18 pouch).
    assert round(P._pack_sell_unit_delivery(85, 12), 4) == round(P.shopify_logistics(1020) / 12, 4)
    assert P._pack_sell_unit_delivery(85, 12) < P._pack_sell_unit_delivery(85, 1)
    # Box of 24 cans @ 85g: parcel 2040g -> SF $51, /24 = ~$2.13/can.
    assert round(P._pack_sell_unit_delivery(85, 24), 4) == round(P.shopify_logistics(2040) / 24, 4)


class _Term:
    """Minimal stand-in for an MBB term row (only the fields _cost_to_hit_mbb reads)."""
    def __init__(self, kind, min_qty=None, min_spend=None):
        self.kind, self.min_qty, self.min_spend = kind, min_qty, min_spend


def test_cost_to_hit_mbb_by_term_kind():
    # buy_x_get_y: pay BASIC price for the paid units (Frontline "buy 10 get 3 free": 10 × $215).
    assert P._cost_to_hit_mbb(_Term("buy_x_get_y", min_qty=10), 215.0, 165.38) == 2150.0
    # spend_discount: the spend threshold itself (Dermoscent "spend $1000 → 10% off").
    assert P._cost_to_hit_mbb(_Term("spend_discount", min_spend=1000.0), 156.0, 140.4) == 1000.0
    # tier / flat: min_qty at the achieved (discounted) cost (Advocate "12 @ 20% off": 12 × $217.6).
    assert P._cost_to_hit_mbb(_Term("tier", min_qty=12), 272.0, 217.6) == 2611.0
    # no term -> no value.
    assert P._cost_to_hit_mbb(None, 100.0, 90.0) is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
