"""
Catalogue ingestion endpoints.

POST /catalogues/import         REMOVED → 410; use POST /catalogues/ingestions (queued pipeline)
GET  /catalogues                List all catalogue imports
GET  /catalogues/{id}           Import detail + item counts
GET  /catalogues/{id}/items     Items extracted from a catalogue (filterable by review_status)
PATCH /catalogues/items/{id}    Update an extracted item (edit fields before approving)
POST /catalogues/items/{id}/match        Match to an existing internal SKU
POST /catalogues/items/{id}/assign-new   Assign a new auto-generated SKU and create product
POST /catalogues/items/{id}/reject       Reject (clinical consumable / not for retail)
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, ORJSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional, Tuple, Dict
from datetime import datetime
from pydantic import BaseModel
import os

import models
import database
from services import extraction_service, audit, tagging_service, tag_service, audit_log
from services.sku_service import next_sku, CATEGORY_PREFIX
from dependencies import require_user
from permissions import require_capability

router = APIRouter(prefix="/catalogues", tags=["catalogues"])

NOW = lambda: datetime.utcnow().isoformat()

_UPLOAD_DIR = os.environ.get("CATALOGUE_UPLOAD_DIR", "/data/catalogue_uploads")


def _item_to_dict(item: models.CatalogueItem) -> dict:
    detail = {}
    if item.confidence_detail:
        import json
        try: detail = json.loads(item.confidence_detail)
        except Exception: pass
    return {
        "id":                item.id,
        "import_id":         item.import_id,
        "supplier_id":       item.supplier_id,
        "raw_description":   item.raw_description,
        "original_description": item.original_description,
        "supplier_sku":      item.supplier_sku,
        "barcode":           item.barcode,
        "cost_price":        item.cost_price,
        "uom":               item.uom,
        "units_per_pack":    item.units_per_pack,
        "min_sellable_qty":  item.min_sellable_qty,
        "brand":             item.brand,
        "variant":           item.variant,
        "pack_size":         item.pack_size,
        "bulk_buy_tiers":    item.bulk_buy_tiers,
        "max_bulk_buy_cost": item.max_bulk_buy_cost,
        "max_bulk_buy_min_qty": item.max_bulk_buy_min_qty,
        "species":           item.species,
        "weight_grams":      item.weight_grams,
        "weight_unit":       item.weight_unit or 'kg',
        "rrp":               item.rrp,
        "min_purchase_qty":  item.min_purchase_qty,
        "bulk_tiers":        _json_or(item.bulk_tiers, None),
        "confidence_score":  item.confidence_score,
        "confidence_detail": detail,
        "review_status":     item.review_status,
        "skipped":           bool(item.skipped),
        "skipped_at":        item.skipped_at,
        "skipped_by":        item.skipped_by,
        "matched_product_id": item.matched_product_id,
        "assigned_sku":      item.assigned_sku,
        "reviewed_by":       item.reviewed_by,
        "reviewed_at":       item.reviewed_at,
        "created_at":        item.created_at,
        "ai_tags":           _json_or(item.ai_tags, []),
        "ai_category":       item.ai_category,
        "ai_subcategory":    item.ai_subcategory,
    }


def _json_or(raw, default):
    import json
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _import_to_dict(imp: models.CatalogueImport, db: Session) -> dict:
    counts = {
        "pending":  db.query(models.CatalogueItem).filter(models.CatalogueItem.import_id == imp.id, models.CatalogueItem.review_status == 'pending').count(),
        "matched":  db.query(models.CatalogueItem).filter(models.CatalogueItem.import_id == imp.id, models.CatalogueItem.review_status == 'matched').count(),
        "new_sku":  db.query(models.CatalogueItem).filter(models.CatalogueItem.import_id == imp.id, models.CatalogueItem.review_status == 'new_sku').count(),
        "rejected": db.query(models.CatalogueItem).filter(models.CatalogueItem.import_id == imp.id, models.CatalogueItem.review_status == 'rejected').count(),
    }
    supplier = db.query(models.Supplier).filter(models.Supplier.id == imp.supplier_id).first() if imp.supplier_id else None
    return {
        "id":           imp.id,
        "supplier_id":  imp.supplier_id,
        "supplier_name": supplier.name if supplier else None,
        "supplier_segment": supplier.segment if supplier else None,
        "filename":     imp.filename,
        "format":       imp.format,
        "imported_at":  imp.imported_at,
        "status":       imp.status,
        "item_count":   imp.item_count,
        "counts":       counts,
        # supplier detection / resolution (stage-1 confirm)
        "detected_supplier_name": imp.detected_supplier_name,
        "detected_brands":        imp.detected_brands,
        "supplier_confidence":    imp.supplier_confidence,
        "supplier_source":        imp.supplier_source,
        "supplier_status":        imp.supplier_status,
    }


# ── Import ────────────────────────────────────────────────────────────────────

@router.post("/import", deprecated=True)
def import_catalogue_removed():
    """REMOVED (v1): synchronous AI import. Use POST /catalogues/ingestions.

    The evidence-first queued pipeline replaced this endpoint: submission
    preserves the original file, extraction/conformance run under the supplier
    contract with durable evidence, and items reach review as mastering
    candidates. A static 410 keeps the removal self-explanatory for stale
    clients instead of a bare 404.
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "ENDPOINT_REMOVED",
            "message": "POST /catalogues/import was removed. Submit catalogues via POST /catalogues/ingestions.",
        },
    )


class SupplierConfirm(BaseModel):
    supplier_id: int
    reviewed_by: Optional[str] = None


