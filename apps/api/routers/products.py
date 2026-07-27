import csv
import io
import json
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, ORJSONResponse
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError
from typing import Optional
from pydantic import BaseModel
from datetime import datetime, date

import models
import database
from services.pricing_service import product_to_dict
from services import tag_service, audit, audit_log
from dependencies import get_current_user, require_user
from permissions import require_capability, has_capability, SENSITIVE_PRODUCT_FIELDS
from schemas.catalogue_pipeline import ServingItemV1
from services.catalogue_serving_reads import CatalogueServingReadService

router = APIRouter(prefix="/products", tags=["products"])


class ServingPublicationPageResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[ServingItemV1]


class ServingPublicationHistoryItemResponse(BaseModel):
    is_current: bool
    superseded_at: str | None = None
    snapshot: ServingItemV1


class ServingPublicationHistoryResponse(BaseModel):
    canonical_sku: str
    current_publications: list[ServingItemV1]
    publication_history: list[ServingPublicationHistoryItemResponse]


def _verified_skus(db: Session) -> set:
    """SKUs currently HITL-verified (latest confirm/assign/unverify event is a verify)."""
    rows = (db.query(models.CatalogueAuditEvent.sku_code, models.CatalogueAuditEvent.action)
            .filter(models.CatalogueAuditEvent.action.in_(["confirm_match", "assign_new", "hitl_verify", "hitl_unverify"]),
                    models.CatalogueAuditEvent.sku_code.isnot(None))
            .order_by(models.CatalogueAuditEvent.created_at).all())
    verified = set()
    for sku, action in rows:
        if action == "hitl_unverify":
            verified.discard(str(sku))
        else:
            verified.add(str(sku))
    return verified


_VERIFIED_CACHE: dict = {"t": 0.0, "skus": None}


def _verified_skus_cached(db: Session) -> set:
    """`_verified_skus` with a short TTL so a progressive, page-by-page load of the inventory
    list doesn't re-run the audit scan once per page."""
    import time
    if _VERIFIED_CACHE["skus"] is not None and (time.monotonic() - _VERIFIED_CACHE["t"]) < 20:
        return _VERIFIED_CACHE["skus"]
    skus = _verified_skus(db)
    _VERIFIED_CACHE.update(t=time.monotonic(), skus=skus)
    return skus


@router.post("/hitl-unverify-all")
def hitl_unverify_all(db: Session = Depends(database.get_db), user: models.User = Depends(require_capability("product_edit"))):
    """Remove HITL-verified status from EVERY currently-verified SKU (logs an unverify
    event each). They drop out of the sheet push until re-verified via onboarding."""
    skus = _verified_skus(db)
    for sku in skus:
        p = db.query(models.Product).filter(models.Product.sku_code == sku).first()
        audit.log_event(db, action="hitl_unverify", user=user,
                        product_id=(p.id if p else None), sku_code=sku,
                        details={"name": p.name if p else None, "bulk": True})
    db.commit()
    return {"unverified": len(skus), "skus": sorted(skus)}


@router.post("/sync-shopify-tags")
def sync_shopify_tags(request: Request, dry_run: bool = Query(False), db: Session = Depends(database.get_db),
                      user: models.User = Depends(require_capability("reference_admin"))):
    """Pull each product's REAL tags from Shopify (matched by normalised title, then an
    alphanumeric-only fallback) and apply them as authoritative `shopify`-source tags,
    replacing the AI-guessed ones. Free + human-accurate. Products with no Shopify match
    keep their existing tags. dry_run reports the match rate without writing anything."""
    import os, re, json
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "seed_shopify_product_tags.json")
    try:
        seed = json.load(open(path))
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="seed_shopify_product_tags.json not found on server")
    by_title, by_alnum = seed.get("by_title", {}), seed.get("by_alnum", {})
    norm  = lambda t: " ".join((t or "").lower().split())
    alnum = lambda t: re.sub(r"[^a-z0-9]+", "", (t or "").lower())

    products = db.query(models.Product).filter(models.Product.status != "DISCONTINUED").all()
    matched, total_tags = 0, 0
    for p in products:
        tags = by_title.get(norm(p.name)) or by_alnum.get(alnum(p.name))
        if not tags:
            continue
        matched += 1
        total_tags += len(tags)
        if not dry_run:
            for link in [l for l in p.tag_links if l.source == "ai"]:
                p.tag_links.remove(link)   # drop AI-guessed tags; keep manual
            db.flush()
            tag_service.apply_tags(db, p, tags, source="shopify", replace=True)
    if not dry_run:
        audit_log.record(db, action="product.sync_shopify_tags", actor=user, entity_type="product",
                         entity_label="shopify-tags-sync",
                         details={"products": len(products), "matched": matched,
                                  "total_tags": total_tags}, request=request)
        db.commit()
    return {"products": len(products), "matched": matched,
            "unmatched": len(products) - matched,
            "match_pct": round(100 * matched / len(products), 1) if products else 0,
            "avg_tags_per_match": round(total_tags / matched, 1) if matched else 0,
            "dry_run": dry_run}


def _load_cat_rules(db: Session) -> dict:
    rules = db.query(models.CategoryRule).all()
    return {r.category: r for r in rules}


def _base_query(db: Session):
    # selectinload issues one IN(...) query per relationship (keyed on the FK indexes
    # added in run_migrations) instead of subqueryload's correlated re-query — markedly
    # faster ORM hydration when listing all ~3.4k products.
    return db.query(models.Product).options(
        selectinload(models.Product.channels),
        selectinload(models.Product.stock_levels),
        selectinload(models.Product.product_suppliers).selectinload(models.ProductSupplier.supplier),
        selectinload(models.Product.product_suppliers).selectinload(models.ProductSupplier.mbb_term_list),
        selectinload(models.Product.sales_velocity),
    )


@router.get("")
def list_products(
    search:    Optional[str]  = Query(None),
    category:  Optional[str]  = Query(None),
    supplier:  Optional[str]  = Query(None),
    status:    Optional[str]  = Query(None),
    low_stock: bool           = Query(False),
    channel:   Optional[str]  = Query(None),
    page:      int            = Query(1, ge=1),
    limit:     int            = Query(5000, ge=1, le=20000),
    offset:    Optional[int]  = Query(None, ge=0),   # explicit row offset (overrides page) — lets the progressive loader use a small first page then larger background pages
    db: Session = Depends(database.get_db),
):
    q = _base_query(db)

    if status:
        q = q.filter(models.Product.status == status.upper())
    else:
        q = q.filter(models.Product.status != 'DISCONTINUED')

    if search:
        term = f"%{search}%"
        q = q.filter(
            models.Product.name.ilike(term) |
            models.Product.sku_code.ilike(term) |
            models.Product.brand.ilike(term)
        )
    if category:
        q = q.filter(models.Product.category == category)

    if supplier:
        q = q.join(models.ProductSupplier).join(models.Supplier).filter(
            models.Supplier.name.ilike(f"%{supplier}%")
        )

    if channel:
        q = q.join(models.ProductChannel).filter(
            models.ProductChannel.channel == channel,
            models.ProductChannel.is_active == 1,
        )

    cat_rules = _load_cat_rules(db)
    verified_skus = _verified_skus_cached(db)
    ordered = q.order_by(models.Product.category, models.Product.name)
    eff_offset = offset if offset is not None else (page - 1) * limit

    def _mark(rows):
        for r in rows:
            r["hitl_verified"] = r["sku_code"] in verified_skus
        return rows

    if low_stock:
        # low_stock filters on the COMPUTED woc, so every row must be built before we can
        # filter + slice — this path can't be pushed into SQL.
        result = _mark([product_to_dict(p, cat_rules) for p in ordered.all()])
        result = [r for r in result if r["woc"] is not None and r["woc"] < 2]
        total = len(result)
        items = result[eff_offset:eff_offset + limit]
    else:
        # Efficient pagination: a cheap SQL COUNT for the total, and we build ONLY the one page
        # being returned (not all ~11k rows). This is what lets the inventory screen fetch a
        # small first page fast and stream the remaining pages in the background.
        total = ordered.count()
        items = _mark([product_to_dict(p, cat_rules)
                       for p in ordered.offset(eff_offset).limit(limit).all()])

    # Return a Response directly: our dicts are already pure JSON primitives, so we skip
    # FastAPI's jsonable_encoder (which otherwise re-walks the structure and dominated latency).
    # ORJSONResponse renders with orjson (faster than stdlib json).
    return ORJSONResponse({
        "now": datetime.utcnow().isoformat(),   # server clock for the /changes delta cursor
        "total": total,
        "page": page,
        "limit": limit,
        "items": items,
    })


