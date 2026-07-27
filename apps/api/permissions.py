"""Role-based access control.

Three roles, increasing restriction:
  - admin      : full access + user management + admin panel.
  - bizops     : view everything; do catalogue onboarding and FULL product/SKU edits.
                 Cannot touch reference data, sheets, stock import, bulk-delete catalogues,
                 user management or the audit panel.
  - data_entry : like bizops, but cannot change the sensitive product fields
                 (name / category / status / hero_sku).

Capabilities are the unit of enforcement — endpoints depend on `require_capability("…")`
rather than checking role strings inline, so the policy lives in one place.
"""
from fastapi import Depends, HTTPException

from dependencies import require_user
import models

ROLE_ADMIN = "admin"
ROLE_BIZOPS = "bizops"
ROLE_DATA_ENTRY = "data_entry"
VALID_ROLES = (ROLE_ADMIN, ROLE_BIZOPS, ROLE_DATA_ENTRY)
ROLE_LABELS = {ROLE_ADMIN: "Admin", ROLE_BIZOPS: "BizOps", ROLE_DATA_ENTRY: "Data Entry"}

# Product fields only Admin + BizOps may change (data_entry is blocked from these).
SENSITIVE_PRODUCT_FIELDS = {"name", "category", "status", "hero_sku"}

_ALL = set(VALID_ROLES)

# capability -> roles that hold it.
CAPABILITIES: dict[str, set[str]] = {
    "catalogue_onboard": _ALL,                       # match / new SKU / reject / edit / bulk / import
    "catalogue_publish": {ROLE_ADMIN, ROLE_BIZOPS},  # apply approved state and publish serving snapshots
    "product_edit":      _ALL,                       # PATCH product, cost, pack, tags, stock, unverify
    "product_sensitive": {ROLE_ADMIN, ROLE_BIZOPS},  # name / category / status / hero_sku
    "catalogue_admin":   {ROLE_ADMIN},               # delete whole imports / clear the queue
    "reference_admin":   {ROLE_ADMIN},               # categories / collections / brands / suppliers
    "sheet":             {ROLE_ADMIN},               # google sheet sync + push
    "stock_import":      {ROLE_ADMIN},               # upload stock CSVs
    "user_admin":        {ROLE_ADMIN},               # create / edit / deactivate users
    "audit_view":        {ROLE_ADMIN},               # view the audit log
    "config_admin":      {ROLE_ADMIN},               # edit transformation config (fees/thresholds/tiers)
}


def has_capability(role: str | None, capability: str) -> bool:
    return bool(role) and role in CAPABILITIES.get(capability, set())


def require_capability(capability: str):
    """FastAPI dependency factory: 401 if unauthenticated, 403 if the role lacks `capability`."""
    def _dep(user: "models.User" = Depends(require_user)) -> "models.User":
        if not has_capability(user.role, capability):
            label = ROLE_LABELS.get(user.role, user.role)
            raise HTTPException(status_code=403,
                                detail=f"{label} role is not permitted to perform this action")
        return user
    return _dep
