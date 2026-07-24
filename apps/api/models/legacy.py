from sqlalchemy import Column, Integer, String, Float, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String, unique=True, nullable=False)
    display_name  = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    role          = Column(String, nullable=False, default='bizops')  # 'admin' | 'bizops' | 'data_entry'
    is_active     = Column(Integer, nullable=False, default=1)
    created_at    = Column(String, nullable=False)
    updated_at    = Column(String, nullable=True)    # last time the account was changed by an admin
    last_login_at = Column(String, nullable=True)    # stamped on each successful login
    email         = Column(String, nullable=True)    # contact email (set on invite / during onboarding)
    # Invite-by-email onboarding: a pending invite has a token + expiry and is_active=0 until accepted.
    invite_token       = Column(String, nullable=True, index=True)
    invite_expires_at  = Column(String, nullable=True)
    invite_accepted_at = Column(String, nullable=True)
    invited_by         = Column(String, nullable=True)   # display_name of the admin who invited


class AuditLog(Base):
    """General append-only audit trail: logins, user-management, and who-edited-what across
    the system (products, catalogue, reference data, sheet sync). One row per event, never
    updated. Actor identity is snapshotted so it survives user rename/deactivation."""
    __tablename__ = "audit_log"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    created_at         = Column(String, nullable=False, index=True)
    action             = Column(String, nullable=False, index=True)   # e.g. login.success, product.update, user.role_change
    actor_user_id      = Column(Integer, nullable=True, index=True)
    actor_username     = Column(String, nullable=True)
    actor_display_name = Column(String, nullable=True)
    actor_role         = Column(String, nullable=True)
    entity_type        = Column(String, nullable=True, index=True)    # product | user | catalogue_item | category | collection | sheet | auth
    entity_id          = Column(String, nullable=True, index=True)
    entity_label       = Column(String, nullable=True)                # human label (sku / username / name)
    details            = Column(String, nullable=True)                # JSON: before/after diff, reason, etc.
    ip                 = Column(String, nullable=True)
    user_agent         = Column(String, nullable=True)


class AccessAcknowledgement(Base):
    """Records a tech-team auditor's click-wrap NDA acceptance + access request.
    On submit, the system also emails chris@algogroup.io with the requestor cc'd."""
    __tablename__ = "access_acknowledgements"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    github_username  = Column(String, nullable=False)
    full_name_typed  = Column(String, nullable=False)
    email_requestor  = Column(String, nullable=True)   # email to CC on the request notification
    terms_version    = Column(String, nullable=False, default='v1-2026-06')
    ip_address       = Column(String, nullable=True)
    accepted_at      = Column(String, nullable=False)
    email_sent_at    = Column(String, nullable=True)   # set if notification email succeeded
    email_send_error = Column(String, nullable=True)   # captured error if send failed


class CategoryRule(Base):
    __tablename__ = "category_rules"

    category          = Column(String, primary_key=True)
    gp_floor          = Column(Float, nullable=False)           # decimal e.g. 0.70
    storage_rule      = Column(String, nullable=False, default='any')  # 'clinic_only' | 'any'
    channel_restriction = Column(String, nullable=True)         # NULL | 'clinic'
    sku_digit         = Column(String, nullable=True)           # 1 leading digit for generated SKUs


class Supplier(Base):
    __tablename__ = "suppliers"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    code          = Column(String, unique=True, nullable=False)  # e.g. "ALF"
    name          = Column(String, nullable=False)
    contact_name  = Column(String)
    contact_email = Column(String)
    lead_time_days = Column(Integer)
    created_at    = Column(String, nullable=False)

    # ── Supplier-master fields (imported from the vet / non-vet / consolidated sheets) ──
    normalized_name    = Column(String)   # matching key (casefold, suffixes stripped)
    segment            = Column(String)   # 'vet' | 'non_vet' | 'unknown'
    type_of_brand      = Column(String)
    # commercial terms (consolidated sheet wins on overlaps)
    moq_value          = Column(String)
    moq_specific       = Column(String)
    credit_term        = Column(String)
    monthly_rebate     = Column(String)
    bulk_buy_structure = Column(String)
    # logistics
    delivery_time      = Column(String)
    delivery_charges   = Column(String)
    warehouse_pickup   = Column(String)
    order_days         = Column(String)   # e.g. "Mon,Wed,Thu"
    delivery_days      = Column(String)
    cut_off_time       = Column(String)
    holidays           = Column(String)
    # contacts
    key_contact        = Column(String)
    contact_phone      = Column(String)
    contact_mobile     = Column(String)
    bank_details       = Column(String)
    supply_agreement   = Column(String)
    other_details      = Column(String)
    # meta
    is_active          = Column(Integer, default=1)
    source             = Column(String)   # 'sheet_import' | 'manual'
    updated_at         = Column(String)
    raw_json           = Column(String)   # raw imported row, for audit

    product_suppliers = relationship("ProductSupplier", back_populates="supplier")
    aliases = relationship("SupplierAlias", back_populates="supplier", cascade="all, delete-orphan")
    brand_links = relationship("SupplierBrand", back_populates="supplier", cascade="all, delete-orphan")