@router.get("/stream")
def stream_products(status: Optional[str] = Query(None)):
    """Stream the inventory as NDJSON: the first line is the meta ({"_meta": {total, now}}), then
    one product per line, built + flushed as each is read from the DB (yield_per keeps memory
    flat). Lets the inventory screen paint rows continuously as they arrive instead of waiting for
    whole pages. The session lives for the entire stream, so it's created here rather than via
    Depends (whose cleanup could close it mid-stream)."""
    def generate():
        import orjson
        db = database.SessionLocal()
        try:
            q = _base_query(db)
            q = (q.filter(models.Product.status == status.upper()) if status
                 else q.filter(models.Product.status != 'DISCONTINUED'))
            ordered = q.order_by(models.Product.category, models.Product.name)
            cat_rules = _load_cat_rules(db)
            verified = _verified_skus_cached(db)
            yield orjson.dumps({"_meta": {"total": ordered.count(),
                                          "now": datetime.utcnow().isoformat()}}) + b"\n"
            # Modest yield_per keeps memory flat and makes rows arrive in small, frequent bursts
            # (smoother on screen than fewer big batches). orjson serialization is ~5-8x faster
            # than stdlib json, which is most of the remaining per-row cost.
            for p in ordered.yield_per(200):
                d = product_to_dict(p, cat_rules)
                d["hitl_verified"] = d["sku_code"] in verified
                yield orjson.dumps(d) + b"\n"
        finally:
            db.close()
    return StreamingResponse(generate(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ── CSV column contract (export.csv ⟷ import-csv) ────────────────────────────────
def _blank(v):
    """None -> '' for CSV output, but keep 0 / 0.0 (a real value)."""
    return "" if v is None else v

# Editable columns: written by export.csv AND accepted by import-csv — the round-trip set.
# A module-level assert (next to the import helpers below) keeps the two in lock-step.
_EDITABLE_COLS = [
    "name", "brand", "category", "status", "hero_sku",
    "uom", "pack_unit", "units_per_pack", "min_purchase_qty", "min_sellable_qty",
    "weight_g", "weight_unit",
    "supplier_name", "basic_cost", "rrp", "notes",
]
# Read-only reference columns: exported for context, ignored by import-csv.
_READONLY_COLS = [
    "supplier_code", "supplier_sku", "cost_last_updated",
    "clinic_selling_price", "shopify_selling_price", "clinic_gp_pct", "shopify_gp_pct",
    "gp_floor", "clinic_qty", "warehouse_qty", "total_qty", "weekly_demand", "woc",
]


@router.get("/export.csv")
def export_products_csv(
    search:   Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    supplier: Optional[str] = Query(None),
    db: Session = Depends(database.get_db),
):
    """Download inventory as CSV for external validation."""
    q = _base_query(db).filter(models.Product.status != "DISCONTINUED")
    if search:
        term = f"%{search}%"
        q = q.filter(models.Product.name.ilike(term) | models.Product.sku_code.ilike(term))
    if category:
        q = q.filter(models.Product.category == category)
    if supplier:
        q = q.join(models.ProductSupplier).join(models.Supplier).filter(
            models.Supplier.name.ilike(f"%{supplier}%")
        )
    products = q.order_by(models.Product.category, models.Product.name).all()
    cat_rules = _load_cat_rules(db)

    output = io.StringIO()
    fieldnames = ["sku_code", *_EDITABLE_COLS, *_READONLY_COLS]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for p in products:
        d = product_to_dict(p, cat_rules)
        ch_map = {c["channel"]: c for c in d["channels"]}
        writer.writerow({
            "sku_code":              d["sku_code"],
            # --- editable (round-trips with Batch update / import-csv) ---
            "name":                  d["name"],
            "brand":                 d["brand"] or "",
            "category":              d["category"],
            "status":                d["status"],
            "hero_sku":              1 if d.get("hero_sku") else 0,
            "uom":                   d["uom"] or "",
            "pack_unit":             d.get("pack_unit") or "",
            "units_per_pack":        _blank(d.get("units_per_pack")),
            "min_purchase_qty":      _blank(d.get("min_purchase_qty")),
            "min_sellable_qty":      _blank(d.get("min_sellable_qty")),
            "weight_g":              _blank(d.get("weight_g")),
            "weight_unit":           p.weight_unit or "",   # raw DB value (d defaults to 'kg' for display)
            "supplier_name":         d["supplier_name"] or "",
            "basic_cost":            _blank(d.get("primary_cost")),
            "rrp":                   _blank(p.rrp),
            "notes":                 d.get("notes") or "",
            # --- read-only reference (ignored on import) ---
            "supplier_code":         d["supplier_code"] or "",
            "supplier_sku":          d["supplier_sku"] or "",
            "cost_last_updated":     (d["cost_last_updated"] or "")[:10],
            "clinic_selling_price":  ch_map.get("clinic", {}).get("selling_price", "") or "",
            "shopify_selling_price": ch_map.get("shopify", {}).get("selling_price", "") or "",
            "clinic_gp_pct":         f'{ch_map["clinic"]["gp_pct"]*100:.1f}%' if ch_map.get("clinic", {}).get("gp_pct") is not None else "",
            "shopify_gp_pct":        f'{ch_map["shopify"]["gp_pct"]*100:.1f}%' if ch_map.get("shopify", {}).get("gp_pct") is not None else "",
            "gp_floor":              f'{d["gp_floor"]*100:.0f}%',
            "clinic_qty":            d["clinic_qty"],
            "warehouse_qty":         d["warehouse_qty"],
            "total_qty":             d["total_qty"],
            "weekly_demand":         d["weekly_demand"] or "",
            "woc":                   d["woc"] or "",
        })

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ims_inventory.csv"},
    )


# Column order mirrors the ops "Hill's / Royal Canin — Verification for IMS" sheet exactly, so the
# export sits side-by-side with it for audit/review.
_MARGIN_COLS = [
    "Supplier SKU", "SKU", "Name", "Description of MBB Terms [Needed]?",
    "MBB Terms (Tier 1)", "MBB Terms (Tier 2)", "MBB Terms (Tier 3)", "MBB Terms (Tier 4)",
    "Cost to Hit MBB", "MBB Cost",
    "SF Express (Logistic Cost) Basic (Shopify)", "SF Express (Logistic Cost) MBB (Shopify)",
    "Selling Price (Shopify)", "Platform Charges (HKTVmall)", "Selling Price (HKTVmall)",
    "Gross Margin (Basic) %", "Gross Margin (MBB) %",
    "Net Margin After Fees (Basic - Shopify) $", "Net Margin After Fees (Basic - Shopify) %",
    "Net Margin After Fees (Basic - HKTVMall) $", "Net Margin After Fees (Basic - HKTVMall) %",
    "Net Margin After Fees (Shopify, MBB) $", "Net Margin % After Fees (Shopify, MBB) %",
    "Net Margin After Fees (HKTVMall, MBB) $", "Net Margin % After Fees (HKTVMall, MBB) %",
    "Weight per Unit", "Brand", "Category", "Status", "Storage Rule", "Supplier",
    "Units Per Pack", "Unit Cost (HKD)", "Cost that we checked (BASIC COST)",
    "Clinic Price", "Shopify Price", "HKTV Price", "Clinic GP%", "Shopify GP%", "HKTV GP%",
    "GP Floor", "Clinic Qty", "Warehouse Qty", "Total Qty", "120d Sales", "Data Grade",
    "Issues", "RRP",
]


@router.get("/export-margins.csv")
def export_margins_csv(
    search:   Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    supplier: Optional[str] = Query(None),
    db: Session = Depends(database.get_db),
):
    """Per-SKU margin breakdown as CSV, matching the ops verification sheet's columns (gross/net
    after fees × basic/MBB × Shopify/HKTV, cost-to-hit MBB, etc.) for data audit/review.
    Read-only — every value comes from the same margin_range the detail view already computes."""
    q = _base_query(db).filter(models.Product.status != "DISCONTINUED")
    if search:
        term = f"%{search}%"
        q = q.filter(models.Product.name.ilike(term) | models.Product.sku_code.ilike(term))
    if category:
        q = q.filter(models.Product.category == category)
    if supplier:
        q = q.join(models.ProductSupplier).join(models.Supplier).filter(
            models.Supplier.name.ilike(f"%{supplier}%")
        )
    products = q.order_by(models.Product.category, models.Product.name).all()
    cat_rules = _load_cat_rules(db)

    def pct(v):   return f"{v * 100:.2f}%" if v is not None else ""
    def money(v): return f"{v:.2f}" if v is not None else ""

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_MARGIN_COLS, extrasaction="ignore")
    writer.writeheader()

    for p in products:
        d = product_to_dict(p, cat_rules, include_margin_range=True)
        mr = d.get("margin_range") or {}
        mrc = {c["channel"]: c for c in (mr.get("channels") or [])}   # net / mbb / SF per channel
        pcm = {c["channel"]: c for c in d["channels"]}                # gross-basic gp_pct per channel
        sh, hk = mrc.get("shopify") or {}, mrc.get("hktv") or {}
        notes = [t["note"] for t in (mr.get("mbb_term_margins") or []) if t.get("note")]

        def net_dollar(ch, key):   # net $ = net % × that channel's price (honors the live formula)
            c = mrc.get(ch) or {}
            m, spx = c.get(key), c.get("selling_price")
            return money(m * spx) if (m is not None and spx) else ""

        sku = (d["sku_code"] or "").strip()
        issues = []
        if not (sku.isdigit() and len(sku) >= 6):  issues.append("No valid SKU")
        if not d.get("supplier_name"):             issues.append("No supplier")
        if d.get("primary_cost") is None:          issues.append("No cost")
        if d.get("units_per_pack") is None:        issues.append("No pack size")
        if any(c.get("recommendation") == "Raise price ⚠" for c in d["channels"]):
            issues.append("Low margin")

        writer.writerow({
            "Supplier SKU": d.get("supplier_sku") or "",
            "SKU": d["sku_code"],
            "Name": d["name"],
            "Description of MBB Terms [Needed]?": "",
            "MBB Terms (Tier 1)": notes[0] if len(notes) > 0 else "",
            "MBB Terms (Tier 2)": notes[1] if len(notes) > 1 else "",
            "MBB Terms (Tier 3)": notes[2] if len(notes) > 2 else "",
            "MBB Terms (Tier 4)": notes[3] if len(notes) > 3 else "",
            "Cost to Hit MBB": money(mr.get("mbb_min_spend")),
            "MBB Cost": money(mr.get("mbb_cost")),
            "SF Express (Logistic Cost) Basic (Shopify)": money(sh.get("delivery_cost")),
            "SF Express (Logistic Cost) MBB (Shopify)": money(sh.get("delivery_cost")),
            "Selling Price (Shopify)": _blank(sh.get("selling_price")),
            "Platform Charges (HKTVmall)": pct(hk.get("channel_fee_pct")),
            "Selling Price (HKTVmall)": _blank(hk.get("selling_price")),
            "Gross Margin (Basic) %": pct((pcm.get("shopify") or {}).get("gp_pct")),
            "Gross Margin (MBB) %": pct(sh.get("gp_pct_mbb")),
            "Net Margin After Fees (Basic - Shopify) $": net_dollar("shopify", "basic_margin"),
            "Net Margin After Fees (Basic - Shopify) %": pct(sh.get("basic_margin")),
            "Net Margin After Fees (Basic - HKTVMall) $": net_dollar("hktv", "basic_margin"),
            "Net Margin After Fees (Basic - HKTVMall) %": pct(hk.get("basic_margin")),
            "Net Margin After Fees (Shopify, MBB) $": net_dollar("shopify", "mbb_margin"),
            "Net Margin % After Fees (Shopify, MBB) %": pct(sh.get("mbb_margin")),
            "Net Margin After Fees (HKTVMall, MBB) $": net_dollar("hktv", "mbb_margin"),
            "Net Margin % After Fees (HKTVMall, MBB) %": pct(hk.get("mbb_margin")),
            "Weight per Unit": f"{p.weight_g / 1000:g}kg" if p.weight_g else "",
            "Brand": d.get("brand") or "",
            "Category": d["category"],
            "Status": d["status"],
            "Storage Rule": p.storage_rule or "",
            "Supplier": d.get("supplier_name") or "",
            "Units Per Pack": _blank(d.get("units_per_pack")),
            "Unit Cost (HKD)": _blank(d.get("unit_cost")),
            "Cost that we checked (BASIC COST)": _blank(d.get("primary_cost")),
            "Clinic Price": _blank((pcm.get("clinic") or {}).get("selling_price")),
            "Shopify Price": _blank((pcm.get("shopify") or {}).get("selling_price")),
            "HKTV Price": _blank((pcm.get("hktv") or {}).get("selling_price")),
            "Clinic GP%": pct((pcm.get("clinic") or {}).get("gp_pct")),
            "Shopify GP%": pct((pcm.get("shopify") or {}).get("gp_pct")),
            "HKTV GP%": pct((pcm.get("hktv") or {}).get("gp_pct")),
            "GP Floor": pct(d.get("gp_floor")),
            "Clinic Qty": d.get("clinic_qty"),
            "Warehouse Qty": d.get("warehouse_qty"),
            "Total Qty": d.get("total_qty"),
            "120d Sales": d.get("sales_120d"),
            "Data Grade": d.get("data_grade") or "",
            "Issues": " | ".join(issues),
            "RRP": _blank(p.rrp),
        })

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ims_margins.csv"},
    )


