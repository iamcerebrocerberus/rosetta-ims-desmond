"""Catalogue logical persistence models.

These tables make CIS-103 catalogue pipeline contracts durable without changing
the current v1 upload/review runtime. Existing `models.py` tables remain the
compatibility surface; this module adds the normalized evidence, staging,
validation, mastering, review, commercial-history, and publication foundation.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base


def _uuid() -> str:
    return str(uuid4())


class CatalogueSourceDocument(Base):
    """Durable source asset for one supplier catalogue file."""

    __tablename__ = "catalogue_source_documents"
    __table_args__ = (
        UniqueConstraint("supplier_catalogue_uuid", name="uq_catalogue_source_documents_catalogue_uuid"),
        UniqueConstraint("source_file_uuid", name="uq_catalogue_source_documents_source_file_uuid"),
        UniqueConstraint("legacy_import_id", name="uq_catalogue_source_documents_legacy_import"),
        CheckConstraint("supplier_catalogue_uuid != source_file_uuid", name="ck_source_document_distinct_uuids"),
        CheckConstraint("filename IS NOT NULL AND length(trim(filename)) > 0", name="ck_source_document_filename"),
        Index("ix_source_documents_supplier", "supplier_id"),
        Index("ix_source_documents_supplier_contract", "supplier_id", "supplier_source_contract_id", "supplier_source_contract_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_catalogue_uuid = Column(String(36), nullable=False, default=_uuid)
    source_file_uuid = Column(String(36), nullable=False, default=_uuid)
    legacy_import_id = Column(Integer, ForeignKey("catalogue_imports.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    filename = Column(String, nullable=False)
    source_format = Column(String, nullable=True)
    source_ref = Column(String, nullable=True)
    source_checksum = Column(String, nullable=True)
    received_at = Column(String, nullable=False)
    supplier_source_contract_id = Column(String, nullable=True)
    supplier_source_contract_version = Column(String, nullable=True)
    document_type = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")
    source_metadata_json = Column(Text, nullable=True)
    # Raw-stage completion metadata — file-level facts only, written by the
    # raw stage after verifying the stored original against its checksum.
    byte_size = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)          # lightweight structural count; PDFs only
    raw_stage_status = Column(String, nullable=True)     # 'completed' | 'failed'
    raw_stage_completed_at = Column(String, nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=True)

    legacy_import = relationship("CatalogueImport", foreign_keys=[legacy_import_id], viewonly=True)
    supplier = relationship("Supplier", foreign_keys=[supplier_id], viewonly=True)
    ingestion_runs = relationship("IngestionRun", back_populates="pipeline_source_document")


class CatalogueRawStageAttempt(Base):
    """Append-only raw-stage attempt history — file-level facts only.

    One row per raw-stage execution (verification of the stored original).
    Rows are never updated or deleted during normal service operation:
    re-running raw appends another attempt, so a later integrity failure can
    never erase the record of an earlier successful verification. The mutable
    current-state fields on CatalogueSourceDocument mirror only the most
    recent attempt. Never stores file content, extracted text, product rows,
    prompts, model output or confidence values.
    """

    __tablename__ = "catalogue_raw_stage_attempts"
    __table_args__ = (
        UniqueConstraint("attempt_uuid", name="uq_raw_stage_attempts_attempt_uuid"),
        Index("ix_raw_stage_attempts_run", "ingestion_run_uuid"),
        Index("ix_raw_stage_attempts_source_document", "catalogue_source_document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_uuid = Column(String(36), nullable=False, default=_uuid)
    ingestion_run_uuid = Column(String(36), nullable=False)
    catalogue_source_document_id = Column(Integer, ForeignKey("catalogue_source_documents.id"), nullable=True)
    status = Column(String, nullable=False)              # 'completed' | 'failed'
    attempted_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=True)
    checksum_sha256 = Column(String, nullable=True)      # observed during this attempt
    byte_size = Column(Integer, nullable=True)
    source_format = Column(String, nullable=True)
    page_count = Column(Integer, nullable=True)
    failure_code = Column(String, nullable=True)         # sanitized machine code
    failure_message = Column(String, nullable=True)      # sanitized public message
    created_at = Column(String, nullable=False)


class CatalogueExtractionAttempt(Base):
    """Append-only Staging extraction attempt with per-unit completeness."""

    __tablename__ = "catalogue_extraction_attempts"
    __table_args__ = (
        UniqueConstraint("attempt_uuid", name="uq_extraction_attempts_uuid"),
        Index("ix_extraction_attempts_run", "ingestion_run_uuid"),
        Index("ix_extraction_attempts_source", "catalogue_source_document_id"),
        CheckConstraint(
            "status IN ('COMPLETE','PARTIAL','FAILED')",
            name="ck_extraction_attempt_status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_uuid = Column(String(36), nullable=False, default=_uuid)
    ingestion_run_uuid = Column(
        String(36),
        ForeignKey("catalogue_ingestion_runs.run_uuid"),
        nullable=False,
    )
    catalogue_source_document_id = Column(
        Integer,
        ForeignKey("catalogue_source_documents.id"),
        nullable=True,
    )
    status = Column(String, nullable=False)
    source_format = Column(String, nullable=False)
    provider = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    started_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=False)
    units_attempted = Column(Integer, nullable=False)
    units_completed = Column(Integer, nullable=False)
    empty_units = Column(Integer, nullable=False, default=0)
    observation_count = Column(Integer, nullable=False, default=0)
    unit_outcomes_json = Column(Text, nullable=False)
    warnings_json = Column(Text, nullable=False)
    errors_json = Column(Text, nullable=False)
    created_at = Column(String, nullable=False)


class CatalogueExtractedEvidence(Base):
    """Extracted evidence observation: verbatim evidence + exact source location.

    Terminology: despite the historical name, this is the EXTRACTION stage's
    output (verbatim text/cells with provider metadata and confidence), not
    the file-only raw stage. The raw stage's records are CatalogueSourceDocument
    and CatalogueRawStageAttempt. Prefer "extracted evidence observation" in
    new code and docs; renaming this table is tracked as follow-up debt
    (docs/technical-debt/rename-raw-observation-to-extracted-evidence.md).
    """

    __tablename__ = "catalogue_extracted_evidence"
    __table_args__ = (
        UniqueConstraint("raw_observation_uuid", name="uq_extracted_evidences_uuid"),
        CheckConstraint("page_number IS NULL OR page_number > 0", name="ck_extracted_evidence_page_positive"),
        CheckConstraint("row_number IS NULL OR row_number > 0", name="ck_extracted_evidence_row_positive"),
        CheckConstraint("extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)", name="ck_extracted_evidence_confidence"),
        CheckConstraint(
            "(raw_text IS NOT NULL AND length(trim(raw_text)) > 0) OR "
            "(raw_cells_json IS NOT NULL AND raw_cells_json != '[]')",
            name="ck_extracted_evidence_has_evidence",
        ),
        CheckConstraint(
            "page_number IS NOT NULL OR sheet_name IS NOT NULL OR row_number IS NOT NULL OR "
            "cell_range IS NOT NULL OR source_object_key IS NOT NULL OR "
            "(source_location_json IS NOT NULL AND source_location_json LIKE '%bounding_box%')",
            name="ck_extracted_evidence_has_location",
        ),
        Index("ix_extracted_evidences_run", "ingestion_run_uuid"),
        Index("ix_extracted_evidences_source_document", "supplier_catalogue_uuid"),
        Index("ix_extracted_evidences_location", "page_number", "sheet_name", "row_number"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_observation_uuid = Column(String(36), nullable=False, default=_uuid)
    contract_version = Column(String, nullable=False)
    ingestion_run_uuid = Column(String(36), ForeignKey("catalogue_ingestion_runs.run_uuid"), nullable=False)
    ingestion_run_id = Column(Integer, ForeignKey("catalogue_ingestion_runs.id"), nullable=True)
    source_document_id = Column(Integer, ForeignKey("catalogue_source_documents.id"), nullable=True)
    supplier_catalogue_uuid = Column(String(36), nullable=False)
    source_file_uuid = Column(String(36), nullable=False)
    extraction_profile_id = Column(String, nullable=False)
    extraction_profile_version = Column(String, nullable=False)
    source_location_json = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    sheet_name = Column(String, nullable=True)
    row_number = Column(Integer, nullable=True)
    cell_range = Column(String, nullable=True)
    source_object_key = Column(String, nullable=True)
    raw_text = Column(Text, nullable=True)
    raw_cells_json = Column(Text, nullable=True)
    extraction_method = Column(String, nullable=False)
    captured_at = Column(String, nullable=False)
    extraction_model = Column(String, nullable=True)
    extraction_model_version = Column(String, nullable=True)
    extraction_confidence = Column(Numeric(5, 4), nullable=True)
    source_metadata_json = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)

    ingestion_run = relationship("IngestionRun", foreign_keys=[ingestion_run_id], viewonly=True)
    source_document = relationship("CatalogueSourceDocument", foreign_keys=[source_document_id], viewonly=True)


class CatalogueNormalizedRow(Base):
    """Interpreted claim (timeline step 6, INTERMEDIATE layer).

    Historical name: these rows are NOT the Staging layer (extracted
    evidence); they hold what the supplier contract + interpreter proposed
    the evidence MEANS. Rename tracked in docs/technical-debt/
    rename-staging-item-to-interpreted-claim.md. Original docstring: Raw source-field snapshot plus typed proposed interpretation."""

    __tablename__ = "catalogue_normalized_rows"
    __table_args__ = (
        UniqueConstraint("catalogue_item_uuid", name="uq_normalized_rows_uuid"),
        CheckConstraint("review_requirement IN ('NOT_REQUIRED','RECOMMENDED','REQUIRED','BLOCKING')", name="ck_staging_review_requirement"),
        CheckConstraint("stage_status IN ('PROPOSED','NEEDS_REVIEW','APPROVED','REJECTED','SUPERSEDED')", name="ck_staging_status"),
        Index("ix_normalized_rows_run_status", "ingestion_run_uuid", "stage_status", "review_requirement"),
        Index("ix_normalized_rows_catalogue", "supplier_catalogue_uuid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    catalogue_item_uuid = Column(String(36), nullable=False, default=_uuid)
    contract_version = Column(String, nullable=False)
    ingestion_run_uuid = Column(String(36), ForeignKey("catalogue_ingestion_runs.run_uuid"), nullable=False)
    supplier_catalogue_uuid = Column(String(36), nullable=False)
    source_file_uuid = Column(String(36), nullable=False)
    extraction_profile_id = Column(String, nullable=False)
    extraction_profile_version = Column(String, nullable=False)
    raw_fields_json = Column(Text, nullable=False)
    normalized_fields_json = Column(Text, nullable=False)
    review_requirement = Column(String, nullable=False)
    stage_status = Column(String, nullable=False, default="PROPOSED")
    validation_issue_ids_json = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)
    metadata_json = Column(Text, nullable=True)

    raw_observation_links = relationship(
        "CatalogueNormalizedRowEvidence",
        back_populates="staging_item",
        cascade="all, delete-orphan",
        order_by="CatalogueNormalizedRowEvidence.sort_order",
    )


class CatalogueNormalizedRowEvidence(Base):
    """Many-to-many lineage from interpreted claims to extracted evidence observations."""

    __tablename__ = "catalogue_normalized_row_evidence"
    __table_args__ = (
        UniqueConstraint("staging_item_id", "raw_observation_id", name="uq_staging_extracted_evidence_link"),
        Index("ix_staging_extracted_evidence_raw_uuid", "raw_observation_uuid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    staging_item_id = Column(Integer, ForeignKey("catalogue_normalized_rows.id"), nullable=False)
    raw_observation_id = Column(Integer, ForeignKey("catalogue_extracted_evidence.id"), nullable=False)
    raw_observation_uuid = Column(String(36), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)

    staging_item = relationship("CatalogueNormalizedRow", back_populates="raw_observation_links")
    raw_observation = relationship("CatalogueExtractedEvidence", foreign_keys=[raw_observation_id], viewonly=True)


class CatalogueValidationIssue(Base):
    """Durable business-readable validation or HITL issue."""

    __tablename__ = "catalogue_validation_issues"
    __table_args__ = (
        UniqueConstraint("validation_issue_uuid", name="uq_validation_issues_uuid"),
        CheckConstraint("severity IN ('INFO','WARNING','ERROR','BLOCKING')", name="ck_validation_issue_severity"),
        CheckConstraint("resolution_status IN ('OPEN','CONFIRMED','CORRECTED','ACCEPTED_AS_IS','DISMISSED')", name="ck_validation_issue_status"),
        CheckConstraint("resolution_status = 'OPEN' OR resolved_at IS NOT NULL", name="ck_validation_issue_resolved_at"),
        CheckConstraint("resolution_status != 'OPEN' OR resolved_at IS NULL", name="ck_validation_issue_open_no_resolved_at"),
        Index("ix_validation_issues_run", "ingestion_run_uuid"),
        Index("ix_validation_issues_item", "catalogue_item_uuid"),
        Index("ix_validation_issues_raw", "raw_observation_uuid"),
        Index("ix_validation_issues_blocking", "stage", "severity", "resolution_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    validation_issue_uuid = Column(String(36), nullable=False, default=_uuid)
    contract_version = Column(String, nullable=False)
    ingestion_run_uuid = Column(String(36), ForeignKey("catalogue_ingestion_runs.run_uuid"), nullable=False)
    catalogue_item_uuid = Column(String(36), nullable=True)
    raw_observation_uuid = Column(String(36), nullable=True)
    stage = Column(String, nullable=False)
    issue_code = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(String, nullable=False)
    resolution_status = Column(String, nullable=False)
    publish_blocking = Column(Integer, nullable=False, default=0)
    field_path = Column(String, nullable=True)
    raw_value_json = Column(Text, nullable=True)
    proposed_value_json = Column(Text, nullable=True)
    expected_value_json = Column(Text, nullable=True)
    review_guidance = Column(Text, nullable=True)
    resolver_id = Column(String, nullable=True)
    resolved_at = Column(String, nullable=True)
    resolution_note = Column(Text, nullable=True)


class CatalogueMasteringCandidate(Base):
    """Reviewable proposal for canonical and supplier-commercial resolution."""

    __tablename__ = "catalogue_mastering_candidates"
    __table_args__ = (
        UniqueConstraint("mastering_candidate_uuid", name="uq_mastering_candidates_uuid"),
        CheckConstraint(
            "review_status IN ('PENDING_REVIEW','APPROVED','APPROVED_WITH_OVERRIDE','REJECTED','NEEDS_CLARIFICATION')",
            name="ck_mastering_candidate_review_status",
        ),
        CheckConstraint(
            "review_status NOT IN ('APPROVED','APPROVED_WITH_OVERRIDE') OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_mastering_candidate_approval_reviewer",
        ),
        CheckConstraint(
            "review_status != 'APPROVED_WITH_OVERRIDE' OR (override_reason IS NOT NULL OR review_decision_uuid IS NOT NULL)",
            name="ck_mastering_candidate_override_reason",
        ),
        Index("ix_mastering_candidates_run", "ingestion_run_uuid"),
        Index("ix_mastering_candidates_item", "catalogue_item_uuid"),
        Index("ix_mastering_candidates_review", "review_status", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mastering_candidate_uuid = Column(String(36), nullable=False, default=_uuid)
    contract_version = Column(String, nullable=False)
    ingestion_run_uuid = Column(String(36), ForeignKey("catalogue_ingestion_runs.run_uuid"), nullable=False)
    supplier_catalogue_uuid = Column(String(36), nullable=False)
    source_file_uuid = Column(String(36), nullable=False)
    extraction_profile_id = Column(String, nullable=False)
    extraction_profile_version = Column(String, nullable=False)
    catalogue_item_uuid = Column(String(36), nullable=False)
    raw_observation_ids_json = Column(Text, nullable=False)
    lineage_json = Column(Text, nullable=False)
    supplier_product_resolution_json = Column(Text, nullable=False)
    product_variant_resolution_json = Column(Text, nullable=False)
    packaging_resolution_json = Column(Text, nullable=False)
    supplier_price_resolution_json = Column(Text, nullable=False)
    mbb_resolution_json = Column(Text, nullable=False)
    review_status = Column(String, nullable=False)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(String, nullable=True)
    override_reason = Column(Text, nullable=True)
    review_decision_uuid = Column(String(36), nullable=True)
    product_family_resolution_json = Column(Text, nullable=True)
    brand_resolution_json = Column(Text, nullable=True)
    category_resolution_json = Column(Text, nullable=True)
    external_mappings_json = Column(Text, nullable=True)
    # Set when a human correction produced a revised candidate for the same
    # normalized row; a superseded candidate accepts no further decisions.
    superseded_by_uuid = Column(String(36), nullable=True)
    created_at = Column(String, nullable=False)
    metadata_json = Column(Text, nullable=True)

    review_decisions = relationship("CatalogueReviewDecision", back_populates="mastering_candidate")


class CatalogueReviewDecision(Base):
    """Typed append-only decision for a candidate or validation issue."""

    __tablename__ = "catalogue_review_decisions"
    __table_args__ = (
        UniqueConstraint("review_decision_uuid", name="uq_review_decisions_uuid"),
        CheckConstraint(
            "mastering_candidate_uuid IS NOT NULL OR validation_issue_uuid IS NOT NULL",
            name="ck_review_decision_has_target",
        ),
        CheckConstraint(
            "review_status IS NULL OR review_status IN ('PENDING_REVIEW','APPROVED','APPROVED_WITH_OVERRIDE','REJECTED','NEEDS_CLARIFICATION')",
            name="ck_review_decision_status",
        ),
        CheckConstraint(
            "review_status != 'APPROVED_WITH_OVERRIDE' OR override_reason IS NOT NULL",
            name="ck_review_decision_override_reason",
        ),
        Index("ix_review_decisions_candidate", "mastering_candidate_uuid"),
        Index("ix_review_decisions_issue", "validation_issue_uuid"),
        Index("ix_review_decisions_actor_time", "actor_id", "decided_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_decision_uuid = Column(String(36), nullable=False, default=_uuid)
    mastering_candidate_uuid = Column(String(36), ForeignKey("catalogue_mastering_candidates.mastering_candidate_uuid"), nullable=True)
    validation_issue_uuid = Column(String(36), ForeignKey("catalogue_validation_issues.validation_issue_uuid"), nullable=True)
    decision_type = Column(String, nullable=False)
    review_status = Column(String, nullable=True)
    actor_id = Column(String, nullable=False)
    actor_display_name = Column(String, nullable=True)
    decided_at = Column(String, nullable=False)
    reason = Column(Text, nullable=True)
    override_reason = Column(Text, nullable=True)
    details_json = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)

    mastering_candidate = relationship("CatalogueMasteringCandidate", back_populates="review_decisions")


class CatalogueProductFamily(Base):
    """Optional product grouping/enrichment above canonical SKU variants."""

    __tablename__ = "catalogue_product_families"
    __table_args__ = (
        UniqueConstraint("family_key", name="uq_product_families_key"),
        Index("ix_product_families_brand_category", "brand", "category"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    family_key = Column(String, nullable=False)
    name = Column(String, nullable=False)
    brand = Column(String, nullable=True)
    category = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=True)


class CatalogueSupplierProduct(Base):
    """Normalized supplier-specific offering of a Product Variant."""

    __tablename__ = "catalogue_supplier_products"
    __table_args__ = (
        UniqueConstraint("supplier_product_key", name="uq_catalogue_supplier_products_key"),
        UniqueConstraint("legacy_product_supplier_id", name="uq_catalogue_supplier_products_legacy"),
        UniqueConstraint("supplier_id", "supplier_sku", name="uq_catalogue_supplier_products_supplier_sku"),
        Index("ix_catalogue_supplier_products_supplier", "supplier_id"),
        Index("ix_catalogue_supplier_products_barcode", "barcode"),
        Index("ix_catalogue_supplier_products_variant", "product_variant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_product_uuid = Column(String(36), nullable=False, default=_uuid)
    supplier_product_key = Column(String, nullable=False)
    legacy_product_supplier_id = Column(Integer, ForeignKey("product_suppliers.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    product_variant_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    product_family_id = Column(Integer, ForeignKey("catalogue_product_families.id"), nullable=True)
    supplier_sku = Column(String, nullable=True)
    barcode = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")
    approved_review_decision_uuid = Column(String(36), nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=True)

    prices = relationship("CatalogueSupplierPrice", back_populates="supplier_product")
    packaging_configurations = relationship("CataloguePackagingConfiguration", back_populates="supplier_product")
    mbb_terms = relationship("CatalogueSupplierMbbTerm", back_populates="supplier_product")


class CataloguePackagingConfiguration(Base):
    """Structured purchase, price basis, sellable unit, content and ordering semantics."""

    __tablename__ = "catalogue_packaging_configurations"
    __table_args__ = (
        UniqueConstraint("packaging_uuid", name="uq_packaging_configurations_uuid"),
        CheckConstraint("sellable_units_per_purchase_unit IS NULL OR sellable_units_per_purchase_unit > 0", name="ck_packaging_sellable_units_positive"),
        CheckConstraint("content_amount IS NULL OR content_amount > 0", name="ck_packaging_content_positive"),
        CheckConstraint(
            "(content_amount IS NULL AND content_uom_code IS NULL) OR (content_amount IS NOT NULL AND content_uom_code IS NOT NULL)",
            name="ck_packaging_content_pair",
        ),
        CheckConstraint("order_increment_amount IS NULL OR order_increment_amount > 0", name="ck_packaging_order_increment_positive"),
        CheckConstraint("minimum_order_amount IS NULL OR minimum_order_amount > 0", name="ck_packaging_minimum_order_positive"),
        CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from", name="ck_packaging_effective_range"),
        Index("ix_packaging_supplier_product", "supplier_product_id", "effective_from"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    packaging_uuid = Column(String(36), nullable=False, default=_uuid)
    supplier_product_id = Column(Integer, ForeignKey("catalogue_supplier_products.id"), nullable=False)
    purchase_uom_code = Column(String, nullable=True)
    purchase_uom_label = Column(String, nullable=True)
    price_basis_uom_code = Column(String, nullable=True)
    price_basis_uom_label = Column(String, nullable=True)
    sellable_unit_uom_code = Column(String, nullable=True)
    sellable_unit_uom_label = Column(String, nullable=True)
    sellable_units_per_purchase_unit = Column(Numeric(18, 6), nullable=True)
    content_amount = Column(Numeric(18, 6), nullable=True)
    content_uom_code = Column(String, nullable=True)
    content_uom_label = Column(String, nullable=True)
    order_increment_amount = Column(Numeric(18, 6), nullable=True)
    order_increment_uom_code = Column(String, nullable=True)
    order_increment_uom_label = Column(String, nullable=True)
    minimum_order_amount = Column(Numeric(18, 6), nullable=True)
    minimum_order_uom_code = Column(String, nullable=True)
    minimum_order_uom_label = Column(String, nullable=True)
    break_pack_allowed = Column(Integer, nullable=True)
    source_text = Column(Text, nullable=True)
    effective_from = Column(String, nullable=True)
    effective_to = Column(String, nullable=True)
    review_decision_uuid = Column(String(36), nullable=True)
    raw_observation_ids_json = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)
    superseded_at = Column(String, nullable=True)

    supplier_product = relationship("CatalogueSupplierProduct", back_populates="packaging_configurations")


class CatalogueSupplierPrice(Base):
    """Effective-dated supplier cost history."""

    __tablename__ = "catalogue_supplier_prices"
    __table_args__ = (
        UniqueConstraint("supplier_price_uuid", name="uq_supplier_prices_uuid"),
        CheckConstraint("amount >= 0", name="ck_supplier_price_amount_non_negative"),
        CheckConstraint("currency = 'HKD'", name="ck_supplier_price_currency_hkd"),
        CheckConstraint("price_basis_uom_code IS NOT NULL", name="ck_supplier_price_basis_required"),
        CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from", name="ck_supplier_price_effective_range"),
        CheckConstraint("is_current IN (0, 1)", name="ck_supplier_price_is_current"),
        Index("ix_supplier_prices_supplier_product", "supplier_product_id", "effective_from", "effective_to"),
        Index("ix_supplier_prices_current", "supplier_product_id", "is_current"),
        Index("ix_supplier_prices_lineage", "ingestion_run_uuid", "mastering_candidate_uuid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_price_uuid = Column(String(36), nullable=False, default=_uuid)
    supplier_product_id = Column(Integer, ForeignKey("catalogue_supplier_products.id"), nullable=False)
    amount = Column(Numeric(14, 4), nullable=False)
    currency = Column(String(3), nullable=False, default="HKD")
    price_basis_uom_code = Column(String, nullable=False)
    price_basis_uom_label = Column(String, nullable=True)
    effective_from = Column(String, nullable=True)
    effective_to = Column(String, nullable=True)
    source_document_id = Column(Integer, ForeignKey("catalogue_source_documents.id"), nullable=True)
    ingestion_run_uuid = Column(String(36), nullable=True)
    mastering_candidate_uuid = Column(String(36), nullable=True)
    review_decision_uuid = Column(String(36), nullable=True)
    is_current = Column(Integer, nullable=False, default=1)
    created_at = Column(String, nullable=False)
    superseded_at = Column(String, nullable=True)

    supplier_product = relationship("CatalogueSupplierProduct", back_populates="prices")


class CatalogueSupplierMbbTerm(Base):
    """Typed Max Bulk Buy term using condition plus benefit."""

    __tablename__ = "catalogue_supplier_mbb_terms"
    __table_args__ = (
        UniqueConstraint("supplier_mbb_term_uuid", name="uq_supplier_mbb_terms_uuid"),
        CheckConstraint("condition_type IN ('minimum_quantity','minimum_spend')", name="ck_mbb_condition_type"),
        CheckConstraint(
            "benefit_type IN ('discounted_unit_price','percentage_discount','fixed_discount','free_quantity')",
            name="ck_mbb_benefit_type",
        ),
        CheckConstraint("condition_quantity_amount IS NULL OR condition_quantity_amount > 0", name="ck_mbb_condition_qty_positive"),
        CheckConstraint("condition_spend_amount IS NULL OR condition_spend_amount > 0", name="ck_mbb_condition_spend_positive"),
        CheckConstraint("percentage_discount IS NULL OR (percentage_discount > 0 AND percentage_discount <= 100)", name="ck_mbb_percentage_range"),
        CheckConstraint("free_quantity_amount IS NULL OR free_quantity_amount > 0", name="ck_mbb_free_qty_positive"),
        CheckConstraint("fixed_discount_amount IS NULL OR fixed_discount_amount > 0", name="ck_mbb_fixed_discount_positive"),
        CheckConstraint("discounted_price_amount IS NULL OR discounted_price_amount >= 0", name="ck_mbb_discounted_price_non_negative"),
        CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from", name="ck_mbb_effective_range"),
        CheckConstraint("is_active IN (0, 1)", name="ck_mbb_is_active"),
        Index("ix_supplier_mbb_terms_supplier_product", "supplier_product_id", "is_active"),
        Index("ix_supplier_mbb_terms_lineage", "ingestion_run_uuid", "mastering_candidate_uuid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_mbb_term_uuid = Column(String(36), nullable=False, default=_uuid)
    supplier_product_id = Column(Integer, ForeignKey("catalogue_supplier_products.id"), nullable=False)
    contract_mbb_term_uuid = Column(String(36), nullable=True)
    scope = Column(String, nullable=False)
    condition_type = Column(String, nullable=False)
    condition_quantity_amount = Column(Numeric(18, 6), nullable=True)
    condition_quantity_uom_code = Column(String, nullable=True)
    condition_quantity_uom_label = Column(String, nullable=True)
    condition_spend_amount = Column(Numeric(14, 4), nullable=True)
    condition_spend_currency = Column(String(3), nullable=True)
    benefit_type = Column(String, nullable=False)
    discounted_price_amount = Column(Numeric(14, 4), nullable=True)
    discounted_price_currency = Column(String(3), nullable=True)
    discounted_price_basis_uom_code = Column(String, nullable=True)
    discounted_price_basis_uom_label = Column(String, nullable=True)
    percentage_discount = Column(Numeric(7, 4), nullable=True)
    fixed_discount_amount = Column(Numeric(14, 4), nullable=True)
    fixed_discount_currency = Column(String(3), nullable=True)
    fixed_discount_reduction_basis = Column(String, nullable=True)
    free_quantity_amount = Column(Numeric(18, 6), nullable=True)
    free_quantity_uom_code = Column(String, nullable=True)
    free_quantity_uom_label = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    effective_from = Column(String, nullable=True)
    effective_to = Column(String, nullable=True)
    source_document_id = Column(Integer, ForeignKey("catalogue_source_documents.id"), nullable=True)
    ingestion_run_uuid = Column(String(36), nullable=True)
    mastering_candidate_uuid = Column(String(36), nullable=True)
    review_decision_uuid = Column(String(36), nullable=True)
    is_active = Column(Integer, nullable=False, default=1)
    created_at = Column(String, nullable=False)
    superseded_at = Column(String, nullable=True)

    supplier_product = relationship("CatalogueSupplierProduct", back_populates="mbb_terms")


class CatalogueServingPublication(Base):
    """Approved serving snapshot safe for consumer-facing reads."""

    __tablename__ = "catalogue_serving_publications"
    __table_args__ = (
        UniqueConstraint("serving_item_uuid", name="uq_serving_publications_uuid"),
        CheckConstraint("review_status IN ('APPROVED','APPROVED_WITH_OVERRIDE')", name="ck_serving_publication_approved"),
        CheckConstraint("current_approved_cost_amount >= 0", name="ck_serving_current_cost_non_negative"),
        CheckConstraint("current_approved_cost_currency = 'HKD'", name="ck_serving_current_cost_hkd"),
        CheckConstraint("is_current IN (0, 1)", name="ck_serving_is_current"),
        Index("ix_serving_publications_current_sku", "canonical_sku", "is_current"),
        Index("ix_serving_publications_supplier_product", "supplier_product_id", "is_current"),
        Index("ix_serving_publications_lineage", "mastering_candidate_uuid", "catalogue_item_uuid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    serving_item_uuid = Column(String(36), nullable=False, default=_uuid)
    contract_version = Column(String, nullable=False)
    publication_key = Column(String, nullable=False)
    publication_version = Column(String, nullable=False)
    canonical_sku = Column(String, nullable=False)
    product_variant_key = Column(String, nullable=False)
    product_variant_name = Column(String, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    supplier_product_id = Column(Integer, ForeignKey("catalogue_supplier_products.id"), nullable=True)
    supplier_product_key = Column(String, nullable=True)
    supplier_sku = Column(String, nullable=True)
    barcode = Column(String, nullable=True)
    current_approved_cost_amount = Column(Numeric(14, 4), nullable=False)
    current_approved_cost_currency = Column(String(3), nullable=False, default="HKD")
    current_approved_cost_basis_uom_code = Column(String, nullable=False)
    current_approved_cost_basis_uom_label = Column(String, nullable=True)
    cost_per_sellable_unit_amount = Column(Numeric(14, 4), nullable=True)
    cost_per_sellable_unit_currency = Column(String(3), nullable=True)
    review_status = Column(String, nullable=False)
    published_at = Column(String, nullable=False)
    mastering_candidate_uuid = Column(String(36), ForeignKey("catalogue_mastering_candidates.mastering_candidate_uuid"), nullable=False)
    catalogue_item_uuid = Column(String(36), nullable=False)
    raw_observation_ids_json = Column(Text, nullable=False)
    lineage_json = Column(Text, nullable=False)
    snapshot_json = Column(Text, nullable=False)
    is_current = Column(Integer, nullable=False, default=1)
    superseded_at = Column(String, nullable=True)
    created_at = Column(String, nullable=False)


__all__ = [
    "CatalogueSourceDocument",
    "CatalogueExtractionAttempt",
    "CatalogueExtractedEvidence",
    "CatalogueNormalizedRow",
    "CatalogueNormalizedRowEvidence",
    "CatalogueValidationIssue",
    "CatalogueMasteringCandidate",
    "CatalogueReviewDecision",
    "CatalogueProductFamily",
    "CatalogueSupplierProduct",
    "CataloguePackagingConfiguration",
    "CatalogueSupplierPrice",
    "CatalogueSupplierMbbTerm",
    "CatalogueServingPublication",
]
