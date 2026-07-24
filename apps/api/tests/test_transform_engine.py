"""Phase A guard for the config-driven transformation engine.

Two things are proven here, against a throwaway temp DB (never prod):
  1. **Shadow equivalence** — for every transformation, the engine reproduces the previously
     hard-coded formula EXACTLY across a wide input matrix (incl. edge cases: None, 0, negative,
     pack=1, boundary weights). This is the behaviour-neutral gate: seeding the config changes
     no number.
  2. **Sandbox safety** — the formula evaluator rejects anything outside the arithmetic
     allow-list (attribute access, imports, lambdas, comprehensions, other calls, unknown names).
"""
import os
import sys
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_ROOT)

import pytest                                  # noqa: E402
import database                                # noqa: E402
import models                                  # noqa: E402
from services import transform_engine as engine        # noqa: E402
from services import pricing_service as P               # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


# ── Sandbox safety ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("expr", [
    "__import__('os').system('x')",   # import / arbitrary call
    "().__class__",                    # tuple + attribute
    "price.__class__",                 # attribute access
    "getattr(price, 'x')",             # disallowed builtin
    "open('/etc/passwd')",             # disallowed builtin
    "[x for x in (1, 2)]",             # comprehension
    "(lambda: 1)()",                   # lambda
    "pow(price, 2)",                   # function not on the allow-list
    "price if True else evil",         # unknown name 'evil'
    "price + secret",                  # unknown name 'secret'
])
def test_sandbox_rejects_unsafe(expr):
    with pytest.raises(Exception):
        engine.eval_formula(expr, {"price": 1.0})


def test_sandbox_allows_safe_arithmetic():
    assert engine.eval_formula("round((price - cost) / price, 4)", {"price": 10.0, "cost": 4.0}) == 0.6
    assert engine.eval_formula("min(a, b) + max(a, b)", {"a": 2, "b": 5}) == 7
    assert engine.eval_formula("x if x > 1 else 0", {"x": 0}) == 0


# ── Shadow equivalence (engine == the hard-coded formula it replaced) ─────────────────────
_COSTS   = [None, 0, 0.01, 9.4, 100.0, 141.0]
_PACKS   = [None, 0, 1, 2, 8, 12, 24, 100]
_PRICES  = [None, 0, -5, 0.5, 10.0, 250.0, 999.0]
_FEES    = [None, 0.0, 0.18, 0.25]
_DELIV   = [0.0, 18.0, 34.0, 85.0]
_WEIGHTS = [None, 0, -1, 1, 500, 501, 1000, 2000, 5000, 7000, 9000, 9001, 50000]
_DEMAND  = [0, 0.0, 0.5, 5.0, 37.3, 100.0]
_QTY     = [0, 1, 10, 500]
_MINQ    = [None, 0, 1, 6, 12]
_FREEQ   = [None, 0, 1, 2]
_DISC    = [None, 0, 0.1, 0.25]


def test_unit_cost_equivalence():
    for bc in _COSTS:
        for up in _PACKS:
            assert engine.evaluate("unit_cost", {"basic_cost": bc, "units_per_pack": up}) \
                == P._legacy_unit_cost(bc, up), (bc, up)


def test_gross_gp_equivalence():
    for pr in _PRICES:
        for c in _COSTS:
            assert engine.evaluate("gross_gp", {"price": pr, "cost": c}) == P._legacy_gp(pr, c), (pr, c)


def test_net_margin_equivalence():
    for pr in _PRICES:
        for c in _COSTS:
            for f in _FEES:
                for d in _DELIV:
                    got = engine.evaluate("net_margin", {"price": pr, "cost": c, "fee_pct": f, "delivery": d})
                    assert got == P._legacy_channel_margin(pr, c, f, d), (pr, c, f, d)


def test_sf_logistics_equivalence():
    for w in _WEIGHTS:
        assert engine.lookup_table("sf_logistics", w) == P._legacy_sf(w), w


def test_woc_equivalence():
    for q in _QTY:
        for d in _DEMAND:
            legacy = round(q / d, 1) if d > 0 else None
            assert engine.evaluate("woc", {"total_qty": q, "weekly_demand": d}) == legacy, (q, d)


def test_sales_120d_equivalence():
    for d in _DEMAND:
        legacy = round(d * 120 / 7) if d > 0 else 0
        assert engine.evaluate("sales_120d", {"weekly_demand": d}) == legacy, d


def test_mbb_equivalence():
    for base in _COSTS:
        for mq in _MINQ:
            for fq in _FREEQ:
                got = engine.evaluate("mbb_buy_x_get_y", {"base": base, "min_qty": mq, "free_qty": fq})
                assert got == P._legacy_term_unit_cost("buy_x_get_y", base, mq, fq, None, None), (base, mq, fq)
        for disc in _DISC:
            got = engine.evaluate("mbb_spend_discount", {"base": base, "discount_pct": disc})
            assert got == P._legacy_term_unit_cost("spend_discount", base, None, None, disc, None), (base, disc)


# ── Parameters reproduce the old module constants ────────────────────────────────────────
def test_params_match_legacy_constants():
    assert engine.get_param("hktv_fee") == 0.18
    assert engine.get_param("cross_channel_threshold") == 0.05
    assert engine.get_param("staleness_days") == 90


# ── Seed: idempotent + DB-backed load stays behaviour-neutral ────────────────────────────
def test_seed_idempotent_and_db_backed_matches_defaults():
    engine.seed_defaults(database.engine)
    engine.seed_defaults(database.engine)   # second call must not duplicate
    d = database.SessionLocal()
    try:
        assert d.query(models.ConfigVersion).filter_by(is_active=1).count() == 1
        assert d.query(models.Transformation).count() == len(engine._DEFAULTS)
        assert d.query(models.TransformationValue).count() == len(engine._DEFAULTS)
    finally:
        d.close()
    engine.invalidate()   # force a reload from the DB (not the default fallback)
    assert engine.evaluate("unit_cost", {"basic_cost": 141.0, "units_per_pack": 12}) == P._legacy_unit_cost(141.0, 12)
    assert engine.evaluate("net_margin", {"price": 250.0, "cost": 40.0, "fee_pct": 0.18, "delivery": 34.0}) \
        == P._legacy_channel_margin(250.0, 40.0, 0.18, 34.0)
    assert engine.get_param("hktv_fee") == 0.18


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