@router.get("/margins.json")
def margins_json(db: Session = Depends(database.get_db)):
    """Per-SKU margin summary (raw values), keyed by sku_code, for the All Inventory 'Margins'
    column view. Read-only — the same margin_range the detail view and the CSV export compute."""
    products = _base_query(db).filter(models.Product.status != "DISCONTINUED").all()
    cat_rules = _load_cat_rules(db)
    out: dict = {}
    for p in products:
        d = product_to_dict(p, cat_rules, include_margin_range=True)
        mr = d.get("margin_range") or {}
        mrc = {c["channel"]: c for c in (mr.get("channels") or [])}
        out[d["sku_code"]] = {
            "basic_cost":  mr.get("basic_cost") if mr.get("basic_cost") is not None else d.get("unit_cost"),
            "mbb_cost":    mr.get("mbb_cost"),
            "cost_to_hit": mr.get("mbb_min_spend"),
            "gp_floor":    d.get("gp_floor"),
            "ch": {
                ch: {
                    "price": (mrc.get(ch) or {}).get("selling_price"),
                    "nb":    (mrc.get(ch) or {}).get("basic_margin"),   # net-after-fees, basic cost
                    "nm":    (mrc.get(ch) or {}).get("mbb_margin"),     # net-after-fees, MBB cost
                }
                for ch in ("clinic", "shopify", "hktv")
            },
        }
    return ORJSONResponse(out)


@router.get("/summary")
def get_summary(db: Session = Depends(database.get_db)):
    active_products   = _base_query(db).filter(models.Product.status == 'ACTIVE').all()
    inactive_count    = db.query(models.Product).filter(models.Product.status == 'INACTIVE').count()
    discontinued_count = db.query(models.Product).filter(models.Product.status == 'DISCONTINUED').count()

    cat_rules = _load_cat_rules(db)
    dicts = [product_to_dict(p, cat_rules) for p in active_products]

    today = date.today()
    expiring_soon = 0
    for p in active_products:
        for exp in p.expiry_tracking:
            try:
                days = (date.fromisoformat(exp.expiry_date) - today).days
                if days < 90:
                    expiring_soon += 1
                    break
            except (ValueError, AttributeError):
                pass

    price_alerts = sum(
        1 for d in dicts
        if any(c["recommendation"] == "Raise price ⚠" for c in d["channels"])
    )

    return {
        "total_active":      len(dicts),
        "inactive_count":    inactive_count,
        "discontinued_count": discontinued_count,
        "low_stock_count":   sum(1 for d in dicts if d["woc"] is not None and d["woc"] < 2),
        "expiring_soon":     expiring_soon,
        "price_alerts":      price_alerts,
    }


def _is_verified(db: Session, sku: str) -> bool:
    last = (db.query(models.CatalogueAuditEvent.action)
            .filter(models.CatalogueAuditEvent.sku_code == sku,
                    models.CatalogueAuditEvent.action.in_(["confirm_match", "assign_new", "hitl_verify", "hitl_unverify"]))
            .order_by(models.CatalogueAuditEvent.created_at.desc()).first())
    return bool(last and last[0] != "hitl_unverify")


def _log_hitl_verify(db, user, product, request=None):
    """Log a manual HITL-verify event (marks the product's SKU verified). Inverse of hitl-unverify."""
    audit.log_event(db, action="hitl_verify", user=user, product_id=product.id,
                    sku_code=product.sku_code, details={"via": "edit"}, request=request)


@router.get("/changes")
def product_changes(since: str = Query(..., description="ISO timestamp of the last sync"),
                    db: Session = Depends(database.get_db)):
    """Delta feed for realtime inventory: every product touched since `since` (incl. ones
    just created/updated by other users confirming scans). Tiny payload vs the full list —
    the frontend polls this and merges, instead of refetching ~5 MB of products.
    NOTE: declared before /{sku} so 'changes' isn't swallowed by the catch-all route."""
    changed = (_base_query(db)
               .filter(models.Product.updated_at > since)
               .order_by(models.Product.updated_at)
               .limit(500)
               .all())
    cat_rules = _load_cat_rules(db)
    now = datetime.utcnow().isoformat()
    return JSONResponse({
        "now": now,
        "count": len(changed),
        "items": [product_to_dict(p, cat_rules) for p in changed],
    })


@router.get("/field-options")   # registered before the catch-all GET below
def field_options(db: Session = Depends(database.get_db), _user: models.User = Depends(require_user)):
    """Distinct existing values to populate the edit dialog's option (dropdown) fields."""
    def distinct(col):
        return sorted({str(v[0]).strip() for v in db.query(col).distinct().all() if v[0] and str(v[0]).strip()})
    return {
        "brands":        distinct(models.Product.brand),
        "subcategories": distinct(models.Product.subcategory),
        "uoms":          distinct(models.Product.uom),
        "pack_units":    distinct(models.Product.pack_unit),
    }


@router.get("/serving-publications", response_model=ServingPublicationPageResponse)
def list_serving_publications(
    search: str | None = Query(None),
    supplier_id: int | None = Query(None, gt=0),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_user),
):
    """Current approved catalogue snapshots for inventory consumers.

    This projection intentionally excludes superseded publications and never
    rebuilds values from mutable supplier-commercial tables.
    """
    page = CatalogueServingReadService(db).list_current(
        search=search,
        supplier_id=supplier_id,
        offset=offset,
        limit=limit,
    )
    return ServingPublicationPageResponse(
        total=page.total,
        offset=page.offset,
        limit=page.limit,
        items=list(page.items),
    )


@router.get(
    "/serving-publications/{canonical_sku:path}",
    response_model=ServingPublicationHistoryResponse,
)
def get_serving_publication_history(
    canonical_sku: str,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_user),
):
    """Current approved supplier offers and immutable history for one SKU."""
    history = CatalogueServingReadService(db).history_for_sku(canonical_sku)
    if not history:
        raise HTTPException(status_code=404, detail="No serving publication found for SKU")
    return ServingPublicationHistoryResponse(
        canonical_sku=canonical_sku,
        current_publications=[item.snapshot for item in history if item.is_current],
        publication_history=[
            {
                "is_current": item.is_current,
                "superseded_at": item.superseded_at,
                "snapshot": item.snapshot.model_dump(mode="json"),
            }
            for item in history
        ],
    )


@router.get("/{sku:path}/suppliers")   # registered before the catch-all GET below
def list_supplier_links(sku: str, db: Session = Depends(database.get_db),
                        _user: models.User = Depends(require_capability("product_edit"))):
    """Full per-supplier terms for the Manage Suppliers editor — includes the ordering-term columns
    (order/minimum increment + UOM, source, pricing note) and cost provenance the main product
    serializer omits. Read-only; effective_unit_cost = basic_cost / cost-basis units."""
    from services.pricing_service import get_unit_cost
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    sups = list(product.product_suppliers)
    costed = [s for s in sups if s.basic_cost is not None]
    pref_id = min(costed, key=lambda s: s.basic_cost).id if costed else None
    return {
        "sku_code": product.sku_code, "uom": product.uom,
        "suppliers": [{
            "id": s.id, "supplier_id": s.supplier_id,
            "name": s.supplier.name if s.supplier else None, "code": s.supplier.code if s.supplier else None,
            "supplier_sku": s.supplier_sku, "barcode": s.barcode,
            "basic_cost": s.basic_cost, "units_per_pack": s.units_per_pack,
            "effective_unit_cost": get_unit_cost(s),
            "order_increment_qty": s.order_increment_qty, "order_increment_uom": s.order_increment_uom,
            "minimum_order_qty": s.minimum_order_qty, "minimum_order_uom": s.minimum_order_uom,
            "minimum_order_source": s.minimum_order_source, "pricing_note": s.pricing_note,
            "cost_source": s.cost_source, "cost_source_ref": s.cost_source_ref, "pack_source": s.pack_source,
            "cost_updated_at": s.cost_updated_at, "uom_verified_at": s.uom_verified_at,
            "is_primary": bool(s.is_primary), "is_preferred": s.id == pref_id, "stock_status": s.stock_status,
        } for s in sups],
    }