@router.patch("/{import_id}/supplier")
def confirm_import_supplier(import_id: int, body: SupplierConfirm, db: Session = Depends(database.get_db),
                            user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Stage-1 confirm: set/override the supplier for a whole catalogue file and propagate it to
    every item (so SKU matching runs against the right supplier). User choice is authoritative."""
    imp = db.query(models.CatalogueImport).filter(models.CatalogueImport.id == import_id).first()
    if not imp:
        raise HTTPException(status_code=404, detail="Catalogue import not found")
    sup = db.query(models.Supplier).filter(models.Supplier.id == body.supplier_id).first()
    if not sup:
        raise HTTPException(status_code=404, detail=f"Supplier {body.supplier_id} not found")
    imp.supplier_id = sup.id
    imp.supplier_source = "user"
    imp.supplier_status = "confirmed"
    db.query(models.CatalogueItem).filter(models.CatalogueItem.import_id == imp.id).update(
        {models.CatalogueItem.supplier_id: sup.id})
    audit.log_event(db, action='supplier_confirm', user=user, import_id=imp.id,
                    details={"supplier_id": sup.id, "supplier_code": sup.code,
                             "supplier_name": sup.name, "filename": imp.filename})
    db.commit()


@router.post("/{import_id}/ai-tag")
def ai_tag_import(import_id: int, request: Request, db: Session = Depends(database.get_db),
                  user: models.User = Depends(require_capability("catalogue_onboard"))):
    """(Re)run the AI tagging pass over an import's still-pending items. Tagging also
    runs automatically on upload; this refreshes it (e.g. after the API key is set)."""
    imp = db.query(models.CatalogueImport).filter(models.CatalogueImport.id == import_id).first()
    if not imp:
        raise HTTPException(status_code=404, detail="Catalogue import not found")
    items = db.query(models.CatalogueItem).filter(
        models.CatalogueItem.import_id == import_id,
        models.CatalogueItem.review_status == 'pending').all()
    if not items:
        return {"tagged": 0}
    sup = db.query(models.Supplier).filter(models.Supplier.id == imp.supplier_id).first() if imp.supplier_id else None
    brand_ctx = imp.detected_brands
    sup_ctx = (sup.name if sup else None) or imp.detected_supplier_name
    suggestions = tagging_service.suggest_tags([
        {"description": it.raw_description, "brand": brand_ctx, "supplier": sup_ctx} for it in items])
    import json
    n = 0
    for it, sug in zip(items, suggestions):
        tags = sug.get("tags")
        # Only overwrite when we actually got tags. An empty result usually means the AI
        # call failed (e.g. no Anthropic credit) — never wipe existing tags in that case.
        if tags:
            it.ai_tags = json.dumps(tags)
            if sug.get("category"):    it.ai_category = sug.get("category")
            if sug.get("subcategory"): it.ai_subcategory = sug.get("subcategory")
            n += 1
    audit_log.record(db, action="catalogue.ai_tag", actor=user, entity_type="catalogue_import",
                     entity_id=import_id, entity_label=imp.filename,
                     details={"tagged": n, "items": len(items)}, request=request)
    db.commit()
    failed = (len(items) > 0 and n == 0)
    return {"tagged": n, "items": len(items), "unchanged": len(items) - n,
            "warning": ("AI tagging returned nothing for every item — the Anthropic API likely "
                        "failed (check credit balance). Existing tags were left unchanged.") if failed else None}
    return {"import_id": imp.id, "supplier_id": sup.id, "supplier_code": sup.code,
            "supplier_name": sup.name, "segment": sup.segment, "status": "confirmed"}


@router.post("/items/{item_id}/detect-species")
def detect_item_species(item_id: int, request: Request, db: Session = Depends(database.get_db),
                        user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Use Claude + web search to identify the target species for one scanned item.
    Slower than the bulk tagging pass (it researches the brand/product online), so it
    runs on demand from the review screen rather than automatically on import."""
    it = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Catalogue item not found")
    imp = db.query(models.CatalogueImport).filter(models.CatalogueImport.id == it.import_id).first()
    brand_ctx = it.brand or (imp.detected_brands if imp else None)
    species = tagging_service.detect_species(it.raw_description, brand_ctx)
    if species:
        it.species = species
        audit_log.record(db, action="catalogue.detect_species", actor=user, entity_type="catalogue_item",
                         entity_id=item_id,
                         entity_label=(it.raw_description or "")[:60] or None,
                         details={"species": species}, request=request)
        db.commit()
    return {"item_id": item_id, "species": species}


# ── Import pre-extracted JSON (bypasses AI extraction) ───────────────────────

class PreExtractedItem(BaseModel):
    description: str
    supplier_sku: Optional[str] = None
    barcode: Optional[str] = None
    cost_price: Optional[float] = None
    uom: Optional[str] = None
    units_per_pack: Optional[int] = None
    brand: Optional[str] = None
    pack_size: Optional[str] = None
    bulk_buy_tiers: Optional[str] = None
    max_bulk_buy_cost: Optional[float] = None
    max_bulk_buy_min_qty: Optional[int] = None
    confidence: float = 0.9

class ImportJsonBody(BaseModel):
    supplier_id: Optional[int] = None
    filename: str = "direct-import"
    items: list[PreExtractedItem]

@router.post("/import-json")
def import_catalogue_json(
    body: ImportJsonBody,
    request: Request,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
):
    now = NOW()
    catalogue = models.CatalogueImport(
        supplier_id=body.supplier_id,
        filename=body.filename,
        format='json',
        imported_at=now,
        status='review',
        item_count=len(body.items),
    )
    db.add(catalogue)
    db.flush()

    for raw in body.items:
        db.add(models.CatalogueItem(
            import_id=catalogue.id,
            supplier_id=body.supplier_id,
            raw_description=raw.description,
            supplier_sku=raw.supplier_sku,
            barcode=raw.barcode,
            cost_price=raw.cost_price,
            uom=raw.uom,
            units_per_pack=raw.units_per_pack,
            brand=raw.brand,
            pack_size=raw.pack_size,
            bulk_buy_tiers=raw.bulk_buy_tiers,
            max_bulk_buy_cost=raw.max_bulk_buy_cost,
            max_bulk_buy_min_qty=raw.max_bulk_buy_min_qty,
            confidence_score=raw.confidence,
            confidence_detail=None,
            review_status='pending',
            created_at=now,
        ))

    audit_log.record(db, action="catalogue.import_json", actor=_user, entity_type="catalogue_import",
                     entity_id=catalogue.id, entity_label=body.filename,
                     details={"supplier_id": body.supplier_id, "item_count": len(body.items),
                              "format": "json"}, request=request)
    db.commit()
    db.refresh(catalogue)
    return {
        "import_id":  catalogue.id,
        "item_count": len(body.items),
        "message":    f"Inserted {len(body.items)} pre-extracted items.",
    }


# ── Scan log (visible progress tracker for the team) ─────────────────────────

@router.get("/scan-log")
def get_scan_log(db: Session = Depends(database.get_db)):
    """Returns a structured scan log showing extraction status per supplier.
    Used by the UI to render a progress dashboard the whole team can see."""
    imports = db.query(models.CatalogueImport).order_by(models.CatalogueImport.id).all()
    log = []
    total_real = 0
    total_err = 0
    for imp in imports:
        real = db.query(models.CatalogueItem).filter(
            models.CatalogueItem.import_id == imp.id,
            models.CatalogueItem.confidence_score > 0,
        ).count()
        err = db.query(models.CatalogueItem).filter(
            models.CatalogueItem.import_id == imp.id,
            models.CatalogueItem.confidence_score == 0,
        ).count()
        supplier = db.query(models.Supplier).filter(models.Supplier.id == imp.supplier_id).first() if imp.supplier_id else None
        status = 'ok' if real > 0 else ('error' if err > 0 else 'empty')
        total_real += real
        total_err += err
        log.append({
            "import_id": imp.id,
            "supplier_id": imp.supplier_id,
            "supplier_name": supplier.name if supplier else None,
            "filename": imp.filename,
            "format": imp.format,
            "imported_at": imp.imported_at,
            "real_items": real,
            "error_items": err,
            "status": status,
        })
    return {
        "total_imports": len(log),
        "total_items": total_real,
        "total_errors": total_err,
        "successful": sum(1 for l in log if l["status"] == "ok"),
        "failed": sum(1 for l in log if l["status"] != "ok"),
        "log": log,
    }


# ── Brand coverage (which brands does IMS already carry) ─────────────────────

@router.get("/brand-coverage")
def get_brand_coverage(db: Session = Depends(database.get_db)):
    """Distinct brand strings from ACTIVE products — used by the review UI
    to classify catalogue items as 'brand already in IMS' (likely worth
    matching) vs 'brand not in IMS' (likely reject candidate)."""
    rows = db.query(models.Product.brand).filter(
        models.Product.status == 'ACTIVE',
        models.Product.brand.isnot(None),
        models.Product.brand != '',
    ).distinct().all()
    brands = sorted({(r[0] or '').strip() for r in rows if (r[0] or '').strip()})
    return {"brands": brands, "count": len(brands)}


@router.get("/subcategories")
def list_subcategories():
    """Controlled subcategory vocabulary (functional/clinical class) — the onboarding
    reviewer picks from this; the AI tagger is constrained to it too."""
    return {"subcategories": tagging_service.CONTROLLED_SUBCATEGORIES}


# ── List imports ──────────────────────────────────────────────────────────────

@router.get("")
def list_imports(db: Session = Depends(database.get_db)):
    imports = db.query(models.CatalogueImport).order_by(models.CatalogueImport.imported_at.desc()).all()
    return [_import_to_dict(i, db) for i in imports]


@router.delete("")
def clear_all_catalogues(request: Request, confirm: bool = Query(False), db: Session = Depends(database.get_db),
                         user: models.User = Depends(require_capability("catalogue_admin"))):
    """Delete EVERY catalogue import + its extracted items (and cost staging) — e.g. to
    re-upload from scratch. Products, product tags, and the onboarding audit trail are
    PRESERVED (the audit keeps its sku_code/product_id snapshots). Requires ?confirm=true."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to clear all catalogues.")
    items = db.query(models.CatalogueItem).delete(synchronize_session=False)
    imports = db.query(models.CatalogueImport).delete(synchronize_session=False)
    try:
        db.execute(text("DELETE FROM catalogue_cost_staging"))
    except Exception:
        pass
    audit_log.record(db, action="catalogue.clear_all", actor=user, entity_type="catalogue_import",
                     entity_label="ALL imports",
                     details={"imports_deleted": imports, "items_deleted": items}, request=request)
    db.commit()
    return {"cleared": True, "imports_deleted": imports, "items_deleted": items,
            "preserved": ["products", "product_tags", "catalogue_audit"]}


@router.delete("/{import_id}")
def delete_catalogue(import_id: int, request: Request, db: Session = Depends(database.get_db),
                     user: models.User = Depends(require_capability("catalogue_admin"))):
    """Delete one catalogue import + its items (+ cost staging). Products/tags/audit kept."""
    imp = db.query(models.CatalogueImport).filter(models.CatalogueImport.id == import_id).first()
    if not imp:
        raise HTTPException(status_code=404, detail="Catalogue import not found")
    _imp_meta = {"filename": imp.filename, "supplier_id": imp.supplier_id}
    items = db.query(models.CatalogueItem).filter(
        models.CatalogueItem.import_id == import_id).delete(synchronize_session=False)
    try:
        db.execute(text("DELETE FROM catalogue_cost_staging WHERE import_id = :i"), {"i": import_id})
    except Exception:
        pass
    audit_log.record(db, action="catalogue.delete_import", actor=user, entity_type="catalogue_import",
                     entity_id=import_id, entity_label=_imp_meta["filename"],
                     details={**_imp_meta, "items_deleted": items}, request=request)
    db.delete(imp)
    db.commit()
    return {"deleted_import": import_id, "items_deleted": items}


def _audit_to_dict(e: models.CatalogueAuditEvent) -> dict:
    import json
    details = {}
    if e.details:
        try: details = json.loads(e.details)
        except Exception: pass
    return {
        "id":           e.id,
        "item_id":      e.item_id,
        "import_id":    e.import_id,
        "product_id":   e.product_id,
        "sku_code":     e.sku_code,
        "action":       e.action,
        "user_id":      e.user_id,
        "username":     e.username,
        "display_name": e.display_name,
        "details":      details,
        "created_at":   e.created_at,
    }


# NOTE: declared before "/{import_id}" so "/audit" isn't swallowed by the int path param.
@router.get("/audit")
def list_audit(
    item_id:    Optional[int] = Query(None),
    product_id: Optional[int] = Query(None),
    sku:        Optional[str] = Query(None),
    action:     Optional[str] = Query(None),
    user_id:    Optional[int] = Query(None),
    import_id:  Optional[int] = Query(None),
    limit:      int = Query(100),
    offset:     int = Query(0),
    db: Session = Depends(database.get_db),
):
    """Query the onboarding audit trail. Filter by item, product, SKU, action,
    user, or import. Newest first. Use this to answer 'who confirmed SKU X, when'."""
    q = db.query(models.CatalogueAuditEvent)
    if item_id is not None:    q = q.filter(models.CatalogueAuditEvent.item_id == item_id)
    if product_id is not None: q = q.filter(models.CatalogueAuditEvent.product_id == product_id)
    if sku:                    q = q.filter(models.CatalogueAuditEvent.sku_code == sku)
    if action:                 q = q.filter(models.CatalogueAuditEvent.action == action)
    if user_id is not None:    q = q.filter(models.CatalogueAuditEvent.user_id == user_id)
    if import_id is not None:  q = q.filter(models.CatalogueAuditEvent.import_id == import_id)
    total = q.count()
    rows = (q.order_by(models.CatalogueAuditEvent.id.desc())
             .offset(max(0, offset)).limit(min(max(1, limit), 500)).all())
    return {"total": total, "events": [_audit_to_dict(r) for r in rows]}


@router.get("/confirmed")
def list_confirmed(import_id: Optional[int] = Query(None),
                   supplier_id: Optional[int] = Query(None),
                   reviewed_by: Optional[str] = Query(None),
                   search: Optional[str] = Query(None),
                   limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0),
                   db: Session = Depends(database.get_db)):
    """Items already confirmed during onboarding (matched to an existing SKU or assigned a new
    one) — the Confirmed list. Filterable by supplier, the reviewer who confirmed it, and a
    free-text search (description / supplier SKU / assigned or matched SKU / brand). Facets make
    the Supplier + Reviewer dropdowns complete regardless of the page limit.
    NOTE: declared before GET /{import_id} so the literal path isn't shadowed by it."""
    from sqlalchemy import or_, func
    CI = models.CatalogueItem
    base = db.query(CI).filter(CI.review_status.in_(['matched', 'new_sku']))
    if import_id:
        base = base.filter(CI.import_id == import_id)
    # Facets over the whole confirmed set (BEFORE the supplier/reviewer/search filters) so the
    # dropdowns list every supplier/reviewer present here, not just the loaded page.
    supplier_facets = [
        {"supplier_id": sid, "count": cnt}
        for sid, cnt in base.with_entities(CI.supplier_id, func.count(CI.id))
        .group_by(CI.supplier_id).order_by(func.count(CI.id).desc()).all()
    ]
    user_facets = [
        {"user": u, "count": cnt}
        for u, cnt in base.with_entities(CI.reviewed_by, func.count(CI.id))
        .filter(CI.reviewed_by.isnot(None)).group_by(CI.reviewed_by)
        .order_by(func.count(CI.id).desc()).all()
    ]
    scoped = base
    if supplier_id is not None:
        scoped = scoped.filter(CI.supplier_id == supplier_id)
    if reviewed_by:
        scoped = scoped.filter(CI.reviewed_by == reviewed_by)
    if search and search.strip():
        like = f"%{search.strip()}%"
        # also match on the resulting product's SKU/name (the confirmed item links to a product)
        match_pids = [pid for (pid,) in db.query(models.Product.id).filter(
            or_(models.Product.sku_code.ilike(like), models.Product.name.ilike(like))).all()]
        scoped = scoped.filter(or_(
            CI.raw_description.ilike(like), CI.original_description.ilike(like),
            CI.supplier_sku.ilike(like), CI.assigned_sku.ilike(like), CI.brand.ilike(like),
            CI.matched_product_id.in_(match_pids or [0])))
    total = scoped.count()
    items = scoped.order_by(CI.reviewed_at.desc(), CI.id.desc()).offset(offset).limit(limit).all()
    pids = [i.matched_product_id for i in items if i.matched_product_id]
    prod = {p.id: (p.sku_code, p.name) for p in
            db.query(models.Product.id, models.Product.sku_code, models.Product.name)
            .filter(models.Product.id.in_(pids or [0])).all()}
    sup = {s.id: s.name for s in db.query(models.Supplier).all()}
    out = []
    for it in items:
        psku, pname = prod.get(it.matched_product_id, (None, None))
        out.append({
            "id": it.id, "raw_description": it.raw_description,
            "original_description": it.original_description,
            "action": it.review_status,                 # matched | new_sku
            "sku": psku or it.assigned_sku, "product_name": pname,
            "supplier_id": it.supplier_id, "supplier_name": sup.get(it.supplier_id),
            "reviewed_by": it.reviewed_by, "reviewed_at": it.reviewed_at,
        })
    return {"confirmed_count": total, "supplier_facets": supplier_facets,
            "user_facets": user_facets, "offset": offset, "items": out}


@router.get("/daily")
def daily_onboarding(days: int = Query(30, ge=1, le=365),
                     db: Session = Depends(database.get_db)):
    """Per-day onboarding throughput for the in-app Daily report: how many items were
    matched / assigned a new SKU / rejected / skipped each day, plus who was active.
    Sourced from the catalogue audit trail. Declared before GET /{import_id} so the
    literal path isn't shadowed by it."""
    from datetime import datetime, timedelta
    CA = models.CatalogueAuditEvent
    cutoff = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%dT00:00:00")
    ACT = {'confirm_match': 'matched', 'assign_new': 'new_sku', 'reject': 'rejected', 'skip': 'skipped'}
    rows = (db.query(CA.created_at, CA.action, CA.display_name, CA.username)
            .filter(CA.action.in_(list(ACT.keys())), CA.created_at >= cutoff).all())
    day_map: dict = {}
    for created, action, disp, uname in rows:
        d = (created or "")[:10]
        key = ACT.get(action)
        if not d or not key:
            continue
        b = day_map.setdefault(d, {"date": d, "matched": 0, "new_sku": 0,
                                   "rejected": 0, "skipped": 0, "_who": set()})
        b[key] += 1
        who = disp or uname
        if who:
            b["_who"].add(who)
    out = []
    for d in sorted(day_map.keys(), reverse=True):
        b = day_map[d]
        who = sorted(b.pop("_who"))
        processed = b["matched"] + b["new_sku"] + b["rejected"]
        out.append({**b, "processed": processed, "total": processed + b["skipped"],
                    "reviewers": who, "reviewer_count": len(who)})
    totals = {k: sum(x[k] for x in out) for k in
              ("matched", "new_sku", "rejected", "skipped", "processed")}
    totals["active_days"] = len(out)
    return {"days_requested": days, "from": cutoff[:10], "totals": totals, "days": out}


@router.get("/already-verified")
def already_verified_candidates(import_id: Optional[int] = Query(None),
                                min_confidence: float = Query(0.9, ge=0.0, le=1.0),
                                limit: int = Query(1000, ge=1, le=4000),
                                include_inactive: bool = Query(False),
                                db: Session = Depends(database.get_db)):
    """Pending items whose TOP match is a SKU that is already HITL-verified — i.e. a
    re-upload of a product you've already onboarded. Powers the 'already-verified' review
    banner so reviewers can clear duplicates instead of re-verifying them. Declared before
    GET /{import_id} so the literal path isn't shadowed by it."""
    from sqlalchemy import or_
    from routers.audit import _verified_sku_set
    verified = _verified_sku_set(db)
    if not verified:
        return {"count": 0, "min_confidence": min_confidence, "items": []}
    CI = models.CatalogueItem
    q = db.query(CI).filter(CI.review_status == 'pending',
                            or_(CI.skipped == 0, CI.skipped.is_(None)))
    if import_id:
        q = q.filter(CI.import_id == import_id)
    pend = q.order_by(CI.confidence_score.desc(), CI.id.desc()).all()
    idx = _build_match_indexes(db, include_inactive)   # fresh index bound to this session
    out = []
    for it in pend:
        matches = _find_matches(it, db, idx, include_inactive=include_inactive)
        if not matches:
            continue
        top = matches[0]
        if str(top["sku_code"]) in verified and (top.get("confidence") or 0) >= min_confidence:
            out.append({"id": it.id, "raw_description": it.raw_description,
                        "matched_sku": top["sku_code"], "matched_name": top["name"],
                        "match_type": top["match_type"], "confidence": top["confidence"],
                        "import_id": it.import_id})
            if len(out) >= limit:
                break
    return {"count": len(out), "min_confidence": min_confidence, "items": out}


class SkipVerifiedBody(BaseModel):
    item_ids:        Optional[list[int]] = None   # explicit list to skip (re-validated), or
    import_id:       Optional[int] = None         # all already-verified pending in this import
    min_confidence:  float = 0.9
    include_inactive: bool = False


@router.post("/skip-already-verified")
def skip_already_verified(body: SkipVerifiedBody, request: Request, db: Session = Depends(database.get_db),
                          user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Clear already-verified duplicates from the pending queue: mark each as MATCHED to its
    already-verified SKU (it leaves pending and shows in the Confirmed list) — NOT the skip
    bucket. Re-validates server-side (top match must still be verified + confident enough), so
    a stale client list can't skip something that isn't genuinely already verified. Conservative:
    does not re-apply the scan's costs to the already-onboarded product."""
    from sqlalchemy import or_
    from routers.audit import _verified_sku_set
    verified = _verified_sku_set(db)
    now = NOW()
    CI = models.CatalogueItem
    if body.item_ids:
        items = db.query(CI).filter(CI.id.in_(body.item_ids), CI.review_status == 'pending').all()
    else:
        q = db.query(CI).filter(CI.review_status == 'pending',
                                or_(CI.skipped == 0, CI.skipped.is_(None)))
        if body.import_id:
            q = q.filter(CI.import_id == body.import_id)
        items = q.all()
    idx = _build_match_indexes(db, body.include_inactive)   # fresh index bound to this session
    prod_cache: dict = {}
    n = 0
    for it in items:
        matches = _find_matches(it, db, idx, include_inactive=body.include_inactive)
        if not matches:
            continue
        top = matches[0]
        sku = str(top["sku_code"])
        if sku not in verified or (top.get("confidence") or 0) < body.min_confidence:
            continue
        product = prod_cache.get(sku)
        if product is None:
            product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
            prod_cache[sku] = product
        if not product:
            continue
        it.review_status = 'matched'
        it.matched_product_id = product.id
        it.reviewed_by = user.display_name
        it.reviewed_at = now
        it.skipped = 0
        audit.log_event(db, action='skip_verified', user=user, request=request, item=it,
                        product_id=product.id, sku_code=sku,
                        details={"reason": "already_verified", "confidence": top.get("confidence"),
                                 "match_type": top.get("match_type"), "name": product.name,
                                 "description": it.raw_description})
        n += 1
    db.commit()
    return {"skipped_verified": n}


@router.get("/{import_id}")
def get_import(import_id: int, db: Session = Depends(database.get_db)):
    imp = db.query(models.CatalogueImport).filter(models.CatalogueImport.id == import_id).first()
    if not imp:
        raise HTTPException(status_code=404, detail="Import not found")
    return _import_to_dict(imp, db)


# ── Items ─────────────────────────────────────────────────────────────────────

@router.get("/{import_id}/items")
def list_items(
    import_id: int,
    review_status: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    db: Session = Depends(database.get_db),
):
    imp = db.query(models.CatalogueImport).filter(models.CatalogueImport.id == import_id).first()
    if not imp:
        raise HTTPException(status_code=404, detail="Import not found")

    q = db.query(models.CatalogueItem).filter(models.CatalogueItem.import_id == import_id)
    if review_status:
        q = q.filter(models.CatalogueItem.review_status == review_status)
    items = q.order_by(models.CatalogueItem.id).all()

    # Preload candidates + indexes ONCE so matching all items doesn't N+1
    idx = _build_match_indexes(db, include_inactive)

    result = []
    for item in items:
        d = _item_to_dict(item)
        d["suggested_matches"] = _find_matches(item, db, idx, include_inactive=include_inactive)
        result.append(d)
    return ORJSONResponse(result)


def _name_tokens(name: str) -> frozenset:
    """Significant (len>3) lowercase words of a product name — the unit of fuzzy matching."""
    return frozenset(w for w in (name or "").lower().split() if len(w) > 3)


class _MatchIndex:
    """Everything _find_matches needs, pre-built once per request so matching N items
    is O(N · shared-word-candidates) instead of O(N · all-products) with re-tokenisation.

    Key speed-ups vs the naive scan:
      - cand_tokens: each candidate's word-set is tokenised ONCE (not per item).
      - word_to_pids: inverted index so an item only scores candidates that share a word
        (a zero-overlap product can never clear the 0.65 threshold, so skipping it is exact).
      - prod_by_id: O(1) product lookup for barcode / supplier-SKU hits (no per-item query).
    """
    __slots__ = ("candidates", "prod_by_id", "barcode_index", "supplier_sku_index",
                 "ps_by_supplier", "cand_tokens", "word_to_pids", "include_inactive")

    def __init__(self, db: Session, include_inactive: bool):
        self.include_inactive = include_inactive
        cand_q = db.query(models.Product)
        if not include_inactive:
            cand_q = cand_q.filter(models.Product.status == 'ACTIVE')
        self.candidates = cand_q.all()
        self.prod_by_id = {p.id: p for p in self.candidates}

        # Tokenise each candidate once + build the inverted word->product-ids index.
        self.cand_tokens: dict[int, frozenset] = {}
        self.word_to_pids: dict[str, set] = {}
        for p in self.candidates:
            toks = _name_tokens(p.name)
            self.cand_tokens[p.id] = toks
            for w in toks:
                self.word_to_pids.setdefault(w, set()).add(p.id)

        self.barcode_index: dict[str, models.ProductSupplier] = {}
        self.supplier_sku_index: dict[tuple[int, str], models.ProductSupplier] = {}
        self.ps_by_supplier: dict[int, dict[int, models.ProductSupplier]] = {}
        for ps in db.query(models.ProductSupplier).all():
            if ps.barcode:
                self.barcode_index.setdefault(ps.barcode, ps)
            if ps.supplier_id is not None and ps.supplier_sku:
                self.supplier_sku_index.setdefault((ps.supplier_id, ps.supplier_sku), ps)
            if ps.supplier_id is not None:
                self.ps_by_supplier.setdefault(ps.supplier_id, {})[ps.product_id] = ps


# Short-TTL cache of the match index. Building it loads + indexes every product, which
# dominated queue-endpoint latency when polled / paginated. Suggestions tolerate ~30s of
# staleness; confirm/match actions still validate against the live products table.
_MATCH_IDX_CACHE: dict[bool, tuple[float, "_MatchIndex"]] = {}
_MATCH_IDX_TTL_S = 30.0


def _build_match_indexes(db: Session, include_inactive: bool = False,
                         use_cache: bool = False) -> "_MatchIndex":
    """Pre-load + index everything _find_matches needs (see _MatchIndex)."""
    if use_cache:
        import time
        hit = _MATCH_IDX_CACHE.get(include_inactive)
        if hit and time.monotonic() - hit[0] < _MATCH_IDX_TTL_S:
            return hit[1]
        idx = _MatchIndex(db, include_inactive)
        _MATCH_IDX_CACHE[include_inactive] = (time.monotonic(), idx)
        return idx
    return _MatchIndex(db, include_inactive)


def _find_matches(
    item: models.CatalogueItem,
    db: Session,
    idx: Optional["_MatchIndex"] = None,
    include_inactive: bool = False,
) -> list[dict]:
    """Find suggested matches for a catalogue item.

    Pass a pre-built _MatchIndex (from _build_match_indexes) when matching many items so
    candidates are tokenised once and only word-sharing products are scored. Without one,
    a transient index is built for this single item.
    """
    if idx is None:
        idx = _build_match_indexes(db, include_inactive)

    matches = []

    def _resolve_product(pid: int):
        p = idx.prod_by_id.get(pid)
        if p is None and idx.include_inactive:   # candidate pool already has it if active
            p = db.query(models.Product).filter(models.Product.id == pid).first()
        elif p is None:
            # barcode/SKU may point at an INACTIVE product not in the (active-only) pool
            p = db.query(models.Product).filter(models.Product.id == pid).first()
        return p

    def _ok_status(p: models.Product) -> bool:
        return idx.include_inactive or (p is not None and p.status == 'ACTIVE')

    def _enrich(p: models.Product, match_type: str, confidence: float) -> dict:
        """SuggestedMatch dict with the fields the UI diff needs; supplier-specific
        units_per_pack / basic_cost so cost comparison is apples-to-apples."""
        ps_for_sup = (idx.ps_by_supplier.get(item.supplier_id, {}).get(p.id)
                      if item.supplier_id is not None else None)
        return {
            "sku_code": p.sku_code,
            "name": p.name,
            "match_type": match_type,
            "confidence": confidence,
            "brand": p.brand,
            "status": p.status,
            "units_per_pack": ps_for_sup.units_per_pack if ps_for_sup else None,
            "basic_cost": ps_for_sup.basic_cost if ps_for_sup else None,
            "uom": p.uom,
        }

    # 1. Exact barcode match
    if item.barcode:
        ps = idx.barcode_index.get(item.barcode)
        if ps:
            p = _resolve_product(ps.product_id)
            if p and _ok_status(p):
                matches.append(_enrich(p, "barcode", 0.99))

    # 2. Supplier SKU match
    if item.supplier_sku and item.supplier_id:
        ps = idx.supplier_sku_index.get((item.supplier_id, item.supplier_sku))
        if ps:
            p = _resolve_product(ps.product_id)
            if p and _ok_status(p) and not any(m["sku_code"] == p.sku_code for m in matches):
                matches.append(_enrich(p, "supplier_sku", 0.95))

    # 3. Fuzzy name match (word overlap, pack-size + cost tie-breakers)
    if item.raw_description and len(matches) < 3:
        words = _name_tokens(item.raw_description)
        if words:
            n_words = len(words)
            # Inverted index: only consider products that share at least one significant
            # word (anything else scores 0 and can't clear the 0.65 threshold anyway).
            cand_pids = set()
            for w in words:
                pids = idx.word_to_pids.get(w)
                if pids:
                    cand_pids |= pids

            scored = []
            for pid in cand_pids:
                overlap = len(words & idx.cand_tokens[pid]) / n_words
                if overlap >= 0.65:   # below this is too permissive (shared brand alone)
                    scored.append((overlap, idx.prod_by_id[pid]))

            # Tie-breakers from this supplier's product_supplier rows:
            #   +0.10 if units_per_pack matches; +0.10 if cost within ±15% of known cost.
            if scored and (item.units_per_pack or item.cost_price):
                ps_for_sup = idx.ps_by_supplier.get(item.supplier_id, {}) if item.supplier_id is not None else {}

                def boost(p):
                    ps = ps_for_sup.get(p.id)
                    if not ps:
                        return 0.0
                    bonus = 0.0
                    if item.units_per_pack and ps.units_per_pack == item.units_per_pack:
                        bonus += 0.10
                    if item.cost_price:
                        ref_cost = ps.basic_cost
                        if ref_cost and ref_cost > 0 and abs(item.cost_price - ref_cost) / ref_cost <= 0.15:
                            bonus += 0.10
                    return bonus

                scored = [(score + boost(p), p) for score, p in scored]

            scored.sort(key=lambda x: x[0], reverse=True)
            for score, p in scored[:3]:
                if not any(m["sku_code"] == p.sku_code for m in matches):
                    matches.append(_enrich(p, "name_fuzzy", round(min(score, 0.99), 2)))

    return matches[:3]


# ── Review actions ────────────────────────────────────────────────────────────

class ItemEdit(BaseModel):
    raw_description: Optional[str] = None
    brand:           Optional[str] = None
    variant:         Optional[str] = None    # size/volume/flavour (e.g. "15ml")
    supplier_sku:    Optional[str] = None
    barcode:         Optional[str] = None
    cost_price:      Optional[float] = None
    uom:             Optional[str] = None
    units_per_pack:  Optional[int] = None
    min_sellable_qty: Optional[int] = None
    bulk_buy_tiers:  Optional[str] = None
    # Every other scanned value is reviewer-correctable too:
    species:          Optional[str] = None    # dog | cat | both | other
    weight_grams:     Optional[float] = None
    weight_unit:      Optional[str] = None    # display unit: 'kg' | 'lb' (grams is canonical)
    rrp:              Optional[float] = None
    min_purchase_qty: Optional[int] = None    # supplier MOQ (packs)
    pack_size:        Optional[str] = None    # raw printed pack-size text
    max_bulk_buy_cost: Optional[float] = None
    max_bulk_buy_min_qty: Optional[int] = None
    supplier_id:      Optional[int] = None    # re-assign the line to a different supplier


@router.patch("/items/{item_id}")
def edit_item(item_id: int, body: ItemEdit, include_inactive: bool = Query(False),
              db: Session = Depends(database.get_db),
              user: models.User = Depends(require_capability("catalogue_onboard"))):
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if item.review_status != 'pending':
        raise HTTPException(status_code=400, detail="Cannot edit an already-reviewed item")

    _fields = ['raw_description', 'brand', 'variant', 'supplier_sku', 'barcode',
               'cost_price', 'uom', 'units_per_pack', 'min_sellable_qty', 'bulk_buy_tiers',
               'species', 'weight_grams', 'weight_unit', 'rrp', 'min_purchase_qty', 'pack_size',
               'max_bulk_buy_cost', 'max_bulk_buy_min_qty', 'supplier_id']
    before = {f: getattr(item, f) for f in _fields}

    if body.raw_description is not None: item.raw_description = body.raw_description
    if body.brand is not None:           item.brand           = body.brand
    if body.variant is not None:         item.variant         = body.variant or None
    if body.supplier_sku is not None:    item.supplier_sku    = body.supplier_sku
    if body.barcode is not None:         item.barcode         = body.barcode
    if body.cost_price is not None:      item.cost_price      = body.cost_price
    if body.uom is not None:             item.uom             = body.uom
    if body.units_per_pack is not None:  item.units_per_pack  = body.units_per_pack
    if body.min_sellable_qty is not None: item.min_sellable_qty = body.min_sellable_qty
    if body.bulk_buy_tiers is not None:  item.bulk_buy_tiers  = body.bulk_buy_tiers
    if body.species is not None:         item.species         = body.species or None
    if body.weight_grams is not None:    item.weight_grams    = body.weight_grams
    if body.weight_unit is not None:     item.weight_unit     = (body.weight_unit or 'kg')
    if body.rrp is not None:             item.rrp             = body.rrp
    if body.min_purchase_qty is not None: item.min_purchase_qty = body.min_purchase_qty
    if body.pack_size is not None:       item.pack_size       = body.pack_size
    if body.max_bulk_buy_cost is not None: item.max_bulk_buy_cost = body.max_bulk_buy_cost
    if body.max_bulk_buy_min_qty is not None: item.max_bulk_buy_min_qty = body.max_bulk_buy_min_qty
    if body.supplier_id is not None:
        if not db.query(models.Supplier).filter(models.Supplier.id == body.supplier_id).first():
            raise HTTPException(status_code=400, detail="Unknown supplier")
        item.supplier_id = body.supplier_id

    after = {f: getattr(item, f) for f in _fields}
    changes = audit.diff_changes(before, after)
    if changes:
        audit.log_event(db, action='edit', user=user, item=item,
                        details={"changes": changes})
    db.commit()
    db.refresh(item)
    d = _item_to_dict(item)
    d["suggested_matches"] = _find_matches(item, db, include_inactive=include_inactive)
    return d


class MatchBody(BaseModel):
    sku_code:    str
    reviewed_by: Optional[str] = None
    tags:        Optional[list[str]] = None   # confirmed tags; defaults to the item's AI tags
    # Reviewer-confirmed fields — applied to the matched product so confirmation updates inventory.
    category:    Optional[str] = None
    brand:       Optional[str] = None
    subcategory: Optional[str] = None
    name:        Optional[str] = None         # optional rename of the matched product


@router.post("/items/{item_id}/match")
def match_to_existing(item_id: int, body: MatchBody, request: Request, db: Session = Depends(database.get_db),
                      user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Match extracted item to an existing internal SKU and update its cost price."""
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    product = db.query(models.Product).filter(models.Product.sku_code == body.sku_code).first()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product {body.sku_code} not found")

    now = NOW()

    # Reviewer matched a catalogue line to this SKU — if it was retired, the supplier
    # is evidently carrying it again, so revive it rather than leave a dead SKU.
    if product.status != 'ACTIVE':
        product.status = 'ACTIVE'
        product.updated_at = now

    # Update cost price on primary supplier link if we have one
    ps = db.query(models.ProductSupplier).filter(
        models.ProductSupplier.product_id == product.id,
        models.ProductSupplier.is_primary == 1,
    ).first()

    if item.cost_price and item.cost_price > 0:
        if ps:
            ps.basic_cost        = item.cost_price
            ps.cost_source       = 'catalogue'
            ps.cost_source_ref   = f'catalogue_import:{item.import_id}'
            ps.cost_updated_at   = now
            ps.updated_at        = now
        else:
            ps = models.ProductSupplier(
                product_id=product.id,
                supplier_id=item.supplier_id,
                supplier_sku=item.supplier_sku,
                barcode=item.barcode,
                basic_cost=item.cost_price,
                cost_source='catalogue',
                cost_source_ref=f'catalogue_import:{item.import_id}',
                cost_updated_at=now,
                is_primary=1,
                updated_at=now,
            )
            db.add(ps)
            db.flush()

    # Scanned Max-Bulk-Buy pricing is no longer auto-written to product_suppliers — MBB now lives
    # in relational mbb_terms, curated via the item's terms editor. Scanned values remain on the
    # catalogue_item (max_bulk_buy_cost / bulk_tiers) for reference / a future term auto-create.

    # Write units_per_pack if extracted and not already manually verified
    if ps and item.units_per_pack and item.units_per_pack > 0:
        if not ps.uom_verified_at:  # don't overwrite a manually confirmed pack size
            ps.units_per_pack = item.units_per_pack
            ps.pack_source    = 'catalogue'   # protect from Sheet re-sync
            ps.updated_at = now

    # Carry the scan's supplier code + barcode into inventory on the SAME (primary) supplier
    # link the cost landed on. This previously re-queried by item.supplier_id — which is
    # frequently None when the supplier wasn't resolved during the scan — so the query
    # returned nothing and the supplier_sku was silently dropped on confirm.
    if (item.supplier_sku or item.barcode):
        if ps is None:                       # no primary link yet (e.g. scan had no cost) — make one
            ps = models.ProductSupplier(
                product_id=product.id,
                supplier_id=item.supplier_id,
                is_primary=1,
                cost_source='catalogue',
                cost_source_ref=f'catalogue_import:{item.import_id}',
                updated_at=now,
            )
            db.add(ps)
            db.flush()
        if item.supplier_sku:                # the scan IS the supplier's price list → authoritative
            ps.supplier_sku = item.supplier_sku
        if item.barcode and not ps.barcode:  # don't clobber a known barcode
            ps.barcode = item.barcode
        ps.updated_at = now

    item.review_status      = 'matched'
    item.matched_product_id = product.id
    item.reviewed_by        = user.display_name
    item.reviewed_at        = now

    # Apply the reviewer's confirmed fields to the matched product — a match is an explicit
    # human decision, so brand / category / name / subcategory propagate to inventory (and
    # therefore to the v7 sheet push). Brand falls back to the (possibly edited) scan brand.
    brand_val = body.brand if body.brand is not None else item.brand
    if brand_val and brand_val.strip() and brand_val.strip() != product.brand:
        product.brand = brand_val.strip()
        product.last_manual_edit_at = now; product.last_manual_edit_by = user.display_name
        product.updated_at = now
    # Category: reviewer's confirmed value if the page sent one, else fall back to the
    # scan's AI category — so a match propagates a category even from a stale/cached page.
    cat = body.category if body.category is not None else item.ai_category
    if cat and cat != product.category and (
            db.query(models.CategoryRule).filter(models.CategoryRule.category == cat).first()
            or cat in CATEGORY_PREFIX):
        product.category = cat
        product.last_manual_edit_at = now; product.last_manual_edit_by = user.display_name
        product.updated_at = now
    if body.name and body.name.strip() and body.name.strip() != product.name:
        product.name = body.name.strip()
        product.last_manual_edit_at = now; product.last_manual_edit_by = user.display_name
        product.updated_at = now
    sub = body.subcategory if body.subcategory is not None else item.ai_subcategory
    if sub and sub != product.subcategory:                # reviewer's pick wins; else gap-fill
        product.subcategory = sub
        product.updated_at = now
    if not product.weight_g and item.weight_grams:        # gap-fill weight from the scan
        product.weight_g = item.weight_grams
        product.weight_unit = item.weight_unit or 'kg'
        product.updated_at = now
    if not product.species and item.species:
        product.species = item.species; product.updated_at = now
    if product.rrp is None and item.rrp is not None:
        product.rrp = item.rrp; product.updated_at = now
    if product.min_purchase_qty is None and item.min_purchase_qty is not None:
        product.min_purchase_qty = item.min_purchase_qty; product.updated_at = now
    if product.min_sellable_qty is None and item.min_sellable_qty is not None:
        product.min_sellable_qty = item.min_sellable_qty; product.updated_at = now
    if not product.uom and item.uom:                      # gap-fill sell UOM from the scan
        product.uom = item.uom; product.updated_at = now

    tags = body.tags if body.tags is not None else _json_or(item.ai_tags, [])
    # Skip the apply when the confirmed set equals the product's current tags — otherwise
    # confirming would re-source the store's real shopify tags as 'ai'.
    if set(tags) != set(tag_service.tags_for_product(db, product.id)):
        tag_service.apply_tags(db, product, tags, source='ai', user=user)

    audit.log_event(db, action='confirm_match', user=user, request=request, item=item,
                    product_id=product.id, sku_code=product.sku_code,
                    details={"product_name": product.name,
                             "cost_price": item.cost_price, "tags": tags,
                             "revived": product.status == 'ACTIVE'})
    db.commit()
    return {"status": "matched", "sku_code": body.sku_code, "product_name": product.name}


class AssignNewBody(BaseModel):
    category:    str
    name:        Optional[str] = None   # override extracted description if needed
    brand:       Optional[str] = None
    uom:         Optional[str] = None
    reviewed_by: Optional[str] = None
    tags:        Optional[list[str]] = None   # confirmed tags; defaults to the item's AI tags
    subcategory: Optional[str] = None         # confirmed subcategory; defaults to the item's AI subcategory


@router.post("/items/{item_id}/assign-new")
def assign_new_sku(item_id: int, body: AssignNewBody, request: Request, db: Session = Depends(database.get_db),
                   user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Generate a new internal SKU and create the product from this catalogue item."""
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    # Valid if the category is in the rules table (canonical IMS list) or the static map.
    rule = db.query(models.CategoryRule).filter(models.CategoryRule.category == body.category).first()
    if not rule and body.category not in CATEGORY_PREFIX:
        valid = [r.category for r in db.query(models.CategoryRule.category).all()] or list(CATEGORY_PREFIX)
        raise HTTPException(status_code=400, detail=f"Unknown item category '{body.category}'. Valid: {valid}")

    now = NOW()
    sku_code     = next_sku(body.category, db)
    storage_rule = (rule.storage_rule if rule else None) or 'any'
    product_name = body.name or item.raw_description or f"SKU {sku_code}"
    # Make sure the variant (size/volume/flavour) is in the name, so sibling variants
    # become distinct, clearly-labelled products.
    if item.variant and item.variant.strip().lower() not in product_name.lower():
        product_name = f"{product_name} - {item.variant.strip()}"
    subcategory  = body.subcategory if body.subcategory is not None else item.ai_subcategory

    product = models.Product(
        sku_code=sku_code,
        name=product_name,
        brand=(body.brand or item.brand or None),   # reviewer's entry, else the scanned brand
        category=body.category,
        subcategory=subcategory or None,
        species=item.species,
        rrp=item.rrp,
        min_purchase_qty=item.min_purchase_qty,
        min_sellable_qty=item.min_sellable_qty,
        uom=body.uom or item.uom,
        weight_g=item.weight_grams,
        weight_unit=item.weight_unit or 'kg',
        storage_rule=storage_rule,
        status='ACTIVE',
        hero_sku=0,
        created_at=now,
        updated_at=now,
    )
    db.add(product)
    db.flush()

    # Create the supplier link whenever the scan carries ANY supplier data — previously a
    # missing cost skipped this whole block and silently dropped the supplier, supplier SKU,
    # barcode, pack size and bulk terms.
    has_cost = bool(item.cost_price and item.cost_price > 0)
    if has_cost or item.supplier_id or item.supplier_sku or item.barcode or item.units_per_pack:
        db.add(models.ProductSupplier(
            product_id=product.id,
            supplier_id=item.supplier_id,
            supplier_sku=item.supplier_sku,
            barcode=item.barcode,
            basic_cost=item.cost_price if has_cost else None,
            cost_source='catalogue',
            cost_source_ref=f'catalogue_import:{item.import_id}',
            cost_updated_at=now if has_cost else None,
            units_per_pack=item.units_per_pack if item.units_per_pack and item.units_per_pack > 0 else None,
            pack_source='catalogue' if item.units_per_pack and item.units_per_pack > 0 else 'sheet',
            # Scanned MBB is no longer written to product_suppliers — it lives in relational mbb_terms now.
            is_primary=1,
            updated_at=now,
        ))

    item.review_status      = 'new_sku'
    item.matched_product_id = product.id
    item.assigned_sku       = sku_code
    item.reviewed_by        = user.display_name
    item.reviewed_at        = now

    tags = body.tags if body.tags is not None else _json_or(item.ai_tags, [])
    # Skip the apply when the confirmed set equals the product's current tags — otherwise
    # confirming would re-source the store's real shopify tags as 'ai'.
    if set(tags) != set(tag_service.tags_for_product(db, product.id)):
        tag_service.apply_tags(db, product, tags, source='ai', user=user)

    audit.log_event(db, action='assign_new', user=user, request=request, item=item,
                    product_id=product.id, sku_code=sku_code,
                    details={"product_name": product_name, "category": body.category,
                             "brand": body.brand, "cost_price": item.cost_price, "tags": tags})
    db.commit()
    return {
        "status":       "new_sku_created",
        "sku_code":     sku_code,
        "product_name": product_name,
        "category":     body.category,
    }


class RejectBody(BaseModel):
    reason:      Optional[str] = None   # e.g. "clinical consumable", "duplicate", "out of scope"
    reviewed_by: Optional[str] = None


@router.post("/items/{item_id}/reject")
def reject_item(item_id: int, body: RejectBody, request: Request, db: Session = Depends(database.get_db),
                user: models.User = Depends(require_capability("catalogue_onboard"))):
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    import json
    item.review_status = 'rejected'
    if body.reason:
        item.bulk_buy_tiers = json.dumps({"rejection_reason": body.reason})
    item.reviewed_by = user.display_name
    item.reviewed_at = NOW()
    audit.log_event(db, action='reject', user=user, request=request, item=item,
                    details={"reason": body.reason, "description": item.raw_description})
    db.commit()
    return {"status": "rejected", "reason": body.reason}


@router.post("/items/{item_id}/skip")
def skip_item(item_id: int, request: Request, db: Session = Depends(database.get_db),
              user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Set a pending item aside for later — it leaves the active queue but stays undecided,
    accessible in the Skipped bucket. Un-skip returns it to the queue."""
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if item.review_status != 'pending':
        raise HTTPException(status_code=400, detail="Only pending items can be skipped")
    item.skipped = 1
    item.skipped_at = NOW()
    item.skipped_by = user.display_name
    audit.log_event(db, action='skip', user=user, request=request, item=item,
                    details={"description": item.raw_description})
    db.commit()
    return {"status": "skipped"}


@router.post("/items/{item_id}/unskip")
def unskip_item(item_id: int, request: Request, db: Session = Depends(database.get_db),
                user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Return a skipped item to the active review queue."""
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.skipped = 0
    item.skipped_at = None
    item.skipped_by = None
    audit.log_event(db, action='unskip', user=user, request=request, item=item,
                    details={"description": item.raw_description})
    db.commit()
    return {"status": "pending"}


@router.post("/items/{item_id}/unconfirm")
def unconfirm_item(item_id: int, request: Request, db: Session = Depends(database.get_db),
                   user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Undo a confirmation: return the item to the active review queue and drop the resulting
    SKU's HITL-verified status (so it leaves the sheet push and is flagged for re-review).
    The created/updated product is PRESERVED — only the decision is reopened."""
    item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if item.review_status not in ('matched', 'new_sku'):
        raise HTTPException(status_code=400, detail="Only confirmed items can be unconfirmed")
    prev = item.review_status
    prod = (db.query(models.Product).filter(models.Product.id == item.matched_product_id).first()
            if item.matched_product_id else None)
    sku = (prod.sku_code if prod else None) or item.assigned_sku
    item.review_status = 'pending'
    item.reviewed_by = None
    item.reviewed_at = None
    item.skipped = 0
    # Log as hitl_unverify so the SKU's verification is dropped (consistent with reopen).
    audit.log_event(db, action='hitl_unverify', user=user, request=request, item=item,
                    product_id=(prod.id if prod else None), sku_code=sku,
                    details={"unconfirmed_from": prev, "name": prod.name if prod else None,
                             "description": item.raw_description})
    db.commit()
    return {"status": "pending", "sku": sku}


@router.post("/{import_id}/translate")
def translate_import(import_id: int, request: Request, db: Session = Depends(database.get_db),
                     user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Translate the still-pending items of an already-scanned import to English (for scans
    done before auto-translation, or any item left in another language). Already-English and
    already-translated items are skipped. Returns how many were translated."""
    items = (db.query(models.CatalogueItem)
             .filter(models.CatalogueItem.import_id == import_id,
                     models.CatalogueItem.review_status == 'pending').all())
    if not items:
        return {"translated": 0, "checked": 0}
    # translate_to_english works on dicts in place — carry a back-reference to each item.
    payload = [{"description": it.raw_description, "_item": it} for it in items]
    extraction_service.translate_to_english(payload)
    n = 0
    for p in payload:
        if p.get("original_description"):
            it = p["_item"]
            it.original_description = p["original_description"]
            it.raw_description = p["description"]
            n += 1
    if n:
        audit_log.record(db, action="catalogue.translate", actor=user, entity_type="catalogue_import",
                         entity_id=import_id, entity_label=str(import_id),
                         details={"translated": n, "checked": len(items)}, request=request)
        db.commit()
    return {"translated": n, "checked": len(items)}


# ── Bulk actions ──────────────────────────────────────────────────────────────

class BulkMatchEntry(BaseModel):
    item_id: int
    sku_code: str
    tags: Optional[list[str]] = None

class BulkMatchBody(BaseModel):
    matches: list[BulkMatchEntry]
    reviewed_by: Optional[str] = None


@router.post("/items/bulk-match")
def bulk_match(body: BulkMatchBody, db: Session = Depends(database.get_db),
               user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Approve many catalogue items to existing IMS SKUs in one call.
    Reuses the same enrichment logic as the single match endpoint: cost,
    supplier_sku, barcode, units_per_pack are written back to product_suppliers."""
    now = NOW()
    results = {"matched": 0, "skipped": 0, "errors": []}

    for entry in body.matches:
        item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == entry.item_id).first()
        if not item:
            results["errors"].append({"item_id": entry.item_id, "error": "not found"})
            continue
        product = db.query(models.Product).filter(models.Product.sku_code == entry.sku_code).first()
        if not product:
            results["errors"].append({"item_id": entry.item_id, "error": f"sku {entry.sku_code} not found"})
            continue

        # Revive a retired SKU on match (see match_to_existing).
        if product.status != 'ACTIVE':
            product.status = 'ACTIVE'
            product.updated_at = now

        ps = db.query(models.ProductSupplier).filter(
            models.ProductSupplier.product_id == product.id,
            models.ProductSupplier.is_primary == 1,
        ).first()

        if item.cost_price and item.cost_price > 0:
            if ps:
                ps.basic_cost      = item.cost_price
                ps.cost_source     = 'catalogue'
                ps.cost_source_ref = f'catalogue_import:{item.import_id}'
                ps.cost_updated_at = now
                ps.updated_at      = now
            else:
                ps = models.ProductSupplier(
                    product_id=product.id,
                    supplier_id=item.supplier_id,
                    supplier_sku=item.supplier_sku,
                    barcode=item.barcode,
                    basic_cost=item.cost_price,
                    cost_source='catalogue',
                    cost_source_ref=f'catalogue_import:{item.import_id}',
                    cost_updated_at=now,
                    is_primary=1,
                    updated_at=now,
                )
                db.add(ps)
                db.flush()

        # Scanned MBB is no longer written to product_suppliers — it lives in relational mbb_terms now.

        if ps and item.units_per_pack and item.units_per_pack > 0 and not ps.uom_verified_at:
            ps.units_per_pack = item.units_per_pack
            ps.pack_source    = 'catalogue'   # protect from Sheet re-sync
            ps.updated_at = now

        if item.barcode or item.supplier_sku:
            ps_supplier = db.query(models.ProductSupplier).filter(
                models.ProductSupplier.product_id == product.id,
                models.ProductSupplier.supplier_id == item.supplier_id,
            ).first()
            if ps_supplier:
                if item.barcode and not ps_supplier.barcode:        ps_supplier.barcode = item.barcode
                if item.supplier_sku and not ps_supplier.supplier_sku: ps_supplier.supplier_sku = item.supplier_sku
                ps_supplier.updated_at = now

        item.review_status      = 'matched'
        item.matched_product_id = product.id
        item.reviewed_by        = user.display_name
        item.reviewed_at        = now
        # Gap-fill product fields from the scan (non-destructive — bulk-match has no
        # per-item review, so we never overwrite existing values).
        if not product.brand and item.brand:
            product.brand = item.brand; product.updated_at = now
        if not product.subcategory and item.ai_subcategory:
            product.subcategory = item.ai_subcategory; product.updated_at = now
        if not product.species and item.species:
            product.species = item.species; product.updated_at = now
        if product.rrp is None and item.rrp is not None:
            product.rrp = item.rrp; product.updated_at = now
        if product.min_purchase_qty is None and item.min_purchase_qty is not None:
            product.min_purchase_qty = item.min_purchase_qty; product.updated_at = now
        if not product.weight_g and item.weight_grams:
            product.weight_g = item.weight_grams
            product.weight_unit = item.weight_unit or 'kg'; product.updated_at = now

        _tags = entry.tags if entry.tags is not None else _json_or(item.ai_tags, [])
        tag_service.apply_tags(db, product, _tags, source='ai', user=user)
        audit.log_event(db, action='confirm_match', user=user, item=item,
                        product_id=product.id, sku_code=product.sku_code,
                        details={"product_name": product.name, "bulk": True,
                                 "cost_price": item.cost_price, "tags": _tags})
        results["matched"] += 1

    db.commit()
    return results


class BulkRejectBody(BaseModel):
    item_ids: list[int]
    reason:      Optional[str] = None
    reviewed_by: Optional[str] = None


@router.post("/items/bulk-reject")
def bulk_reject(body: BulkRejectBody, db: Session = Depends(database.get_db),
                user: models.User = Depends(require_capability("catalogue_onboard"))):
    now = NOW()
    import json
    n = 0
    for iid in body.item_ids:
        item = db.query(models.CatalogueItem).filter(models.CatalogueItem.id == iid).first()
        if not item:
            continue
        item.review_status = 'rejected'
        if body.reason:
            item.bulk_buy_tiers = json.dumps({"rejection_reason": body.reason})
        item.reviewed_by = user.display_name
        item.reviewed_at = now
        audit.log_event(db, action='reject', user=user, item=item,
                        details={"reason": body.reason, "bulk": True,
                                 "description": item.raw_description})
        n += 1
    db.commit()
    return {"rejected": n, "reason": body.reason}


class MatchConfidentBody(BaseModel):
    import_id:        Optional[int] = None
    min_confidence:   float = 0.95
    include_inactive: bool = False
    reviewed_by:      Optional[str] = None


@router.post("/items/match-confident")
def match_confident(body: MatchConfidentBody, db: Session = Depends(database.get_db),
                    user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Approve EVERY pending item whose top match is confident enough (≥ min_confidence) — across
    the WHOLE queue, not just the page the reviewer loaded. Matching is against ACTIVE inventory
    by default. Reuses bulk-match so the cost / supplier-SKU / barcode / pack write-back, tagging
    and audit logic stays in one place."""
    from sqlalchemy import or_
    CI = models.CatalogueItem
    q = db.query(CI).filter(CI.review_status == 'pending', or_(CI.skipped == 0, CI.skipped.is_(None)))
    if body.import_id:
        q = q.filter(CI.import_id == body.import_id)
    pend = q.order_by(CI.confidence_score.desc(), CI.id.desc()).all()
    idx = _build_match_indexes(db, body.include_inactive)
    entries = []
    for it in pend:
        matches = _find_matches(it, db, idx, include_inactive=body.include_inactive)
        if matches and (matches[0].get("confidence") or 0) >= body.min_confidence:
            entries.append(BulkMatchEntry(item_id=it.id, sku_code=str(matches[0]["sku_code"])))
    if not entries:
        return {"matched": 0, "scanned": len(pend)}
    result = bulk_match(BulkMatchBody(matches=entries, reviewed_by=body.reviewed_by), db, user)
    return {"matched": result.get("matched", 0), "scanned": len(pend)}


class RejectBrandBody(BaseModel):
    brand:            str
    import_id:        Optional[int] = None
    unmatched_only:   bool = True             # only reject brand items that have NO match
    reason:           Optional[str] = None
    include_inactive: bool = False
    reviewed_by:      Optional[str] = None


@router.post("/items/reject-brand")
def reject_brand(body: RejectBrandBody, db: Session = Depends(database.get_db),
                 user: models.User = Depends(require_capability("catalogue_onboard"))):
    """Reject EVERY pending item of a brand (by default only the unmatched ones) — across the
    whole queue, not just the loaded page. Used for 'brand not carried'. Reuses bulk-reject."""
    from sqlalchemy import or_
    CI = models.CatalogueItem
    q = db.query(CI).filter(CI.review_status == 'pending', or_(CI.skipped == 0, CI.skipped.is_(None)),
                            CI.brand == body.brand)
    if body.import_id:
        q = q.filter(CI.import_id == body.import_id)
    pend = q.all()
    if body.unmatched_only:
        idx = _build_match_indexes(db, body.include_inactive)
        ids = [it.id for it in pend
               if not _find_matches(it, db, idx, include_inactive=body.include_inactive)]
    else:
        ids = [it.id for it in pend]
    if not ids:
        return {"rejected": 0, "scanned": len(pend)}
    result = bulk_reject(BulkRejectBody(item_ids=ids,
                                        reason=body.reason or f"brand_not_carried:{body.brand}",
                                        reviewed_by=body.reviewed_by), db, user)
    return {"rejected": result.get("rejected", 0), "scanned": len(pend)}


# ── Pending queue across all imports ─────────────────────────────────────────

@router.delete("/queue/pending")
def clear_pending_queue(request: Request, confirm: bool = Query(False), db: Session = Depends(database.get_db),
                        user: models.User = Depends(require_capability("catalogue_admin"))):
    """Remove every still-pending (queued, waiting-to-confirm) catalogue item across all
    imports. The imports themselves and any already-processed items (matched / new-SKU /
    rejected) are kept — so this just empties the review queue. Requires ?confirm=true."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to clear the queue.")
    n = db.query(models.CatalogueItem).filter(
        models.CatalogueItem.review_status == 'pending').delete(synchronize_session=False)
    audit_log.record(db, action="catalogue.clear_pending", actor=user, entity_type="catalogue_item",
                     entity_label="pending queue", details={"items_deleted": n}, request=request)
    db.commit()
    return {"cleared": True, "items_deleted": n}


@router.get("/queue/pending")
def get_pending_queue(
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    import_id: Optional[int] = Query(None),
    supplier_id: Optional[int] = Query(None),
    skipped_by: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    skipped: bool = Query(False),
    db: Session = Depends(database.get_db),
):
    """Pending items for review. `skipped=true` returns the skip bucket (items set aside
    for later) instead of the active queue. pending_count = active to-do; skipped_count =
    bucket size; filtered_count = items in the current scope."""
    from sqlalchemy import or_, func
    CI = models.CatalogueItem
    base = db.query(CI).filter(CI.review_status == 'pending')
    active = base.filter(or_(CI.skipped == 0, CI.skipped.is_(None)))
    bucket = base.filter(CI.skipped == 1)
    total_pending = active.count()
    skipped_count = bucket.count()
    confirmed_count = db.query(CI).filter(CI.review_status.in_(['matched', 'new_sku'])).count()
    view = bucket if skipped else active
    in_scope = view.filter(CI.import_id == import_id) if import_id else view
    # Supplier facets over the whole in-scope view (BEFORE applying the supplier filter) so the
    # review screen's supplier dropdown lists every supplier that has items here, independent of
    # the page limit — previously the dropdown was built only from the loaded page.
    supplier_facets = [
        {"supplier_id": sid, "count": cnt}
        for sid, cnt in in_scope.with_entities(CI.supplier_id, func.count(CI.id))
        .group_by(CI.supplier_id).order_by(func.count(CI.id).desc()).all()
    ]
    # Reviewer facets — who skipped the items in this view (the skip bucket records skipped_by) —
    # so the Skipped table's user dropdown is complete regardless of the page limit.
    user_facets = [
        {"user": u, "count": cnt}
        for u, cnt in in_scope.with_entities(CI.skipped_by, func.count(CI.id))
        .filter(CI.skipped_by.isnot(None)).group_by(CI.skipped_by)
        .order_by(func.count(CI.id).desc()).all()
    ]
    scoped = in_scope
    if supplier_id is not None:
        scoped = scoped.filter(CI.supplier_id == supplier_id)
    if skipped_by:
        scoped = scoped.filter(CI.skipped_by == skipped_by)
    if search and search.strip():
        like = f"%{search.strip()}%"
        scoped = scoped.filter(or_(
            CI.raw_description.ilike(like), CI.original_description.ilike(like),
            CI.supplier_sku.ilike(like), CI.brand.ilike(like),
            CI.barcode.ilike(like), CI.assigned_sku.ilike(like)))
    filtered_count = (
        scoped.count()
        if (import_id or supplier_id is not None or skipped_by or (search and search.strip()))
        else (skipped_count if skipped else total_pending)
    )
    items = (
        scoped
        .order_by(models.CatalogueItem.confidence_score.desc(), models.CatalogueItem.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    idx = _build_match_indexes(db, include_inactive, use_cache=True)
    imports_by_id = {i.id: i for i in db.query(models.CatalogueImport).all()}
    result = []
    for item in items:
        d = _item_to_dict(item)
        d["suggested_matches"] = _find_matches(item, db, idx, include_inactive=include_inactive)
        imp = imports_by_id.get(item.import_id)
        d["import_filename"] = imp.filename if imp else None
        result.append(d)

    # Attach the TOP match's REAL tags (preferring shopify-sourced over AI-guessed) so the
    # review card shows the store's actual tags instead of AI suggestions. Batched: one
    # query for products, one for their tag links.
    top_skus = {r["suggested_matches"][0]["sku_code"] for r in result if r["suggested_matches"]}
    if top_skus:
        prods = db.query(models.Product.id, models.Product.sku_code).filter(
            models.Product.sku_code.in_(top_skus)).all()
        id_to_sku = {pid: sku for pid, sku in prods}
        tag_rows = (db.query(models.ProductTag.product_id, models.ProductTag.source, models.Tag.label)
                    .join(models.Tag, models.ProductTag.tag_id == models.Tag.id)
                    .filter(models.ProductTag.product_id.in_(id_to_sku.keys())).all())
        by_sku: dict = {}
        for pid, source, label in tag_rows:
            by_sku.setdefault(id_to_sku[pid], []).append((source, label))
        for r in result:
            if not r["suggested_matches"]:
                continue
            pairs = by_sku.get(r["suggested_matches"][0]["sku_code"]) or []
            shopify = sorted({lb for s, lb in pairs if s == "shopify"})
            r["suggested_matches"][0]["tags"] = shopify or sorted({lb for _, lb in pairs})
            r["suggested_matches"][0]["tags_source"] = "shopify" if shopify else ("mixed" if pairs else None)

    return ORJSONResponse({"pending_count": total_pending, "skipped_count": skipped_count,
                         "confirmed_count": confirmed_count, "supplier_facets": supplier_facets,
                         "user_facets": user_facets,
                         "filtered_count": filtered_count, "offset": offset, "items": result})