class SupplierAlias(Base):
    """Alternate names/spellings/codes that resolve to a supplier (for catalogue matching)."""
    __tablename__ = "supplier_aliases"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    supplier_id      = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    alias            = Column(String, nullable=False)
    normalized_alias = Column(String, nullable=False, index=True)
    source           = Column(String)   # 'parenthetical' | 'manual' | 'learned' | 'name'
    created_at       = Column(String)

    supplier = relationship("Supplier", back_populates="aliases")
    __table_args__ = (UniqueConstraint("supplier_id", "normalized_alias", name="uq_supplier_alias"),)


class SupplierBrand(Base):
    """Brands a supplier carries — the strongest catalogue->supplier matching signal."""
    __tablename__ = "supplier_brands"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    supplier_id      = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    brand_name       = Column(String, nullable=False)
    normalized_brand = Column(String, nullable=False, index=True)
    is_fmcg          = Column(Integer)  # 1 / 0 / None
    created_at       = Column(String)

    supplier = relationship("Supplier", back_populates="brand_links")
    __table_args__ = (UniqueConstraint("supplier_id", "normalized_brand", name="uq_supplier_brand"),)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_status", "status"),
        Index("ix_products_category", "category"),
        Index("ix_products_updated_at", "updated_at"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    sku_code     = Column(String, unique=True, nullable=False, index=True)
    name         = Column(String, nullable=False)
    brand        = Column(String)
    category     = Column(String, nullable=False)
    subcategory  = Column(String, nullable=True)   # AI-detected functional/clinical class (e.g. "antibiotic")
    segment      = Column(String, nullable=True)   # 'vet' | 'non_vet' | NULL — veterinary vs retail SKU (derived from supplier segment)
    species      = Column(String, nullable=True)   # dog | cat | both | other (OCR)
    rrp          = Column(Float, nullable=True)     # recommended retail price, HKD (OCR)
    min_purchase_qty = Column(Integer, nullable=True)  # supplier MOQ per SKU (OCR) — number of packs you must order
    min_sellable_qty = Column(Integer, nullable=True)  # smallest sellable quantity in `uom` units — usually 1 (1 tablet)
    # Per-platform listing status, refreshed by the platform reconciliation.
    # NULL = not listed on that platform.
    shopify_status   = Column(String, nullable=True)   # active | archived | draft
    daysmart_status  = Column(String, nullable=True)   # active
    hktv_status      = Column(String, nullable=True)   # online | offline
    # Platform-recorded cost of goods (merchant-entered / computed on each platform).
    shopify_cost     = Column(Float, nullable=True)    # Shopify InventoryItem unitCost
    daysmart_cost    = Column(Float, nullable=True)    # DaySmart avg unit cost (balances API)
    hktv_cost        = Column(Float, nullable=True)    # HKTV product template Cost column
    uom          = Column(String)           # sell UOM: tablet, ml, g — the unit you sell one of
    pack_unit    = Column(String)           # buy UOM: box, bottle, strip — the supplier packaging unit
    storage_rule = Column(String, nullable=False, default='any')   # 'clinic_only' | 'any'
    status       = Column(String, nullable=False, default='ACTIVE') # ACTIVE | INACTIVE | DISCONTINUED
    hero_sku     = Column(Integer, nullable=False, default=0)
    notes        = Column(String)
    weight_g     = Column(Float, nullable=True)
    weight_unit  = Column(String, nullable=True)   # display/source unit: 'kg' (default) | 'lb' — grams is canonical
    last_manual_edit_at = Column(String, nullable=True)  # set only by human PATCH, never by sync
    last_manual_edit_by = Column(String, nullable=True)  # display_name of the user who made the edit
    created_at   = Column(String, nullable=False)
    updated_at   = Column(String, nullable=False)

    channels          = relationship("ProductChannel", back_populates="product")
    stock_levels      = relationship("StockLevel", back_populates="product")
    product_suppliers = relationship("ProductSupplier", back_populates="product")
    sales_velocity    = relationship("SalesVelocity", back_populates="product")
    competitor_prices = relationship("CompetitorPrice", back_populates="product")
    expiry_tracking   = relationship("ExpiryTracking", back_populates="product")
    stock_adjustments = relationship("StockAdjustment", back_populates="product")
    catalogue_items   = relationship("CatalogueItem", back_populates="matched_product")
    tag_links         = relationship("ProductTag", back_populates="product", cascade="all, delete-orphan")


class ProductSupplier(Base):
    __tablename__ = "product_suppliers"
    __table_args__ = (
        UniqueConstraint('product_id', 'supplier_id'),
        Index("ix_product_suppliers_product_id", "product_id"),
        Index("ix_product_suppliers_barcode", "barcode"),
        Index("ix_product_suppliers_sup_sku", "supplier_id", "supplier_sku"),
    )

    id              = Column(Integer, primary_key=True, autoincrement=True)
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=False)
    supplier_id     = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    supplier_sku    = Column(String)
    barcode         = Column(String)
    basic_cost      = Column(Float)          # the wholesale cost — the single cost all margin math runs on
    # (catalogue_cost / daysmart_cost / cost_reconciled_at retired — invoice reconciliation is a
    #  procurement concern; the OCR catalogue cost writes straight to basic_cost.)
    units_per_pack       = Column(Integer, nullable=True)  # IMS-live value (locked once uom_verified_at is set)
    units_per_pack_sheet = Column(Integer, nullable=True)  # shadow: last value seen from Sheet sync
    # Pack-size provenance — mirrors cost_source. 'sheet' is the one-time seed and the only
    # tier the Sheet sync may overwrite; 'catalogue' (OCR flow) and 'manual' edits are protected.
    pack_source     = Column(String, nullable=False, default='sheet')   # sheet|manual|catalogue
    uom_verified_at = Column(String, nullable=True)   # IMS-stamped date UOM/pack size was manually confirmed
    uom_verified_by = Column(String, nullable=True)   # name/initials of person who confirmed the pack size
    basic_cost_sheet = Column(Float, nullable=True)   # shadow: last basic_cost value seen from Sheet sync
    # Max-Bulk-Buy is now the relational `mbb_terms` table (0..N per supplier) — see MbbTerm.
    # The old flat scalars (bulk_buy_cost / bulk_buy_min_qty / mbb_terms / mbb_tiers / mbb_type /
    # mbb_min_amount / mbb_free_qty / mbb_discount_pct) are dropped in run_migrations.
    is_primary      = Column(Integer, nullable=False, default=0)
    # Cost confidence (Story 1.5) — priority: catalogue(OCR) > invoice_matched > po_issued > manual > sheet
    # 'sheet' = one-time Google-Sheet seed; 'catalogue' = human-reviewed OCR catalogue flow (top tier).
    cost_source     = Column(String, nullable=False, default='manual')   # sheet|manual|po_issued|invoice_matched|catalogue
    cost_source_ref = Column(String, nullable=True)   # e.g. "catalogue_import:42" or "invoice:17"
    cost_updated_at = Column(String, nullable=True)   # ISO datetime of last cost change specifically
    updated_at      = Column(String, nullable=False)
    # ── Supplier stock status (out-of-stock tracking) — per (SKU x supplier) link ──
    stock_status        = Column(String, nullable=False, default='in_stock')  # in_stock | out_of_stock
    reported_out_at     = Column(String, nullable=True)   # YYYY-MM-DD this supplier went OOS for this SKU
    expected_restock_at = Column(String, nullable=True)   # YYYY-MM-DD expected back in stock
    stock_confirmed_by  = Column(String, nullable=True)   # who set/confirmed the status
    stock_note          = Column(String, nullable=True)   # e.g. "seasonal supply gap"
    stock_updated_at    = Column(String, nullable=True)   # ISO datetime the status last changed
    # ── Ordering terms — order multiple / MOQ. Kept SEPARATE from units_per_pack (pack size) and
    #    basic_cost. Additive + nullable, with NO behavioural default: NULL = not-yet-set. Populated
    #    only by a separate reviewed remediation (never by ingestion/sync in this change). Nothing
    #    here feeds get_unit_cost — it is descriptive ordering metadata. `minimum_order_source` is an
    #    app-level enum (no DB CHECK): inferred_from_order_multiple | explicit_supplier_moq | manual | unknown.
    order_increment_qty  = Column(Integer, nullable=True)   # order in multiples of this many sell-units
    order_increment_uom  = Column(String, nullable=True)    # sell-unit the qty counts (e.g. product.uom, else 'sellable_unit')
    minimum_order_qty    = Column(Integer, nullable=True)   # smallest orderable quantity, in sell-units
    minimum_order_uom    = Column(String, nullable=True)    # sell-unit for minimum_order_qty
    minimum_order_source = Column(String, nullable=True)    # provenance of minimum_order_qty (app-level enum)
    pricing_note         = Column(String, nullable=True)    # free-text audit note (basis-fix provenance, human approval, etc.)

    product  = relationship("Product", back_populates="product_suppliers")
    supplier = relationship("Supplier", back_populates="product_suppliers")
    mbb_term_list = relationship("MbbTerm", back_populates="product_supplier",
                                 cascade="all, delete-orphan", order_by="MbbTerm.sort_order")
    stock_events = relationship("SupplierStockEvent", back_populates="product_supplier",
                                cascade="all, delete-orphan", order_by="SupplierStockEvent.out_at")