@router.get("/{sku:path}/sku-history")   # registered before the catch-all GET below
def sku_history(sku: str, db: Session = Depends(database.get_db),
                _user: models.User = Depends(require_user)):
    """History of this product's SKU-code renames. Kept against the stable product.id,
    so the full chain survives further renames. Newest first; `from` = the prior code."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    rows = (db.query(models.AuditLog)
            .filter(models.AuditLog.action == "product.sku_change",
                    models.AuditLog.entity_id == str(product.id))
            .order_by(models.AuditLog.created_at.desc()).all())
    history = []
    for r in rows:
        d = {}
        if r.details:
            try:
                d = json.loads(r.details)
            except Exception:
                d = {}
        history.append({"from": d.get("from"), "to": d.get("to"),
                        "at": r.created_at, "by": (r.actor_display_name or r.actor_username)})
    return {"sku_code": sku, "history": history}


@router.get("/{sku:path}")   # :path so a sku_code containing '/' (e.g. "...7mg/ml") still matches
def get_product(sku: str, db: Session = Depends(database.get_db)):
    product = _base_query(db).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    cat_rules = _load_cat_rules(db)
    d = product_to_dict(product, cat_rules, include_margin_range=True)
    d["tags"] = tag_service.tags_for_product(db, product.id)
    d["tags_shopify"] = sorted({lb for src, lb in
        db.query(models.ProductTag.source, models.Tag.label)
          .join(models.Tag, models.ProductTag.tag_id == models.Tag.id)
          .filter(models.ProductTag.product_id == product.id).all() if src == "shopify"})
    d["hitl_verified"] = _is_verified(db, sku)
    return d


def _reopen_catalogue_items(db: Session, product: models.Product) -> int:
    """Flip the catalogue item(s) that produced this SKU back to 'pending' so the SKU
    returns to the onboarding confirm queue and can be re-reviewed/reconfirmed after an
    unverify. We clear the review stamps but keep all extracted/edited field values."""
    items = (db.query(models.CatalogueItem)
             .filter((models.CatalogueItem.matched_product_id == product.id) |
                     (models.CatalogueItem.assigned_sku == product.sku_code))
             .all())
    reopened = 0
    for it in items:
        if it.review_status in ('matched', 'new_sku'):
            it.review_status = 'pending'
            it.reviewed_by = None
            it.reviewed_at = None
            reopened += 1
    return reopened


@router.post("/{sku:path}/hitl-unverify")
def hitl_unverify(sku: str, db: Session = Depends(database.get_db), user: models.User = Depends(require_capability("product_edit"))):
    """Remove a SKU's HITL-verified status (logs an unverify event) so it drops out of the
    sheet push, and return its catalogue item(s) to the pending confirm queue so it can be
    re-reviewed and reconfirmed."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    reopened = _reopen_catalogue_items(db, product)
    audit.log_event(db, action="hitl_unverify", user=user, product_id=product.id, sku_code=sku,
                    details={"name": product.name, "reopened_items": reopened})
    db.commit()
    return {"sku_code": sku, "hitl_verified": False, "reopened_items": reopened}


class TagsBody(BaseModel):
    tags: list[str]


def _audit_product(db, user, action, product, request=None, **details):
    """Record a single product mutation in the audit log (entity_type=product).
    Used by every product-edit endpoint so no action goes uncaptured."""
    audit_log.record(db, action=action, actor=user, entity_type="product",
                     entity_id=product.id, entity_label=product.sku_code,
                     details=(details or None), request=request)


