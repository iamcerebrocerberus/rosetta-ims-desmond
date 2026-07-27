"""Audit-coverage regression guard.

Statically asserts that EVERY mutating endpoint (POST/PUT/PATCH/DELETE) in every router either
records to the audit trail (audit_log.record / audit.log_event / _audit_product) or is explicitly
allow-listed as a non-persisting compute endpoint. This is what stops a newly-added write endpoint
from silently shipping without an audit row — the gap that made the competitor rows untraceable.

Run: `python -m pytest tests/test_audit_coverage.py`  (or `python tests/test_audit_coverage.py`).
"""
import ast
import glob
import os

ROUTERS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routers")

# Mutating endpoints that legitimately DO NOT persist anything (pure compute / preview / proxy),
# so they need no audit row. Keyed by "<router_file>::<function_name>". Keep this list tight —
# every entry is a deliberate exemption, not a TODO.
ALLOWLIST = {
    "collections.py::preview",          # evaluates a rule, saves nothing
    "collections.py::suggest",          # returns AI draft collections, saves nothing
    "config.py::validate",              # dry-run validates a proposed config edit, saves nothing
    # Persist via a delegated, already-audited function — auditing here would double-log:
    "products.py::update_product_slash",  # -> update_product (audited)
    "catalogues.py::match_confident",     # -> bulk_match (audits each item via log_event)
    "catalogues.py::reject_brand",        # -> bulk_reject (audits each item via log_event)
    "reparse.py::confirm_reparse",        # -> reparse_service.apply_change (audits each applied change)
    "catalogues.py::import_catalogue_removed",  # static 410 tombstone for the removed v1 import; mutates nothing
}

_AUDIT_ATTRS = {"record", "log_event"}          # audit_log.record(...) / audit.log_event(...)
_MUTATING = {"post", "put", "patch", "delete"}


def _is_mutating_route(func: ast.FunctionDef) -> bool:
    for dec in func.decorator_list:
        # @router.post("...") -> Call(func=Attribute(attr='post'))
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if dec.func.attr in _MUTATING:
                return True
    return False


def _has_audit_call(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in _AUDIT_ATTRS:
            return True                                  # audit_log.record / audit.log_event
        if isinstance(f, ast.Attribute) and f.attr.startswith("_audit"):
            return True                                  # self._audit_product(...)
        if isinstance(f, ast.Name) and f.id.startswith("_audit"):
            return True                                  # _audit_product(...)
    return False


def find_uncovered() -> list[str]:
    uncovered = []
    for path in sorted(glob.glob(os.path.join(ROUTERS_DIR, "*.py"))):
        fname = os.path.basename(path)
        if fname == "__init__.py":
            continue
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_mutating_route(node):
                key = f"{fname}::{node.name}"
                if key in ALLOWLIST:
                    continue
                if not _has_audit_call(node):
                    uncovered.append(key)
    return uncovered


def test_every_mutating_endpoint_is_audited():
    uncovered = find_uncovered()
    assert not uncovered, (
        "These mutating endpoints write data but record no audit row. Add an "
        "audit_log.record(...) call, or (only if it truly persists nothing) add it to "
        "ALLOWLIST in this test:\n  " + "\n  ".join(uncovered)
    )


if __name__ == "__main__":
    missing = find_uncovered()
    if missing:
        print(f"❌ {len(missing)} mutating endpoint(s) with NO audit row:")
        for k in missing:
            print("   ", k)
        raise SystemExit(1)
    print("✅ every mutating endpoint records an audit row (or is allow-listed)")