class SupplierStockEvent(Base):
    """One out-of-stock period for a (SKU x supplier) link. restock_at NULL = still out.
    Backs the OOS history + durations shown on the item page."""
    __tablename__ = "supplier_stock_events"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    product_supplier_id = Column(Integer, ForeignKey("product_suppliers.id"), nullable=False, index=True)
    out_at              = Column(String, nullable=False)   # YYYY-MM-DD it went out of stock
    restock_at          = Column(String, nullable=True)    # YYYY-MM-DD it came back (NULL = ongoing)
    note                = Column(String, nullable=True)
    created_by          = Column(String, nullable=True)
    created_at          = Column(String, nullable=False)

    product_supplier = relationship("ProductSupplier", back_populates="stock_events")


class MbbTerm(Base):
    """One Max-Bulk-Buy term on a (SKU x supplier) link. A ProductSupplier has 0..N of these —
    it replaces the old flat mbb_* scalars, which could only hold a SINGLE term. Each term is
    typed, and its effective per-SELL-unit cost is DERIVED from the per-unit base cost, so there
    is no stored per-box number to mis-divide (that was the source of the old basis bug)."""
    __tablename__ = "mbb_terms"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    product_supplier_id = Column(Integer, ForeignKey("product_suppliers.id"), nullable=False, index=True)
    kind                = Column(String, nullable=False)   # buy_x_get_y | spend_discount | tier | flat_unit_cost
    # unlock thresholds (either may apply to any kind)
    min_qty             = Column(Integer, nullable=True)   # units to unlock — the "buy X"
    min_spend           = Column(Float, nullable=True)     # HK$ to unlock
    # benefits — only the one matching `kind` is set
    free_qty            = Column(Integer, nullable=True)   # buy_x_get_y: get Y free
    discount_pct        = Column(Float, nullable=True)     # spend_discount: fraction off, e.g. 0.10
    unit_cost           = Column(Float, nullable=True)     # tier / flat_unit_cost: explicit per-SELL-unit cost
    note                = Column(String, nullable=True)    # human label (was the free-text mbb_terms)
    sort_order          = Column(Integer, nullable=False, default=0)
    created_at          = Column(String, nullable=False)
    updated_at          = Column(String, nullable=True)

    product_supplier = relationship("ProductSupplier", back_populates="mbb_term_list")