@router.patch("/{sku:path}/tags")
def set_product_tags(sku: str, body: TagsBody, db: Session = Depends(database.get_db),
                     user: models.User = Depends(require_capability("product_edit"))):
    """Replace a product's full tag set (manual edit). Tags are stored as 'manual'."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    tag_service.clear_tags(db, product)   # full replace across all sources
    tags = tag_service.apply_tags(db, product, body.tags, source='manual', user=user)
    _audit_product(db, user, "product.tags_update", product, tags=tags)
    db.commit()
    return {"sku_code": sku, "tags": tags}


class ProductUpdate(BaseModel):
    name:           Optional[str]   = None
    brand:          Optional[str]   = None
    category:       Optional[str]   = None
    status:         Optional[str]   = None
    hero_sku:       Optional[bool]  = None
    subcategory:    Optional[str]   = None   # functional / clinical class
    segment:        Optional[str]   = None   # 'vet' | 'non_vet' | '' to clear
    species:        Optional[str]   = None   # dog | cat | both | other
    rrp:            Optional[float] = None   # recommended retail price (HKD)
    storage_rule:   Optional[str]   = None   # 'any' | 'clinic_only'
    notes:          Optional[str]   = None
    uom:            Optional[str]   = None   # sell UOM (tablet, ml, g)
    pack_unit:      Optional[str]   = None   # buy UOM (box, bottle, strip)
    min_purchase_qty: Optional[int] = None   # supplier MOQ (packs)
    min_sellable_qty: Optional[int] = None   # smallest sellable qty in uom units (usually 1)
    weight_g:       Optional[float] = None   # net weight per sell-unit, canonical grams
    weight_unit:    Optional[str]   = None   # display unit: 'kg' | 'lb' (grams stays canonical)
    basic_cost:     Optional[float] = None   # updates primary supplier cost → cost_source: manual
    units_per_pack: Optional[int]   = None   # updates primary supplier pack size
    mark_verified:    Optional[bool]  = None  # log a HITL-verify event for this SKU on save


@router.patch("/{sku}")
def update_product(
    sku: str,
    body: ProductUpdate,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_capability("product_edit")),
):
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Sensitive fields (name / category / status / hero_sku) need the product_sensitive
    # capability — Admin + BizOps have it; data_entry does not.
    if not has_capability(current_user.role, "product_sensitive"):
        attempted = {f for f in SENSITIVE_PRODUCT_FIELDS if getattr(body, f, None) is not None}
        if attempted:
            raise HTTPException(
                status_code=403,
                detail=f"Your role cannot modify: {', '.join(sorted(attempted))}",
            )

    now = datetime.utcnow().isoformat()
    ps = next((s for s in product.product_suppliers if s.is_primary), None) or \
         (product.product_suppliers[0] if product.product_suppliers else None)

    # Snapshot before/after for the audit trail.
    _fields = {
        "name": product.name, "brand": product.brand, "category": product.category,
        "status": product.status, "hero_sku": bool(product.hero_sku), "notes": product.notes,
        "subcategory": product.subcategory, "segment": product.segment, "species": product.species,
        "rrp": product.rrp, "storage_rule": product.storage_rule,
        "uom": product.uom, "pack_unit": product.pack_unit,
        "min_purchase_qty": product.min_purchase_qty, "min_sellable_qty": product.min_sellable_qty,
        "weight_g": product.weight_g, "weight_unit": product.weight_unit,
        "basic_cost": (ps.basic_cost if ps else None),
        "units_per_pack": (ps.units_per_pack if ps else None),
    }
    before = dict(_fields)

    if body.name is not None:         product.name = body.name
    if body.brand is not None:        product.brand = body.brand
    if body.category is not None:     product.category = body.category
    if body.status is not None:       product.status = body.status.upper()
    if body.hero_sku is not None:     product.hero_sku = int(body.hero_sku)
    if body.subcategory is not None:  product.subcategory = body.subcategory
    if body.segment is not None:
        _seg = (body.segment or "").strip().lower()
        product.segment = _seg if _seg in ("vet", "non_vet") else None
    if body.species is not None:      product.species = body.species
    if body.rrp is not None:          product.rrp = body.rrp
    if body.storage_rule is not None: product.storage_rule = body.storage_rule
    if body.notes is not None:        product.notes = body.notes
    if body.uom is not None:          product.uom = body.uom
    if body.pack_unit is not None:    product.pack_unit = body.pack_unit
    if body.min_purchase_qty is not None: product.min_purchase_qty = body.min_purchase_qty
    if body.min_sellable_qty is not None: product.min_sellable_qty = body.min_sellable_qty
    if body.weight_g is not None:     product.weight_g    = body.weight_g
    if body.weight_unit is not None:  product.weight_unit = (body.weight_unit or 'kg')
    product.last_manual_edit_at = now
    product.last_manual_edit_by = current_user.display_name

    if ps:
        if body.basic_cost is not None:
            ps.basic_cost  = body.basic_cost
            ps.cost_source = 'manual'
            ps.updated_at  = now
        if body.units_per_pack is not None:
            ps.units_per_pack = body.units_per_pack
            ps.pack_source    = 'manual'   # protect from Sheet re-sync
            ps.updated_at     = now

    after = {
        "name": product.name, "brand": product.brand, "category": product.category,
        "status": product.status, "hero_sku": bool(product.hero_sku), "notes": product.notes,
        "subcategory": product.subcategory, "segment": product.segment, "species": product.species,
        "rrp": product.rrp, "storage_rule": product.storage_rule,
        "uom": product.uom, "pack_unit": product.pack_unit,
        "min_purchase_qty": product.min_purchase_qty, "min_sellable_qty": product.min_sellable_qty,
        "weight_g": product.weight_g, "weight_unit": product.weight_unit,
        "basic_cost": (ps.basic_cost if ps else None),
        "units_per_pack": (ps.units_per_pack if ps else None),
    }
    changes = audit_log.diff(before, after)
    if changes:
        audit_log.record(db, action="product.update", actor=current_user,
                         entity_type="product", entity_id=product.id, entity_label=product.sku_code,
                         details={"changes": changes}, request=request)

    if body.mark_verified and not _is_verified(db, product.sku_code):
        _log_hitl_verify(db, current_user, product, request)
        _VERIFIED_CACHE["skus"] = None
    product.updated_at = now
    db.commit()

    db.refresh(product)
    # Reload with relationships
    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


class SkuChange(BaseModel):
    new_sku: str


@router.patch("/{sku:path}/sku-code")
def change_sku_code(sku: str, body: SkuChange, request: Request,
                    db: Session = Depends(database.get_db),
                    current_user: models.User = Depends(require_capability("product_sensitive"))):
    """Rename a product's SKU code. The new code must be unique (409 otherwise).
    Denormalised sku_code copies (onboarding audit trail, matched catalogue items)
    are kept in lock-step so history + HITL-verified status follow the rename."""
    new = (body.new_sku or "").strip()
    if not new:
        raise HTTPException(status_code=400, detail="New SKU is required")
    if len(new) > 64:
        raise HTTPException(status_code=400, detail="SKU is too long (max 64 characters)")
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if new == product.sku_code:
        return _return_product(db, sku)
    clash = db.query(models.Product.id).filter(models.Product.sku_code == new,
                                               models.Product.id != product.id).first()
    if clash:
        raise HTTPException(status_code=409, detail=f"SKU '{new}' already exists — pick a unique code")

    old = product.sku_code
    now = datetime.utcnow().isoformat()
    product.sku_code = new
    product.last_manual_edit_at = now
    product.last_manual_edit_by = current_user.display_name
    product.updated_at = now
    db.query(models.CatalogueAuditEvent).filter(models.CatalogueAuditEvent.sku_code == old)\
        .update({"sku_code": new}, synchronize_session=False)
    db.query(models.CatalogueItem).filter(models.CatalogueItem.assigned_sku == old)\
        .update({"assigned_sku": new}, synchronize_session=False)
    audit_log.record(db, action="product.sku_change", actor=current_user,
                     entity_type="product", entity_id=product.id, entity_label=new,
                     details={"from": old, "to": new}, request=request)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"SKU '{new}' already exists — pick a unique code")
    _VERIFIED_CACHE["skus"] = None   # drop the cached verified-SKU set so the old code stops showing as verified at once
    return _return_product(db, new)


# ── Shared field-apply, used by the CSV batch import below ───────────────────────
# Mirrors update_product()'s field semantics — keep the two in sync if fields change.
def _apply_product_update(product, ps, data: dict, now: str, editor_name: str):
    snap = lambda: {
        "name": product.name, "brand": product.brand, "category": product.category,
        "status": product.status, "hero_sku": bool(product.hero_sku), "notes": product.notes,
        "subcategory": product.subcategory, "species": product.species,
        "rrp": product.rrp, "storage_rule": product.storage_rule,
        "uom": product.uom, "pack_unit": product.pack_unit,
        "min_purchase_qty": product.min_purchase_qty, "min_sellable_qty": product.min_sellable_qty,
        "weight_g": product.weight_g, "weight_unit": product.weight_unit,
        "segment": product.segment,
        "basic_cost": (ps.basic_cost if ps else None),
        "supplier_id": (ps.supplier_id if ps else None),
        "units_per_pack": (ps.units_per_pack if ps else None),
        "barcode": (ps.barcode if ps else None),
        "supplier_sku": (ps.supplier_sku if ps else None),
        "order_increment_qty": (ps.order_increment_qty if ps else None),
        "order_increment_uom": (ps.order_increment_uom if ps else None),
        "minimum_order_qty": (ps.minimum_order_qty if ps else None),
        "minimum_order_uom": (ps.minimum_order_uom if ps else None),
        "minimum_order_source": (ps.minimum_order_source if ps else None),
        "pricing_note": (ps.pricing_note if ps else None),
    }
    before = snap()
    if "name" in data:             product.name = data["name"]
    if "brand" in data:            product.brand = data["brand"]
    if "category" in data:         product.category = data["category"]
    if "status" in data:           product.status = str(data["status"]).upper()
    if "hero_sku" in data:         product.hero_sku = int(bool(data["hero_sku"]))
    if "subcategory" in data:      product.subcategory = data["subcategory"]
    if "species" in data:          product.species = data["species"]
    if "rrp" in data:              product.rrp = data["rrp"]
    if "storage_rule" in data:     product.storage_rule = data["storage_rule"]
    if "notes" in data:            product.notes = data["notes"]
    if "uom" in data:              product.uom = data["uom"]
    if "pack_unit" in data:        product.pack_unit = data["pack_unit"]
    if "min_purchase_qty" in data: product.min_purchase_qty = data["min_purchase_qty"]
    if "min_sellable_qty" in data: product.min_sellable_qty = data["min_sellable_qty"]
    if "weight_g" in data:         product.weight_g = data["weight_g"]
    if "weight_unit" in data:      product.weight_unit = (data["weight_unit"] or "kg")
    if "segment" in data:
        _seg = str(data["segment"] or "").strip().lower()
        product.segment = _seg if _seg in ("vet", "non_vet") else None
    product.last_manual_edit_at = now
    product.last_manual_edit_by = editor_name
    if ps:
        if data.get("_supplier_id") is not None:
            ps.supplier_id = data["_supplier_id"]; ps.updated_at = now
        if data.get("basic_cost") is not None:
            ps.basic_cost = data["basic_cost"]; ps.cost_source = "manual"; ps.updated_at = now
        if data.get("units_per_pack") is not None:
            ps.units_per_pack = data["units_per_pack"]; ps.pack_source = "manual"; ps.updated_at = now
        # Pack-safe cost from the reading-export's per-sell-unit "Unit Cost (HKD)": restore the
        # stored whole-pack basic_cost (inverse of get_unit_cost). Explicit basic_cost wins.
        if data.get("unit_cost_in") is not None and data.get("basic_cost") is None:
            _u = data["unit_cost_in"]
            if ps.uom_verified_at and ps.units_per_pack and ps.units_per_pack > 1:
                ps.basic_cost = round(_u * ps.units_per_pack, 4)
            else:
                ps.basic_cost = _u
            ps.cost_source = "manual"; ps.updated_at = now
        if "barcode" in data:      ps.barcode      = (str(data["barcode"]).strip() or None);      ps.updated_at = now
        if "supplier_sku" in data: ps.supplier_sku = (str(data["supplier_sku"]).strip() or None); ps.updated_at = now
        # Ordering terms (order multiple / MOQ) — descriptive metadata; nothing here feeds cost.
        if "order_increment_qty" in data: ps.order_increment_qty = data["order_increment_qty"]; ps.updated_at = now
        if "order_increment_uom" in data: ps.order_increment_uom = (str(data["order_increment_uom"]).strip() or None); ps.updated_at = now
        if "minimum_order_qty" in data:   ps.minimum_order_qty   = data["minimum_order_qty"];   ps.updated_at = now
        if "minimum_order_uom" in data:   ps.minimum_order_uom   = (str(data["minimum_order_uom"]).strip() or None); ps.updated_at = now
        if "minimum_order_source" in data:
            _src = str(data["minimum_order_source"]).strip().lower()
            ps.minimum_order_source = _src if _src in (
                "inferred_from_order_multiple", "explicit_supplier_moq", "manual", "unknown") else None
            ps.updated_at = now
        if "pricing_note" in data:        ps.pricing_note        = (str(data["pricing_note"]).strip() or None); ps.updated_at = now
    after = snap()
    return before, after


# CSV columns we accept (aliases map to the canonical field). Empty cells = "no change".
_CSV_EDITABLE = {"name", "brand", "category", "status", "hero_sku", "notes", "uom", "pack_unit",
                 "min_purchase_qty", "min_sellable_qty", "weight_g", "weight_unit",
                 "supplier_name", "basic_cost", "unit_cost_in", "rrp", "units_per_pack", "segment",
                 "barcode", "supplier_sku", "subcategory", "storage_rule", "species",
                 "order_increment_qty", "order_increment_uom", "minimum_order_qty",
                 "minimum_order_uom", "minimum_order_source", "pricing_note"}
# Aliases let the items-table "Export CSV" (frontend handleExport, human-readable headers)
# round-trip through import-csv too, not just the backend export.csv. Keys are lower-cased.
# "unit cost (hkd)" is the export's PER-SELL-UNIT cost -> `unit_cost_in`, converted back to
# the stored whole-pack basic_cost in _apply_product_update (inverse of get_unit_cost).
_CSV_ALIASES = {"supplier": "supplier_name", "units per pack": "units_per_pack",
                "storage rule": "storage_rule", "unit cost (hkd)": "unit_cost_in"}
_CSV_INT   = {"min_purchase_qty", "min_sellable_qty", "units_per_pack",
              "order_increment_qty", "minimum_order_qty"}
_CSV_FLOAT = {"weight_g", "basic_cost", "unit_cost_in", "rrp"}
# Supplier-scoped fields: if a SKU has no product_supplier row yet, setting any of these on
# import creates one (so Cost Price / Supplier Name apply to supplier-less SKUs too).
# `_supplier_id` is synthesised from the supplier_name lookup below (not a raw CSV column).
_SUPPLIER_DATA_KEYS = ("basic_cost", "unit_cost_in", "_supplier_id", "units_per_pack", "barcode",
                       "supplier_sku", "order_increment_qty", "order_increment_uom",
                       "minimum_order_qty", "minimum_order_uom", "minimum_order_source",
                       "pricing_note")

# Lock-step guarantee: every editable column export.csv emits must be accepted by import-csv.
assert set(_EDITABLE_COLS) <= (_CSV_EDITABLE | set(_CSV_ALIASES)), \
    f"export.csv / import-csv column drift: {set(_EDITABLE_COLS) - (_CSV_EDITABLE | set(_CSV_ALIASES))}"


def _coerce_csv(key: str, val: str):
    if key == "hero_sku":
        return str(val).strip().lower() in ("1", "true", "yes", "y", "t")
    if key in _CSV_INT:
        return int(float(str(val).replace(",", "").strip()))
    if key in _CSV_FLOAT:
        f = float(str(val).replace("%", "").replace("$", "").replace(",", "").strip())
        if key == "mbb_discount_pct" and f > 1:   # accept "10" as 10%
            f = f / 100.0
        return f
    return str(val)


@router.post("/import-csv")
async def import_products_csv(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(False),
    mark_verified: bool = Query(False),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_capability("product_edit")),
):
    """Batch-update existing products from a CSV keyed by `sku_code`. Mirrors the export.csv
    columns. `dry_run=true` previews the changes without writing. Sensitive fields
    (name / category / status / hero_sku) are ignored for roles without product_sensitive."""
    content = await file.read()
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "sku_code" not in headers and "sku" not in headers:
        raise HTTPException(status_code=400, detail="CSV must include a 'sku_code' (or 'sku') column.")
    can_sensitive = has_capability(current_user.role, "product_sensitive")
    now = datetime.utcnow().isoformat()
    # name / normalized-name → Supplier, for resolving the editable supplier_name column
    suppliers_by_name: dict = {}
    for _s in db.query(models.Supplier).all():
        for _k in ((_s.name or "").strip().lower(), (_s.normalized_name or "").strip().lower()):
            if _k:
                suppliers_by_name.setdefault(_k, _s)
    summary = {"total": 0, "updated": 0, "unchanged": 0, "not_found": 0, "errors": 0, "verified": 0}
    rows: list[dict] = []
    found_products: list = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        sku = (row.get("sku_code") or row.get("sku") or "").strip()
        if not sku:
            continue
        summary["total"] += 1
        product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
        if not product:
            rows.append({"sku_code": sku, "status": "not_found"}); summary["not_found"] += 1; continue
        data, ignored = {}, []
        try:
            for col, val in row.items():
                key = _CSV_ALIASES.get(col, col)
                if key not in _CSV_EDITABLE or val is None or val == "":
                    continue
                if key in SENSITIVE_PRODUCT_FIELDS and not can_sensitive:
                    ignored.append(key); continue
                data[key] = _coerce_csv(key, val)
        except (ValueError, TypeError) as e:
            rows.append({"sku_code": sku, "status": "error", "error": f"bad value ({e})"})
            summary["errors"] += 1; continue
        # supplier_name is editable but not a raw model field — resolve it to a supplier link
        if "supplier_name" in data:
            _sname = str(data.pop("supplier_name")).strip()
            _sup = suppliers_by_name.get(_sname.lower()) if _sname else None
            if _sup is not None:
                data["_supplier_id"] = _sup.id
            elif _sname:
                ignored.append(f"supplier_name '{_sname}' (no matching supplier)")
        found_products.append(product)   # found + parsed OK → eligible for HITL-verify marking
        if not data:
            rows.append({"sku_code": sku, "status": "unchanged", "ignored": ignored}); summary["unchanged"] += 1; continue
        ps = next((s for s in product.product_suppliers if s.is_primary), None) or \
             (product.product_suppliers[0] if product.product_suppliers else None)
        # if the CSV names a supplier this SKU already links to, target that exact row
        _target_sid = data.get("_supplier_id")
        if _target_sid is not None:
            _existing = next((s for s in product.product_suppliers if s.supplier_id == _target_sid), None)
            if _existing is not None:
                ps = _existing; data.pop("_supplier_id", None)   # already linked — nothing to change
        # no supplier row yet but the CSV sets cost / MBB / supplier → create one so it applies
        if ps is None and any(k in data for k in _SUPPLIER_DATA_KEYS):
            ps = models.ProductSupplier(product_id=product.id, is_primary=1,
                                        cost_source="manual", pack_source="manual", updated_at=now)
            db.add(ps)
        before, after = _apply_product_update(product, ps, data, now, current_user.display_name)
        changes = audit_log.diff(before, after)
        if changes:
            product.updated_at = now
            if not dry_run:   # one per-SKU entry so the change shows in that SKU's own history
                audit_log.record(db, action="product.update", actor=current_user,
                                 entity_type="product", entity_id=product.id, entity_label=sku,
                                 details={"changes": changes, "via": "csv_batch"}, request=request)
            rows.append({"sku_code": sku, "status": "updated", "changes": changes, "ignored": ignored})
            summary["updated"] += 1
        else:
            rows.append({"sku_code": sku, "status": "unchanged", "ignored": ignored}); summary["unchanged"] += 1
    # HITL-verify: count found SKUs not already verified (so dry-run can preview the count);
    # only log the events + bust the cache when actually applying.
    if mark_verified and found_products:
        vset = _verified_skus(db)
        to_verify = [p for p in found_products if p.sku_code not in vset]
        summary["verified"] = len(to_verify)
        if not dry_run and to_verify:
            for p in to_verify:
                _log_hitl_verify(db, current_user, p, request)
            _VERIFIED_CACHE["skus"] = None
    if dry_run:
        db.rollback()
    else:
        if summary["updated"]:
            changed_skus = [r["sku_code"] for r in rows if r["status"] == "updated"]
            audit_log.record(db, action="product.bulk_update", actor=current_user,
                             entity_type="product", entity_id=None,
                             entity_label=f"{summary['updated']} SKUs via CSV",
                             details={"summary": summary, "skus": changed_skus[:200]}, request=request)
        db.commit()
    return ORJSONResponse({"dry_run": dry_run, "summary": summary, "rows": rows[:1000]})


def _return_product(db: Session, sku: str) -> dict:
    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    return product_to_dict(updated, _load_cat_rules(db), include_margin_range=True)


# Literal placeholders that must never be persisted as real values (Rule F) — spreadsheet/CSV
# exports leak these into string cells. Normalise them to None on write.
_PLACEHOLDERS = {"", "#n/a", "n/a", "na", "nan", "none", "null", "-", "—"}


def _clean_str(v):
    """Trim + drop placeholder junk. Returns None for empty/#N/A-style values, else the string."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in _PLACEHOLDERS else s


