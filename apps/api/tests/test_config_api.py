"""Phase B guard: editing transformation config (parameters/tables) is live, versioned,
reversible, and validated. Engine-level tests against a throwaway temp DB (never prod).

An autouse fixture resets the config to seeded defaults before AND after each test, so this
file cannot leave the shared test DB in a non-default state for other suites.
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


def test_edit_parameter_is_live_and_versioned():
    d = database.SessionLocal()
    try:
        assert engine.get_param("hktv_fee") == 0.18
        new, before, after = engine.edit_value(d, "hktv_fee", value=0.20, editor="tester", note="raise fee")
        d.commit(); engine.invalidate()
        assert before == 0.18 and after == 0.20
        assert engine.get_param("hktv_fee") == 0.20                       # took effect immediately
        assert d.query(models.ConfigVersion).count() == 2                 # new version, old retained
        assert d.query(models.ConfigVersion).filter_by(is_active=1).count() == 1
    finally:
        d.close()


def test_rollback_restores_prior_value():
    d = database.SessionLocal()
    try:
        v0 = d.query(models.ConfigVersion).filter_by(is_active=1).first().id
        engine.edit_value(d, "hktv_fee", value=0.25, editor="t"); d.commit(); engine.invalidate()
        assert engine.get_param("hktv_fee") == 0.25
        engine.restore_version(d, v0, editor="t"); d.commit(); engine.invalidate()
        assert engine.get_param("hktv_fee") == 0.18                       # rolled back
        assert d.query(models.ConfigVersion).filter_by(is_active=1).count() == 1
    finally:
        d.close()


def test_unchanged_keys_carry_over_on_edit():
    d = database.SessionLocal()
    try:
        engine.edit_value(d, "hktv_fee", value=0.20, editor="t"); d.commit(); engine.invalidate()
        # editing hktv_fee must not disturb other transformations
        assert engine.get_param("cross_channel_threshold") == 0.05
        assert engine.get_param("staleness_days") == 90
        assert engine.lookup_table("sf_logistics", 600) == 30.0
    finally:
        d.close()


def test_validation_rejects_bad_parameter():
    d = database.SessionLocal()
    try:
        for bad in (1.5, -0.1):
            with pytest.raises(ValueError):
                engine.edit_value(d, "hktv_fee", value=bad, editor="t")
            d.rollback()
        with pytest.raises(ValueError):
            engine.edit_value(d, "staleness_days", value=0, editor="t")   # below min
        d.rollback()
        with pytest.raises(ValueError):
            engine.edit_value(d, "hktv_fee", value="x", editor="t")       # not a number
        d.rollback()
        engine.invalidate()
        assert engine.get_param("hktv_fee") == 0.18                        # unchanged
    finally:
        d.close()


def test_formula_edit_rejected_in_phase_b():
    d = database.SessionLocal()
    try:
        with pytest.raises(ValueError):
            engine.edit_value(d, "unit_cost", value=1.0, editor="t")
        with pytest.raises(ValueError):
            engine.edit_value(d, "net_margin", value=1.0, editor="t")
    finally:
        d.close()


def test_edit_table_changes_lookup():
    d = database.SessionLocal()
    try:
        assert engine.lookup_table("sf_logistics", 600) == 30.0            # default <=1000 tier (SF LTO)
        new_table = {"tiers": [[1000, 20.0], [5000, 40.0]], "over": 90.0, "unknown": 30.0}
        engine.edit_value(d, "sf_logistics", table=new_table, editor="t"); d.commit(); engine.invalidate()
        assert engine.lookup_table("sf_logistics", 600) == 20.0            # now <=1000 -> 20
        assert engine.lookup_table("sf_logistics", 8000) == 90.0           # over
        assert engine.lookup_table("sf_logistics", 0) == 30.0             # unknown
    finally:
        d.close()


def test_invalid_table_rejected():
    d = database.SessionLocal()
    try:
        with pytest.raises(ValueError):   # not strictly ascending
            engine.edit_value(d, "sf_logistics",
                              table={"tiers": [[1000, 20], [500, 10]], "over": 1, "unknown": 1}, editor="t")
        d.rollback()
        with pytest.raises(ValueError):   # empty tiers
            engine.edit_value(d, "sf_logistics", table={"tiers": [], "over": 1, "unknown": 1}, editor="t")
        d.rollback()
        with pytest.raises(ValueError):   # negative cost
            engine.edit_value(d, "sf_logistics",
                              table={"tiers": [[1000, -5]], "over": 1, "unknown": 1}, editor="t")
    finally:
        d.close()


def test_unknown_key_rejected():
    d = database.SessionLocal()
    try:
        with pytest.raises(ValueError):
            engine.edit_value(d, "does_not_exist", value=1.0, editor="t")
        with pytest.raises(ValueError):
            engine.restore_version(d, 999999, editor="t")
    finally:
        d.close()


def test_list_config_and_versions():
    d = database.SessionLocal()
    try:
        cfg = engine.list_config(d)
        keys = {c["key"] for c in cfg}
        assert {"unit_cost", "net_margin", "hktv_fee", "sf_logistics"} <= keys
        hktv = next(c for c in cfg if c["key"] == "hktv_fee")
        assert hktv["editable"] and hktv["value"] == 0.18
        assert next(c for c in cfg if c["key"] == "unit_cost")["editable"] is False   # formula, not yet
        assert len(engine.list_versions(d)) >= 1
    finally:
        d.close()


def test_router_imports_cleanly():
    from routers import config as config_router
    assert config_router.router is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