class ProductChannel(Base):
    __tablename__ = "product_channels"
    __table_args__ = (
        UniqueConstraint('product_id', 'channel'),
        Index("ix_product_channels_product_id", "product_id"),
    )

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    product_id          = Column(Integer, ForeignKey("products.id"), nullable=False)
    channel             = Column(String, nullable=False)   # 'clinic' | 'shopify' | 'hktv'
    is_active           = Column(Integer, nullable=False, default=1)
    selling_price       = Column(Float)
    has_dispensing_fee  = Column(Integer, nullable=False, default=0)
    channel_fee_pct     = Column(Float, nullable=True)     # e.g. 0.08 = 8% HKTV platform fee
    units_per_listing   = Column(Integer, nullable=True)   # how many sell-units per HKTV listing (e.g. 12 for a case)
    updated_at          = Column(String, nullable=False)

    product = relationship("Product", back_populates="channels")


class StockLevel(Base):
    __tablename__ = "stock_levels"
    __table_args__ = (
        UniqueConstraint('product_id', 'location'),
        Index("ix_stock_levels_product_id", "product_id"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    location    = Column(String, nullable=False)   # 'clinic' | 'warehouse'
    qty         = Column(Float, nullable=False, default=0)
    as_of_date  = Column(String)                   # YYYY-MM-DD of source export
    source      = Column(String, nullable=False, default='import')  # 'import' | 'manual_adjustment'
    updated_at  = Column(String, nullable=False)

    product = relationship("Product", back_populates="stock_levels")


class SalesVelocity(Base):
    __tablename__ = "sales_velocity"
    __table_args__ = (
        Index("ix_sales_velocity_product_id", "product_id"),
    )

    id            = Column(Integer, primary_key=True, autoincrement=True)
    product_id    = Column(Integer, ForeignKey("products.id"), nullable=False)
    weekly_demand = Column(Float, nullable=False, default=0)   # combined across channels (drives WOC)
    # Per-channel split (algo-dashboard multichannel sync); sum == weekly_demand. Null on legacy rows.
    weekly_demand_clinic  = Column(Float)
    weekly_demand_hktv    = Column(Float)
    weekly_demand_shopify = Column(Float)
    trend_json    = Column(String)   # JSON [["YYYY-MM", units], ...] last ~5 months (all channels) for the sales spark
    period_days   = Column(Integer, nullable=False, default=28)
    calculated_at = Column(String, nullable=False)
    source        = Column(String)   # 'shopify' | 'daysmart' | 'hktv' | 'algo_multichannel' | 'combined'

    product = relationship("Product", back_populates="sales_velocity")


class ExpiryTracking(Base):
    __tablename__ = "expiry_tracking"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    batch_ref   = Column(String)
    expiry_date = Column(String, nullable=False)   # YYYY-MM-DD
    qty         = Column(Float)
    location    = Column(String)
    created_at  = Column(String, nullable=False)

    product = relationship("Product", back_populates="expiry_tracking")


class CompetitorPrice(Base):
    __tablename__ = "competitor_prices"
    __table_args__ = (
        Index("ix_competitor_prices_product", "product_id"),
    )

    id              = Column(Integer, primary_key=True, autoincrement=True)
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=False)
    competitor_name = Column(String, nullable=False)
    channel         = Column(String)   # 'hktv' | 'shopify' | 'general'
    price           = Column(Float)    # last successfully-scraped selling price (HKD); kept on a failed re-scrape
    url             = Column(String)   # competitor product-page link the BO pasted
    platform        = Column(String)   # detected: shopify | opencart | woocommerce | hktvmall | generic
    in_stock        = Column(Integer)  # 1 | 0 | NULL (unknown) — from the last scrape
    title           = Column(String)   # product title seen on the competitor page (scrape sanity-check)
    last_checked    = Column(String)   # YYYY-MM-DD of the last scrape attempt
    last_status     = Column(String)   # 'ok' | 'no price found' | 'error: ...' — last scrape result
    notes           = Column(String)
    created_at      = Column(String, nullable=False)
    updated_at      = Column(String, nullable=False)

    product = relationship("Product", back_populates="competitor_prices")


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    location    = Column(String, nullable=False)
    delta       = Column(Float, nullable=False)   # positive = increase
    reason      = Column(String, nullable=False)
    adjusted_by = Column(String)
    adjusted_at = Column(String, nullable=False)

    product = relationship("Product", back_populates="stock_adjustments")


class CatalogueImport(Base):
    __tablename__ = "catalogue_imports"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    filename    = Column(String, nullable=False)
    format      = Column(String)   # 'pdf' | 'xlsx' | 'jpeg' | 'gdoc'
    imported_at = Column(String, nullable=False)
    status      = Column(String, nullable=False, default='pending')
    item_count  = Column(Integer)

    # ── Supplier detection / resolution (stage-1 supplier confirm) ──
    detected_supplier_name = Column(String)   # what the AI read off the document
    detected_brands        = Column(String)   # comma-joined brands detected
    supplier_confidence    = Column(Float)     # resolver confidence for the matched supplier
    supplier_source        = Column(String)   # 'user' | 'ai' | None
    supplier_status        = Column(String)   # 'confirmed' | 'needs_review'

    # ── Re-parse (RP-1.2): storage key of the persisted upload, for future re-OCR from source ──
    source_ref             = Column(String, nullable=True)

    items = relationship("CatalogueItem", back_populates="catalogue_import")


class CatalogueCostStaging(Base):
    __tablename__ = "catalogue_cost_staging"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    import_id          = Column(Integer, ForeignKey("catalogue_imports.id"), nullable=True)
    supplier_id        = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    raw_supplier_sku   = Column(String)
    matched_product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    match_confidence   = Column(Float)             # 0.0–1.0
    extracted_cost     = Column(Float)
    status             = Column(String, nullable=False, default='pending')  # pending|approved|rejected
    reviewed_by        = Column(String)
    reviewed_at        = Column(String)
    created_at         = Column(String, nullable=False)


class CatalogueItem(Base):
    __tablename__ = "catalogue_items"
    __table_args__ = (
        Index("ix_catalogue_items_review_status", "review_status"),
        Index("ix_catalogue_items_status_import", "review_status", "import_id", "confidence_score"),
        Index("ix_catalogue_items_import_id", "import_id"),
    )

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    import_id          = Column(Integer, ForeignKey("catalogue_imports.id"), nullable=False)
    supplier_id        = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    raw_description    = Column(String)   # product name shown for review (English after translation)
    original_description = Column(String, nullable=True)  # source text as printed, when translated from another language
    supplier_sku       = Column(String)
    barcode            = Column(String)
    cost_price         = Column(Float)
    uom                = Column(String)
    units_per_pack     = Column(Integer, nullable=True)  # how many sell-units per purchasable pack (the pack/box you buy whole)
    min_sellable_qty   = Column(Integer, nullable=True)  # smallest sellable quantity in `uom` units — usually 1 (1 tablet)
    brand              = Column(String, nullable=True)   # extracted brand (e.g. "Zoetis", "Dechra")
    variant            = Column(String, nullable=True)   # size/volume/flavour distinguishing sibling variants (e.g. "15ml")
    pack_size          = Column(String, nullable=True)   # raw pack-size string (e.g. "100 tabs/ box")
    max_bulk_buy_cost  = Column(Float, nullable=True)    # deepest-discount per-unit cost across all bulk tiers
    max_bulk_buy_min_qty = Column(Integer, nullable=True)  # qty needed to hit max_bulk_buy_cost
    bulk_buy_tiers     = Column(String)   # human-readable tier string (e.g. "5 bots @ 490; 10 bots @ 460")
    confidence_score   = Column(Float)    # 0.0–1.0
    confidence_detail  = Column(String)   # JSON: per-field confidence
    review_status      = Column(String, nullable=False, default='pending')
    # Skip bucket — a pending item the reviewer sets aside for later (stays undecided;
    # hidden from the active queue, surfaced in the Skipped view). Un-skip clears it.
    skipped            = Column(Integer, nullable=False, default=0)
    skipped_at         = Column(String, nullable=True)
    skipped_by         = Column(String, nullable=True)
    matched_product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    assigned_sku       = Column(String)
    reviewed_by        = Column(String)
    reviewed_at        = Column(String)
    created_at         = Column(String, nullable=False)
    ai_tags            = Column(String, nullable=True)   # JSON array of suggested free-form tags
    ai_category        = Column(String, nullable=True)   # AI-suggested SKU category
    ai_subcategory     = Column(String, nullable=True)   # AI-detected functional/clinical class
    # Additional OCR-extracted fields (v7 OCR-marked columns)
    species            = Column(String, nullable=True)   # dog | cat | both | other
    weight_grams       = Column(Float, nullable=True)    # net weight per sell-unit (canonical, grams)
    weight_unit        = Column(String, nullable=True)   # display/source unit: 'kg' (default) | 'lb'
    rrp                = Column(Float, nullable=True)     # recommended retail price (HKD)
    min_purchase_qty   = Column(Integer, nullable=True)  # supplier MOQ per SKU
    bulk_tiers         = Column(String, nullable=True)   # JSON: [{min_qty, unit_cost}]
    # ── Re-parse versioning (RP-1.1): which parser/prompt version last produced these fields,
    #    when it was last re-parsed, and from where ('text' = retained fields | 'source' = re-OCR). ──
    parser_version     = Column(String, nullable=True)
    reparsed_at        = Column(String, nullable=True)
    reparse_source     = Column(String, nullable=True)   # 'text' | 'source'

    catalogue_import  = relationship("CatalogueImport", back_populates="items")
    matched_product   = relationship("Product", back_populates="catalogue_items")


class CatalogueAuditEvent(Base):
    """Append-only trail of every human decision taken during catalogue onboarding.
    One row per action (confirm_match / assign_new / edit / reject / supplier_confirm),
    attributed to the authenticated user, linked to the catalogue item and — once it
    exists — the inventory Product / SKU. Never updated, only inserted."""
    __tablename__ = "catalogue_audit"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    item_id      = Column(Integer, ForeignKey("catalogue_items.id"), nullable=True, index=True)
    import_id    = Column(Integer, ForeignKey("catalogue_imports.id"), nullable=True, index=True)
    product_id   = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    sku_code     = Column(String, nullable=True, index=True)   # denormalised for cheap lookup-by-SKU
    action       = Column(String, nullable=False)              # confirm_match|assign_new|edit|reject|supplier_confirm
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    username     = Column(String, nullable=True)               # snapshot — survives user rename/delete
    display_name = Column(String, nullable=True)
    details      = Column(String, nullable=True)               # JSON: before/after, reason, match target, etc.
    created_at   = Column(String, nullable=False, index=True)


class ReparseBatch(Base):
    """RP-2.1 — one re-parse run over a scope (a SKU / import / supplier). Its ReparseChange rows are
    the reviewable diff; nothing writes to Product/ProductSupplier until a change is confirmed."""
    __tablename__ = "reparse_batch"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    scope_type     = Column(String, nullable=False)                    # 'item' | 'import' | 'supplier'
    scope_ref      = Column(String, nullable=False)                    # sku_code / import_id / supplier_id
    parser_version = Column(String, nullable=True)                     # version this run derived with
    mode           = Column(String, nullable=False, default='text')    # 'text' | 'source'
    status         = Column(String, nullable=False, default='open')    # 'open' | 'applied' | 'discarded'
    item_count     = Column(Integer, nullable=True)
    changed_count  = Column(Integer, nullable=True)
    created_at     = Column(String, nullable=False)
    created_by     = Column(String, nullable=True)

    changes = relationship("ReparseChange", back_populates="batch")


class ReparseChange(Base):
    """RP-2.1 — one field diff (old -> new) for one catalogue item within a batch, awaiting confirm.
    A confirmed change is applied via the normal commit path; cost-affecting writes are guarded."""
    __tablename__ = "reparse_change"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    batch_id          = Column(Integer, ForeignKey("reparse_batch.id"), nullable=False, index=True)
    catalogue_item_id = Column(Integer, ForeignKey("catalogue_items.id"), nullable=False)
    product_id        = Column(Integer, ForeignKey("products.id"), nullable=True)   # set once committed
    field             = Column(String, nullable=False)
    old_value         = Column(String, nullable=True)
    new_value         = Column(String, nullable=True)
    affects_cost      = Column(Integer, nullable=False, default=0)     # 1 if it moves effective unit cost
    eff_cost_before   = Column(Float, nullable=True)
    eff_cost_after    = Column(Float, nullable=True)
    status            = Column(String, nullable=False, default='pending')   # pending|confirmed|rejected|stale
    confirmed_by      = Column(String, nullable=True)
    confirmed_at      = Column(String, nullable=True)

    batch = relationship("ReparseBatch", back_populates="changes")


class Tag(Base):
    """A free-form (Shopify-style) product tag. `slug` is the normalised key
    (lowercase, single-spaced) used for matching; `label` is the display form."""
    __tablename__ = "tags"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    slug       = Column(String, unique=True, nullable=False, index=True)
    label      = Column(String, nullable=False)
    created_at = Column(String, nullable=False)

    product_links = relationship("ProductTag", back_populates="tag", cascade="all, delete-orphan")


class ProductTag(Base):
    """Many-to-many product↔tag, with provenance (AI vs human)."""
    __tablename__ = "product_tags"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    tag_id     = Column(Integer, ForeignKey("tags.id"), nullable=False, index=True)
    source     = Column(String, nullable=False, default='ai')   # ai | manual
    confidence = Column(Float, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(String, nullable=False)
    __table_args__ = (
        UniqueConstraint('product_id', 'tag_id', name='uq_product_tag'),
    )

    tag     = relationship("Tag", back_populates="product_links")
    product = relationship("Product", back_populates="tag_links")


class Collection(Base):
    """A Shopify-style smart collection: a saved rule (JSON) that dynamically
    selects products. Membership is evaluated on the fly, never stored."""
    __tablename__ = "collections"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    name         = Column(String, nullable=False)
    slug         = Column(String, unique=True, nullable=False, index=True)
    description  = Column(String, nullable=True)
    rule_json    = Column(String, nullable=False)   # {"match":"all|any","conditions":[...]}
    is_smart     = Column(Integer, nullable=False, default=1)
    ai_generated = Column(Integer, nullable=False, default=0)
    created_by   = Column(String, nullable=True)
    created_at   = Column(String, nullable=False)
    updated_at   = Column(String, nullable=False)


# ── Configuration-driven transformation engine (Phase A) ──────────────────────────
# Registry + versioned config that back backend/services/transform_engine.py. The engine seeds
# these to reproduce the previously hard-coded formulas exactly; editing arrives in Phases B/C.
class Transformation(Base):
    """Registry of every configurable transformation (margin / cost / WOC …). Descriptive
    metadata for the config UI + the engine; the editable content lives in TransformationValue."""
    __tablename__ = "transformations"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    key          = Column(String, unique=True, nullable=False, index=True)  # e.g. unit_cost, net_margin
    name         = Column(String, nullable=False)
    description  = Column(String, nullable=True)
    category     = Column(String, nullable=False)   # cost | margin | inventory | classification
    output_field = Column(String, nullable=True)    # the field this produces
    input_vars   = Column(String, nullable=True)    # JSON list of variable names the formula may use
    kind         = Column(String, nullable=False)   # formula | parameter | table
    sort_order   = Column(Integer, nullable=False, default=0)


class ConfigVersion(Base):
    """One immutable snapshot of the whole transformation config. Exactly one row has
    is_active=1. Editing clones the active values into a new version; rollback flips is_active."""
    __tablename__ = "config_versions"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    created_at        = Column(String, nullable=False)
    created_by        = Column(String, nullable=True)
    note              = Column(String, nullable=True)
    parent_version_id = Column(Integer, ForeignKey("config_versions.id"), nullable=True)
    is_active         = Column(Integer, nullable=False, default=0)


class TransformationValue(Base):
    """The editable content of one transformation within one config version."""
    __tablename__ = "transformation_values"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    config_version_id  = Column(Integer, ForeignKey("config_versions.id"), nullable=False, index=True)
    transformation_key = Column(String, nullable=False, index=True)
    value_kind         = Column(String, nullable=False)   # formula | scalar | table
    formula_text       = Column(String, nullable=True)
    scalar_value       = Column(Float, nullable=True)
    table_json         = Column(String, nullable=True)