def _validate_supplier_terms(oiq, oiu, moq, mou):
    """Rule §7 hard check: a set order-increment / minimum-order quantity requires its UOM."""
    if oiq is not None and not (oiu or "").strip():
        raise HTTPException(status_code=400, detail="Order increment UOM is required when order increment qty is set.")
    if moq is not None and not (mou or "").strip():
        raise HTTPException(status_code=400, detail="Minimum order UOM is required when minimum order qty is set.")


class SupplierLink(BaseModel):
    supplier_id:           Optional[int]   = None
    supplier_sku:          Optional[str]   = None
    barcode:               Optional[str]   = None
    basic_cost:            Optional[float] = None
    units_per_pack:        Optional[int]   = None   # COST BASIS units = sellable units covered by basic_cost
    is_primary:            Optional[bool]  = None
    # Supplier ordering terms (descriptive; do NOT feed unit cost). Sentinel "" clears a field.
    order_increment_qty:   Optional[int]   = None
    order_increment_uom:   Optional[str]   = None
    minimum_order_qty:     Optional[int]   = None
    minimum_order_uom:     Optional[str]   = None
    minimum_order_source:  Optional[str]   = None
    pricing_note:          Optional[str]   = None


# ── Relational MBB terms (0..N per supplier link) ───────────────────────────
_MBB_KINDS = ("buy_x_get_y", "spend_discount", "tier", "flat_unit_cost")


class MbbTermBody(BaseModel):
    kind:         Optional[str]   = None   # buy_x_get_y | spend_discount | tier | flat_unit_cost
    min_qty:      Optional[int]   = None
    min_spend:    Optional[float] = None
    free_qty:     Optional[int]   = None
    discount_pct: Optional[float] = None
    unit_cost:    Optional[float] = None
    note:         Optional[str]   = None
    sort_order:   Optional[int]   = None


def _find_supplier_link(db, sku, ps_id):
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    link = next((ps for ps in product.product_suppliers if ps.id == ps_id), None)
    if not link:
        raise HTTPException(status_code=404, detail="Supplier link not found")
    return product, link


@router.post("/{sku:path}/suppliers/{ps_id}/mbb-terms")
def add_mbb_term(sku: str, ps_id: int, body: MbbTermBody, db: Session = Depends(database.get_db),
                 current_user: models.User = Depends(require_capability("product_edit"))):
    """Add one Max-Bulk-Buy term to a supplier link."""
    product, link = _find_supplier_link(db, sku, ps_id)
    kind = (body.kind or "").strip()
    if kind not in _MBB_KINDS:
        raise HTTPException(status_code=400, detail="Invalid MBB term kind")
    now = datetime.utcnow().isoformat()
    term = models.MbbTerm(
        product_supplier_id=link.id, kind=kind, min_qty=body.min_qty, min_spend=body.min_spend,
        free_qty=body.free_qty, discount_pct=body.discount_pct, unit_cost=body.unit_cost,
        note=body.note, sort_order=body.sort_order if body.sort_order is not None else len(link.mbb_term_list),
        created_at=now)
    db.add(term)
    _audit_product(db, current_user, "product.mbb_term_add", product, supplier_id=link.supplier_id, kind=kind)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


@router.patch("/{sku:path}/suppliers/{ps_id}/mbb-terms/{term_id}")
def update_mbb_term(sku: str, ps_id: int, term_id: int, body: MbbTermBody,
                    db: Session = Depends(database.get_db),
                    current_user: models.User = Depends(require_capability("product_edit"))):
    """Edit one MBB term."""
    product, link = _find_supplier_link(db, sku, ps_id)
    term = next((t for t in link.mbb_term_list if t.id == term_id), None)
    if not term:
        raise HTTPException(status_code=404, detail="MBB term not found")
    if body.kind:
        k = body.kind.strip()
        if k not in _MBB_KINDS:
            raise HTTPException(status_code=400, detail="Invalid MBB term kind")
        term.kind = k
    for f in ("min_qty", "min_spend", "free_qty", "discount_pct", "unit_cost", "note", "sort_order"):
        v = getattr(body, f)
        if v is not None:
            setattr(term, f, v)
    now = datetime.utcnow().isoformat()
    term.updated_at = now
    _audit_product(db, current_user, "product.mbb_term_update", product, supplier_id=link.supplier_id)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


