"""Phase C guard: editing FORMULA transformations is live, versioned, reversible, and safe.
A candidate formula must pass the sandbox allow-list using only its declared inputs AND compute
a number/None on representative inputs. Engine-level tests against a throwaway temp DB.

An autouse fixture resets the config to seeded defaults before AND after each test, so this
file cannot leave the shared test DB non-default for other suites.
"""
import os
import sys
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_ROOT)

import pytest                                    # noqa: E402
import database                                  # noqa: E402
import models                                    # noqa: E402
from services import transform_engine as engine  # noqa: E402

models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

_GP_4 = round((3 - 1) / 3, 4)   # 0.6667 — default gross_gp rounds to 4 dp
_GP_2 = round((3 - 1) / 3, 2)   # 0.67  — an edited variant rounding to 2 dp
_GP2_FORMULA = "None if (not price or price <= 0 or cost is None or cost <= 0) else round((price - cost) / price, 2)"


def _reset_to_defaults():
    d = database.SessionLocal()
    try:
        for m in (models.TransformationValue, models.ConfigVersion, models.Transformation):
            d.query(m).delete()
        d.commit()
    finally:
        d.close()
    engine.seed_defaults(database.engine)
    engine.invalidate()


@pytest.fixture(autouse=True)
def _fresh_config():
    _reset_to_defaults()
    yield
    _reset_to_defaults()


def test_edit_formula_is_live_and_versioned():
    d = database.SessionLocal()
    try:
        assert engine.evaluate("gross_gp", {"price": 3.0, "cost": 1.0}) == _GP_4
        new, before, after = engine.edit_value(d, "gross_gp", formula=_GP2_FORMULA, editor="tester", note="2dp")
        d.commit(); engine.invalidate()
        assert engine.evaluate("gross_gp", {"price": 3.0, "cost": 1.0}) == _GP_2     # took effect live
        assert "round((price - cost) / price, 2)" in after
        assert d.query(models.ConfigVersion).count() == 2                            # new version, old retained
    finally:
        d.close()


def test_formula_rollback_restores_prior():
    d = database.SessionLocal()
    try:
        v0 = d.query(models.ConfigVersion).filter_by(is_active=1).first().id
        engine.edit_value(d, "gross_gp", formula=_GP2_FORMULA, editor="t"); d.commit(); engine.invalidate()
        assert engine.evaluate("gross_gp", {"price": 3.0, "cost": 1.0}) == _GP_2
        engine.restore_version(d, v0, editor="t"); d.commit(); engine.invalidate()
        assert engine.evaluate("gross_gp", {"price": 3.0, "cost": 1.0}) == _GP_4      # rolled back
    finally:
        d.close()


def test_unsafe_or_broken_formulas_rejected():
    d = database.SessionLocal()
    try:
        bad = [
            "__import__('os').system('x')",   # import / arbitrary call
            "price.__class__",                 # attribute access
            "getattr(price, 'x')",             # disallowed builtin
            "price + secret",                  # name not declared for gross_gp
            "price / 0",                       # crashes on the sample inputs
            "price > cost",                    # returns a bool, not a number
            "",                                # empty
            "   ",                             # blank
        ]
        for f in bad:
            with pytest.raises(ValueError):
                engine.edit_value(d, "gross_gp", formula=f, editor="t")
            d.rollback()
        engine.invalidate()
        assert engine.evaluate("gross_gp", {"price": 3.0, "cost": 1.0}) == _GP_4      # unchanged
    finally:
        d.close()


def test_formula_constrained_to_declared_inputs():
    d = database.SessionLocal()
    try:
        # unit_cost declares [basic_cost, units_per_pack]; referencing `price` must be rejected
        with pytest.raises(ValueError):
            engine.edit_value(d, "unit_cost", formula="basic_cost / price", editor="t")
    finally:
        d.close()


def test_formula_requires_formula_arg_and_rejects_param_on_formula_key():
    d = database.SessionLocal()
    try:
        with pytest.raises(ValueError):
            engine.edit_value(d, "gross_gp", value=1.0, editor="t")   # value= on a formula key -> needs formula=
    finally:
        d.close()


def test_validate_formula_dry():
    assert engine.validate_formula("gross_gp", "round((price - cost) / price, 4)")
    for bad in ("price + nope", "price.__dict__", "min(price)", ""):
        with pytest.raises(ValueError):
            engine.validate_formula("gross_gp", bad)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