@router.delete("/{sku:path}/suppliers/{ps_id}/mbb-terms/{term_id}")
def delete_mbb_term(sku: str, ps_id: int, term_id: int, db: Session = Depends(database.get_db),
                    current_user: models.User = Depends(require_capability("product_edit"))):
    """Remove one MBB term."""
    product, link = _find_supplier_link(db, sku, ps_id)
    term = next((t for t in link.mbb_term_list if t.id == term_id), None)
    if not term:
        raise HTTPException(status_code=404, detail="MBB term not found")
    db.delete(term)
    now = datetime.utcnow().isoformat()
    _audit_product(db, current_user, "product.mbb_term_delete", product, supplier_id=link.supplier_id)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


@router.post("/{sku:path}/suppliers")
def add_supplier_link(sku: str, body: SupplierLink,
                      db: Session = Depends(database.get_db),
                      current_user: models.User = Depends(require_capability("product_edit"))):
    """Link a supplier to this product."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if body.supplier_id is None:
        raise HTTPException(status_code=400, detail="supplier_id is required")
    sup = db.query(models.Supplier).filter(models.Supplier.id == body.supplier_id).first()
    if not sup:
        raise HTTPException(status_code=404, detail="Supplier not found")
    if any(ps.supplier_id == body.supplier_id for ps in product.product_suppliers):
        raise HTTPException(status_code=409, detail=f"{sup.name} is already linked to this SKU")
    now = datetime.utcnow().isoformat()
    oi_uom = _clean_str(body.order_increment_uom)
    mo_uom = _clean_str(body.minimum_order_uom)
    _validate_supplier_terms(body.order_increment_qty, oi_uom, body.minimum_order_qty, mo_uom)
    first = len(product.product_suppliers) == 0
    make_primary = bool(body.is_primary) or first
    if make_primary:
        for ps in product.product_suppliers:
            ps.is_primary = 0
    link = models.ProductSupplier(
        product_id=product.id, supplier_id=body.supplier_id,
        supplier_sku=_clean_str(body.supplier_sku), barcode=_clean_str(body.barcode),
        basic_cost=body.basic_cost, units_per_pack=body.units_per_pack,
        cost_source='manual', pack_source='manual',
        order_increment_qty=body.order_increment_qty, order_increment_uom=oi_uom,
        minimum_order_qty=body.minimum_order_qty, minimum_order_uom=mo_uom,
        minimum_order_source=_clean_str(body.minimum_order_source), pricing_note=_clean_str(body.pricing_note),
        is_primary=1 if make_primary else 0, updated_at=now,
    )
    db.add(link)
    _audit_product(db, current_user, "product.supplier_add", product,
                   supplier_id=body.supplier_id, supplier=sup.name)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


@router.patch("/{sku:path}/suppliers/{ps_id}")
def update_supplier_link(sku: str, ps_id: int, body: SupplierLink,
                         db: Session = Depends(database.get_db),
                         current_user: models.User = Depends(require_capability("product_edit"))):
    """Edit one supplier link — change the supplier, its SKU/barcode/cost/pack, or make it primary."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    link = next((ps for ps in product.product_suppliers if ps.id == ps_id), None)
    if not link:
        raise HTTPException(status_code=404, detail="Supplier link not found")
    now = datetime.utcnow().isoformat()
    if body.supplier_id is not None and body.supplier_id != link.supplier_id:
        sup = db.query(models.Supplier).filter(models.Supplier.id == body.supplier_id).first()
        if not sup:
            raise HTTPException(status_code=404, detail="Supplier not found")
        if any(ps.supplier_id == body.supplier_id for ps in product.product_suppliers if ps.id != ps_id):
            raise HTTPException(status_code=409, detail=f"{sup.name} is already linked to this SKU")
        link.supplier_id = body.supplier_id
    # Only fields the client actually sent are touched (so "make primary" / stock calls that omit
    # the cost/ordering fields don't wipe them). A sent field with a blank/placeholder value clears it.
    sent = body.model_dump(exclude_unset=True)
    if "supplier_sku" in sent:  link.supplier_sku = _clean_str(sent["supplier_sku"])
    if "barcode" in sent:       link.barcode = _clean_str(sent["barcode"])
    if sent.get("basic_cost") is not None:
        link.basic_cost = sent["basic_cost"]; link.cost_source = 'manual'; link.cost_updated_at = now
    if sent.get("units_per_pack") is not None:
        link.units_per_pack = sent["units_per_pack"]; link.pack_source = 'manual'
    # Supplier ordering terms — descriptive; do NOT feed unit cost.
    if "order_increment_qty" in sent:   link.order_increment_qty = sent["order_increment_qty"]
    if "order_increment_uom" in sent:   link.order_increment_uom = _clean_str(sent["order_increment_uom"])
    if "minimum_order_qty" in sent:     link.minimum_order_qty = sent["minimum_order_qty"]
    if "minimum_order_uom" in sent:     link.minimum_order_uom = _clean_str(sent["minimum_order_uom"])
    if "minimum_order_source" in sent:  link.minimum_order_source = _clean_str(sent["minimum_order_source"])
    if "pricing_note" in sent:          link.pricing_note = _clean_str(sent["pricing_note"])
    _validate_supplier_terms(link.order_increment_qty, link.order_increment_uom,
                             link.minimum_order_qty, link.minimum_order_uom)
    if sent.get("is_primary"):
        for ps in product.product_suppliers:
            ps.is_primary = 1 if ps.id == ps_id else 0
    link.updated_at = now
    _audit_product(db, current_user, "product.supplier_update", product, ps_id=ps_id, supplier_id=link.supplier_id)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


@router.delete("/{sku:path}/suppliers/{ps_id}")
def delete_supplier_link(sku: str, ps_id: int,
                         db: Session = Depends(database.get_db),
                         current_user: models.User = Depends(require_capability("product_edit"))):
    """Remove a supplier link. A SKU must keep at least one supplier; deleting the primary promotes the cheapest remaining one."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if len(product.product_suppliers) <= 1:
        raise HTTPException(status_code=409, detail="Can't remove the only supplier — a SKU must keep at least one")
    link = next((ps for ps in product.product_suppliers if ps.id == ps_id), None)
    if not link:
        raise HTTPException(status_code=404, detail="Supplier link not found")
    now = datetime.utcnow().isoformat()
    was_primary = bool(link.is_primary)
    sup_id, sup_name = link.supplier_id, (link.supplier.name if link.supplier else None)
    remaining = [ps for ps in product.product_suppliers if ps.id != ps_id]
    db.delete(link)
    if was_primary and remaining and not any(ps.is_primary for ps in remaining):
        remaining.sort(key=lambda ps: (ps.basic_cost is None, ps.basic_cost or 0))
        remaining[0].is_primary = 1
        remaining[0].updated_at = now
    _audit_product(db, current_user, "product.supplier_delete", product,
                   ps_id=ps_id, supplier_id=sup_id, supplier=sup_name)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


@router.patch("/{sku:path}/suppliers/{supplier_id}/primary")
def set_primary_supplier(sku: str, supplier_id: int, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    """Switch which supplier is primary for this product."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    now = datetime.utcnow().isoformat()
    found = False
    for ps in product.product_suppliers:
        ps.is_primary = 1 if ps.supplier_id == supplier_id else 0
        ps.updated_at = now
        if ps.supplier_id == supplier_id:
            found = True
    if not found:
        raise HTTPException(status_code=404, detail="Supplier not linked to this product")
    _audit_product(db, _user, "product.primary_supplier", product, supplier_id=supplier_id)
    db.commit()
    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


class SupplierStockBody(BaseModel):
    status:              Optional[str] = None   # 'in_stock' | 'out_of_stock'
    expected_restock_at: Optional[str] = None   # YYYY-MM-DD
    note:                Optional[str] = None
    confirmed_by:        Optional[str] = None


@router.patch("/{sku:path}/suppliers/{ps_id}/stock")
def set_supplier_stock(sku: str, ps_id: int, body: SupplierStockBody,
                       db: Session = Depends(database.get_db),
                       current_user: models.User = Depends(require_capability("product_edit"))):
    """Set a supplier link's stock status. Going out_of_stock opens an OOS history event;
    coming back in closes the open one (restock_at = today)."""
    product, link = _find_supplier_link(db, sku, ps_id)
    new = (body.status or "").strip()
    if new not in ("in_stock", "out_of_stock"):
        raise HTTPException(status_code=400, detail="status must be 'in_stock' or 'out_of_stock'")
    now = datetime.utcnow().isoformat()
    today = now[:10]
    was = link.stock_status or "in_stock"
    who = (body.confirmed_by or "").strip() or current_user.display_name
    open_ev = next((e for e in link.stock_events if e.restock_at is None), None)

    if new == "out_of_stock":
        link.stock_status        = "out_of_stock"
        link.expected_restock_at = (body.expected_restock_at or "").strip() or None
        link.stock_note          = (body.note or "").strip() or None
        link.stock_confirmed_by  = who
        link.stock_updated_at    = now
        if was != "out_of_stock":
            link.reported_out_at = today
            db.add(models.SupplierStockEvent(product_supplier_id=link.id, out_at=today,
                                             note=link.stock_note, created_by=who, created_at=now))
        elif open_ev:
            open_ev.note = link.stock_note   # still out — keep the note in sync
    else:  # in_stock
        link.stock_status        = "in_stock"
        link.reported_out_at     = None
        link.expected_restock_at = None
        link.stock_confirmed_by  = who
        link.stock_updated_at    = now
        if open_ev:
            open_ev.restock_at = today

    _audit_product(db, current_user, "product.supplier_stock", product,
                   supplier_id=link.supplier_id, status=new)
    product.updated_at = now
    db.commit()
    return _return_product(db, sku)


class UomVerify(BaseModel):
    verified_by: Optional[str] = None


@router.patch("/{sku:path}/uom")
def verify_pack_size(sku: str, body: UomVerify, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    ps = next((p for p in product.product_suppliers if p.is_primary), None)
    if not ps and product.product_suppliers:
        ps = product.product_suppliers[0]
    if not ps:
        raise HTTPException(status_code=404, detail="No supplier record for this product")

    if not ps.units_per_pack:
        raise HTTPException(
            status_code=400,
            detail="Pack size not set — enter it via Data Review or Bulk Edit first",
        )

    now = datetime.utcnow().isoformat()
    ps.uom_verified_at = now
    ps.uom_verified_by = body.verified_by
    ps.updated_at      = now
    _audit_product(db, _user, "product.uom_verify", product, units_per_pack=ps.units_per_pack, verified_by=body.verified_by)
    db.commit()

    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


# ── Invoice cost lock (3-way match) ──────────────────────────────────────────
# Desmond reconciles PO ↔ delivery note ↔ invoice and locks the cost at
# invoice_matched tier — highest trust, protected from all future sync overwrites.

class InvoiceConfirm(BaseModel):
    invoice_ref:    str
    confirmed_cost: float


@router.post("/{sku:path}/cost/lock-invoice")
def lock_invoice_cost(
    sku: str,
    body: InvoiceConfirm,
    current_user: models.User = Depends(require_capability("product_edit")),
    db: Session = Depends(database.get_db),
):
    """Lock cost at invoice_matched tier after 3-way match confirmation."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    ps = _get_primary_ps(product) if hasattr(product, 'product_suppliers') else None
    # _get_primary_ps defined below — forward reference OK since we call it after module load
    ps = next((s for s in product.product_suppliers if s.is_primary), None) or \
         (product.product_suppliers[0] if product.product_suppliers else None)
    if not ps:
        raise HTTPException(status_code=400, detail="No supplier record — add a supplier first")

    now = datetime.utcnow().isoformat()
    ps.basic_cost          = body.confirmed_cost
    ps.cost_source         = 'invoice_matched'
    ps.cost_source_ref     = body.invoice_ref
    ps.cost_updated_at     = now
    ps.updated_at          = now
    product.last_manual_edit_at = now
    product.last_manual_edit_by = current_user.display_name if current_user else None
    product.updated_at          = now
    _audit_product(db, current_user, "product.cost_lock_invoice", product, invoice_ref=body.invoice_ref, cost=body.confirmed_cost)
    db.commit()

    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


# ── Conflict resolution endpoints ─────────────────────────────────────────────
# When Sheet shadow ≠ IMS locked value, the team resolves it here.
# These never touch the Sheet — they only update IMS.

def _get_primary_ps(product):
    ps = next((p for p in product.product_suppliers if p.is_primary), None)
    return ps or (product.product_suppliers[0] if product.product_suppliers else None)


@router.post("/{sku:path}/cost/accept-sheet")
def accept_sheet_cost(sku: str, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    """Accept the Sheet shadow cost as the new IMS cost (Sheet was right)."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    ps = _get_primary_ps(product)
    if not ps:
        raise HTTPException(status_code=404, detail="No supplier record found")
    if ps.basic_cost_sheet is None:
        raise HTTPException(status_code=400, detail="No Sheet shadow value to accept")
    now = datetime.utcnow().isoformat()
    ps.basic_cost      = ps.basic_cost_sheet
    # A human chose to accept the Sheet value — that's a manual confirmation,
    # not an OCR-catalogue cost. 'manual' (> 'sheet') protects it from re-seeds.
    ps.cost_source     = 'manual'
    ps.cost_updated_at = now
    ps.updated_at      = now
    _audit_product(db, _user, "product.cost_accept_sheet", product, basic_cost=ps.basic_cost)
    db.commit()
    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


@router.post("/{sku:path}/cost/dismiss-conflict")
def dismiss_cost_conflict(sku: str, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    """Mark IMS cost as correct; sync shadow to live to clear the conflict flag."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    ps = _get_primary_ps(product)
    if not ps:
        raise HTTPException(status_code=404, detail="No supplier record found")
    now = datetime.utcnow().isoformat()
    ps.basic_cost_sheet = ps.basic_cost   # shadow now matches live — conflict cleared
    ps.updated_at       = now
    _audit_product(db, _user, "product.cost_dismiss_conflict", product, basic_cost=ps.basic_cost)
    db.commit()
    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


@router.post("/{sku:path}/uom/accept-sheet")
def accept_sheet_uom(sku: str, body: UomVerify, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    """Accept Sheet pack size as the verified IMS value (Sheet was right)."""
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    ps = _get_primary_ps(product)
    if not ps:
        raise HTTPException(status_code=404, detail="No supplier record found")
    if ps.units_per_pack_sheet is None:
        raise HTTPException(status_code=400, detail="No Sheet shadow value to accept")
    now = datetime.utcnow().isoformat()
    ps.units_per_pack   = ps.units_per_pack_sheet
    ps.uom_verified_at  = now
    ps.uom_verified_by  = body.verified_by
    ps.updated_at       = now
    _audit_product(db, _user, "product.uom_accept_sheet", product, units_per_pack=ps.units_per_pack)
    db.commit()
    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


class ChannelPriceUpdate(BaseModel):
    selling_price: float


@router.patch("/{sku:path}/channels/{channel}/price")
def update_channel_price(
    sku: str,
    channel: str,
    body: ChannelPriceUpdate,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("product_edit")),
):
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    pc = db.query(models.ProductChannel).filter(
        models.ProductChannel.product_id == product.id,
        models.ProductChannel.channel == channel,
    ).first()
    if not pc:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not found for this product")

    _old_price = pc.selling_price
    pc.selling_price = body.selling_price
    pc.updated_at = datetime.utcnow().isoformat()
    _audit_product(db, _user, "product.price_update", product, channel=channel, **{"from": _old_price, "to": body.selling_price})
    db.commit()

    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


class StockAdjustBody(BaseModel):
    location: str   # 'clinic' | 'warehouse'
    delta:    float
    reason:   str


@router.patch("/{sku:path}/stock/adjust")
def adjust_stock(sku: str, body: StockAdjustBody, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if body.location not in ('clinic', 'warehouse'):
        raise HTTPException(status_code=400, detail="location must be 'clinic' or 'warehouse'")

    now = datetime.utcnow().isoformat()

    stock = db.query(models.StockLevel).filter(
        models.StockLevel.product_id == product.id,
        models.StockLevel.location == body.location,
    ).first()

    if stock:
        stock.qty = max(0, stock.qty + body.delta)
        stock.source = 'manual_adjustment'
        stock.updated_at = now
    else:
        stock = models.StockLevel(
            product_id=product.id,
            location=body.location,
            qty=max(0, body.delta),
            source='manual_adjustment',
            updated_at=now,
        )
        db.add(stock)

    db.add(models.StockAdjustment(
        product_id=product.id,
        location=body.location,
        delta=body.delta,
        reason=body.reason,
        adjusted_at=now,
    ))

    _audit_product(db, _user, "product.stock_adjust", product, location=body.location, delta=body.delta, reason=body.reason)
    db.commit()

    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


# Cost confidence priority — higher index = higher confidence.
# 'sheet' is the one-time Google-Sheet seed (lowest). 'catalogue' is the
# human-reviewed OCR catalogue flow — the top tier, beating even a 3-way-matched
# invoice, because a reviewer has confirmed it against the live supplier price list.
_COST_SOURCE_PRIORITY = ['sheet', 'manual', 'po_issued', 'invoice_matched', 'catalogue']


class CostUpdate(BaseModel):
    basic_cost:      float
    cost_source:     str   # sheet|manual|po_issued|invoice_matched|catalogue
    cost_source_ref: Optional[str] = None
    force:           bool = False   # bypass downgrade protection


@router.patch("/{sku:path}/cost")
def update_product_cost(sku: str, body: CostUpdate, db: Session = Depends(database.get_db), _user: models.User = Depends(require_capability("product_edit"))):
    if body.cost_source not in _COST_SOURCE_PRIORITY:
        raise HTTPException(status_code=400, detail=f"cost_source must be one of {_COST_SOURCE_PRIORITY}")

    product = db.query(models.Product).filter(models.Product.sku_code == sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    ps = next((p for p in product.product_suppliers if p.is_primary), None)
    if not ps and product.product_suppliers:
        ps = product.product_suppliers[0]
    if not ps:
        raise HTTPException(status_code=404, detail="No supplier record found for this product")

    current_priority = _COST_SOURCE_PRIORITY.index(ps.cost_source or 'manual')
    new_priority     = _COST_SOURCE_PRIORITY.index(body.cost_source)

    if new_priority < current_priority and not body.force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cost source downgrade blocked: current is '{ps.cost_source}', "
                f"incoming is '{body.cost_source}'. Pass force=true to override."
            ),
        )

    now = datetime.utcnow().isoformat()
    _old_cost = ps.basic_cost
    ps.basic_cost      = body.basic_cost
    ps.cost_source     = body.cost_source
    ps.cost_source_ref = body.cost_source_ref
    ps.cost_updated_at = now
    ps.updated_at      = now
    _audit_product(db, _user, "product.cost_update", product, **{"from": _old_cost, "to": body.basic_cost, "source": body.cost_source})
    db.commit()

    updated = _base_query(db).filter(models.Product.sku_code == sku).first()
    cat_rules = _load_cat_rules(db)
    return product_to_dict(updated, cat_rules, include_margin_range=True)


# Catch-all PATCH for sku_codes containing '/' (e.g. "…mg/ml") which the single-segment
# PATCH /{sku} (update_product, edit details) cannot match. Registered LAST so every specific
# /{sku:path}/… route above still matches first.
@router.patch("/{sku:path}")
def update_product_slash(sku: str, body: ProductUpdate, request: Request,
                         db: Session = Depends(database.get_db),
                         current_user: models.User = Depends(require_capability("product_edit"))):
    return update_product(sku, body, request, db, current_user)
