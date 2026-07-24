"""Persistence mappers for catalogue pipeline contracts.

The Pydantic contracts remain the boundary source of truth. These helpers store
their durable state in SQLAlchemy rows and reconstruct the same contract payloads
without importing FastAPI routers or executing extraction runtime code.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

import models
from schemas.catalogue_pipeline import (
    MasteringCandidateV1,
    ExtractedEvidenceV1,
    ServingItemV1,
    InterpretedClaimV1,
    ValidationIssueV1,
)
from schemas.catalogue_pipeline.common import MbbTerm, PackagingConfiguration, Quantity, UnitOfMeasure
from schemas.catalogue_pipeline.enums import IssueResolutionStatus, IssueSeverity, ReviewStatus


class CataloguePersistenceError(ValueError):
    """Base error for catalogue contract persistence failures."""


class CatalogueLineageError(CataloguePersistenceError):
    """Raised when contract lineage cannot be persisted consistently."""


class CataloguePublicationError(CataloguePersistenceError):
    """Raised when an unapproved or blocked item is being published."""


def persist_extracted_evidence(db: Session, contract: ExtractedEvidenceV1) -> models.CatalogueExtractedEvidence:
    """Persist a extracted evidence observation contract as immutable source evidence."""

    existing = _extracted_evidence(db, contract.raw_observation_id)
    if existing is not None:
        return existing

    run = _ingestion_run(db, contract.ingestion_run_id)
    source_document = _source_document(db, contract.supplier_catalogue_id)
    location = contract.source_location.model_dump(mode="json")
    row = models.CatalogueExtractedEvidence(
        raw_observation_uuid=str(contract.raw_observation_id),
        contract_version=contract.contract_version,
        ingestion_run_uuid=str(contract.ingestion_run_id),
        ingestion_run_id=run.id if run else None,
        source_document_id=source_document.id if source_document else None,
        supplier_catalogue_uuid=str(contract.supplier_catalogue_id),
        source_file_uuid=str(contract.source_file_id),
        extraction_profile_id=contract.extraction_profile.profile_id,
        extraction_profile_version=contract.extraction_profile.profile_version,
        source_location_json=_json_dumps(location),
        page_number=location.get("page_number"),
        sheet_name=location.get("sheet_name"),
        row_number=location.get("row_number"),
        cell_range=location.get("cell_range"),
        source_object_key=location.get("source_object_key"),
        raw_text=contract.raw_text,
        raw_cells_json=_json_dumps([cell.model_dump(mode="json") for cell in contract.raw_cells]),
        extraction_method=contract.extraction_method.value,
        captured_at=_aware_iso(contract.captured_at),
        extraction_model=contract.extraction_model,
        extraction_model_version=contract.extraction_model_version,
        extraction_confidence=contract.extraction_confidence,
        source_metadata_json=_json_dumps(contract.source_metadata),
        created_at=_aware_iso(contract.captured_at),
    )
    db.add(row)
    db.flush()
    return row


def extracted_evidence_to_contract(row: models.CatalogueExtractedEvidence) -> ExtractedEvidenceV1:
    """Reconstruct a extracted evidence observation contract from persistence."""

    return ExtractedEvidenceV1.model_validate(
        {
            "contract_version": row.contract_version,
            "raw_observation_id": row.raw_observation_uuid,
            "ingestion_run_id": row.ingestion_run_uuid,
            "supplier_catalogue_id": row.supplier_catalogue_uuid,
            "source_file_id": row.source_file_uuid,
            "extraction_profile": {
                "profile_id": row.extraction_profile_id,
                "profile_version": row.extraction_profile_version,
            },
            "source_location": _json_loads(row.source_location_json),
            "raw_text": row.raw_text,
            "raw_cells": _json_loads(row.raw_cells_json) or [],
            "extraction_method": row.extraction_method,
            "captured_at": row.captured_at,
            "extraction_model": row.extraction_model,
            "extraction_model_version": row.extraction_model_version,
            "extraction_confidence": _decimal_json(row.extraction_confidence),
            "source_metadata": _json_loads(row.source_metadata_json) or {},
        }
    )


def persist_interpreted_claim(db: Session, contract: InterpretedClaimV1) -> models.CatalogueInterpretedClaim:
    """Persist a interpreted claim and its evidence lineage."""

    existing = _interpreted_claim(db, contract.catalogue_item_id)
    if existing is not None:
        return existing

    observations = _require_extracted_evidences(db, contract.raw_observation_ids)
    _assert_observations_match_trace(observations, str(contract.trace.ingestion_run_id), str(contract.trace.supplier_catalogue_id))

    row = models.CatalogueInterpretedClaim(
        catalogue_item_uuid=str(contract.catalogue_item_id),
        contract_version=contract.contract_version,
        ingestion_run_uuid=str(contract.trace.ingestion_run_id),
        supplier_catalogue_uuid=str(contract.trace.supplier_catalogue_id),
        source_file_uuid=str(contract.trace.source_file_id),
        extraction_profile_id=contract.trace.extraction_profile.profile_id,
        extraction_profile_version=contract.trace.extraction_profile.profile_version,
        raw_fields_json=_json_dumps(contract.raw_fields.model_dump(mode="json")),
        proposed_fields_json=_json_dumps(contract.proposed_fields.model_dump(mode="json")),
        review_requirement=contract.review_requirement.value,
        stage_status="NEEDS_REVIEW" if contract.review_requirement.value in {"REQUIRED", "BLOCKING"} else "PROPOSED",
        validation_issue_ids_json=_json_dumps([str(item) for item in contract.validation_issue_ids]),
        created_at=_aware_iso(contract.created_at),
        metadata_json=_json_dumps(contract.metadata),
    )
    db.add(row)
    db.flush()
    for index, observation in enumerate(observations):
        db.add(
            models.CatalogueInterpretedClaimEvidence(
                staging_item_id=row.id,
                raw_observation_id=observation.id,
                raw_observation_uuid=observation.raw_observation_uuid,
                sort_order=index,
            )
        )
    db.flush()
    return row


def interpreted_claim_to_contract(row: models.CatalogueInterpretedClaim) -> InterpretedClaimV1:
    """Reconstruct a interpreted claim contract from persistence."""

    raw_ids = [link.raw_observation_uuid for link in sorted(row.raw_observation_links, key=lambda item: item.sort_order)]
    return InterpretedClaimV1.model_validate(
        {
            "contract_version": row.contract_version,
            "trace": {
                "ingestion_run_id": row.ingestion_run_uuid,
                "supplier_catalogue_id": row.supplier_catalogue_uuid,
                "source_file_id": row.source_file_uuid,
                "extraction_profile": {
                    "profile_id": row.extraction_profile_id,
                    "profile_version": row.extraction_profile_version,
                },
            },
            "catalogue_item_id": row.catalogue_item_uuid,
            "raw_observation_ids": raw_ids,
            "raw_fields": _json_loads(row.raw_fields_json),
            "proposed_fields": _json_loads(row.proposed_fields_json),
            "review_requirement": row.review_requirement,
            "validation_issue_ids": _json_loads(row.validation_issue_ids_json) or [],
            "created_at": row.created_at,
            "metadata": _json_loads(row.metadata_json) or {},
        }
    )


def persist_validation_issue(db: Session, contract: ValidationIssueV1) -> models.CatalogueValidationIssue:
    """Persist a durable validation/HITL issue."""

    existing = _validation_issue(db, contract.validation_issue_id)
    if existing is not None:
        return existing

    row = models.CatalogueValidationIssue(
        validation_issue_uuid=str(contract.validation_issue_id),
        contract_version=contract.contract_version,
        ingestion_run_uuid=str(contract.ingestion_run_id),
        catalogue_item_uuid=str(contract.catalogue_item_id) if contract.catalogue_item_id else None,
        raw_observation_uuid=str(contract.raw_observation_id) if contract.raw_observation_id else None,
        stage=contract.stage.value,
        issue_code=contract.issue_code,
        severity=contract.severity.value,
        message=contract.message,
        created_at=_aware_iso(contract.created_at),
        resolution_status=contract.resolution_status.value,
        publish_blocking=1 if contract.publish_blocking else 0,
        field_path=contract.field_path,
        raw_value_json=_json_dumps(contract.raw_value),
        proposed_value_json=_json_dumps(contract.proposed_value),
        expected_value_json=_json_dumps(contract.expected_value),
        review_guidance=contract.review_guidance,
        resolver_id=contract.resolver_id,
        resolved_at=_aware_iso(contract.resolved_at) if contract.resolved_at else None,
        resolution_note=contract.resolution_note,
    )
    db.add(row)
    db.flush()
    return row


def validation_issue_to_contract(row: models.CatalogueValidationIssue) -> ValidationIssueV1:
    """Reconstruct a Validation Issue contract from persistence."""

    return ValidationIssueV1.model_validate(
        {
            "contract_version": row.contract_version,
            "validation_issue_id": row.validation_issue_uuid,
            "ingestion_run_id": row.ingestion_run_uuid,
            "catalogue_item_id": row.catalogue_item_uuid,
            "raw_observation_id": row.raw_observation_uuid,
            "stage": row.stage,
            "issue_code": row.issue_code,
            "severity": row.severity,
            "message": row.message,
            "created_at": row.created_at,
            "resolution_status": row.resolution_status,
            "field_path": row.field_path,
            "raw_value": _json_loads(row.raw_value_json),
            "proposed_value": _json_loads(row.proposed_value_json),
            "expected_value": _json_loads(row.expected_value_json),
            "review_guidance": row.review_guidance,
            "resolver_id": row.resolver_id,
            "resolved_at": row.resolved_at,
            "resolution_note": row.resolution_note,
        }
    )


def persist_mastering_candidate(db: Session, contract: MasteringCandidateV1) -> models.CatalogueMasteringCandidate:
    """Persist a Mastering Candidate after lineage and blocking checks."""

    existing = _mastering_candidate(db, contract.mastering_candidate_id)
    if existing is not None:
        return existing

    staging = _interpreted_claim(db, contract.catalogue_item_id)
    if staging is None:
        raise CatalogueLineageError(f"Interpreted claim {contract.catalogue_item_id} does not exist")
    if staging.ingestion_run_uuid != str(contract.trace.ingestion_run_id):
        raise CatalogueLineageError("Mastering Candidate cannot cross ingestion runs")

    observations = _require_extracted_evidences(db, contract.raw_observation_ids)
    _assert_observations_match_trace(observations, str(contract.trace.ingestion_run_id), str(contract.trace.supplier_catalogue_id))
    if contract.review_status in {ReviewStatus.APPROVED, ReviewStatus.APPROVED_WITH_OVERRIDE}:
        _raise_for_open_blocking_issues(db, catalogue_item_uuid=str(contract.catalogue_item_id))

    row = models.CatalogueMasteringCandidate(
        mastering_candidate_uuid=str(contract.mastering_candidate_id),
        contract_version=contract.contract_version,
        ingestion_run_uuid=str(contract.trace.ingestion_run_id),
        supplier_catalogue_uuid=str(contract.trace.supplier_catalogue_id),
        source_file_uuid=str(contract.trace.source_file_id),
        extraction_profile_id=contract.trace.extraction_profile.profile_id,
        extraction_profile_version=contract.trace.extraction_profile.profile_version,
        catalogue_item_uuid=str(contract.catalogue_item_id),
        raw_observation_ids_json=_json_dumps([str(item) for item in contract.raw_observation_ids]),
        lineage_json=_json_dumps(contract.lineage.model_dump(mode="json")),
        supplier_product_resolution_json=_json_dumps(contract.supplier_product_resolution.model_dump(mode="json")),
        product_variant_resolution_json=_json_dumps(contract.product_variant_resolution.model_dump(mode="json")),
        packaging_resolution_json=_json_dumps(contract.packaging_resolution.model_dump(mode="json")),
        supplier_price_resolution_json=_json_dumps(contract.supplier_price_resolution.model_dump(mode="json")),
        mbb_resolution_json=_json_dumps(contract.mbb_resolution.model_dump(mode="json")),
        review_status=contract.review_status.value,
        reviewed_by=contract.reviewed_by,
        reviewed_at=_aware_iso(contract.reviewed_at) if contract.reviewed_at else None,
        override_reason=contract.override_reason,
        review_decision_uuid=str(contract.review_decision_id) if contract.review_decision_id else None,
        product_family_resolution_json=_optional_model_json(contract.product_family_resolution),
        brand_resolution_json=_optional_model_json(contract.brand_resolution),
        category_resolution_json=_optional_model_json(contract.category_resolution),
        external_mappings_json=_json_dumps([item.model_dump(mode="json") for item in contract.external_mappings]),
        created_at=_aware_iso(contract.created_at),
        metadata_json=_json_dumps(contract.metadata),
    )
    db.add(row)
    db.flush()
    _persist_candidate_review_decision(db, contract)
    return row


def mastering_candidate_to_contract(row: models.CatalogueMasteringCandidate) -> MasteringCandidateV1:
    """Reconstruct a Mastering Candidate contract from persistence."""

    return MasteringCandidateV1.model_validate(
        {
            "contract_version": row.contract_version,
            "mastering_candidate_id": row.mastering_candidate_uuid,
            "trace": {
                "ingestion_run_id": row.ingestion_run_uuid,
                "supplier_catalogue_id": row.supplier_catalogue_uuid,
                "source_file_id": row.source_file_uuid,
                "extraction_profile": {
                    "profile_id": row.extraction_profile_id,
                    "profile_version": row.extraction_profile_version,
                },
            },
            "catalogue_item_id": row.catalogue_item_uuid,
            "raw_observation_ids": _json_loads(row.raw_observation_ids_json),
            "lineage": _json_loads(row.lineage_json),
            "supplier_product_resolution": _json_loads(row.supplier_product_resolution_json),
            "product_variant_resolution": _json_loads(row.product_variant_resolution_json),
            "packaging_resolution": _json_loads(row.packaging_resolution_json),
            "supplier_price_resolution": _json_loads(row.supplier_price_resolution_json),
            "mbb_resolution": _json_loads(row.mbb_resolution_json),
            "review_status": row.review_status,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at,
            "override_reason": row.override_reason,
            "review_decision_id": row.review_decision_uuid,
            "product_family_resolution": _json_loads(row.product_family_resolution_json),
            "brand_resolution": _json_loads(row.brand_resolution_json),
            "category_resolution": _json_loads(row.category_resolution_json),
            "external_mappings": _json_loads(row.external_mappings_json) or [],
            "created_at": row.created_at,
            "metadata": _json_loads(row.metadata_json) or {},
        }
    )


def persist_serving_item(db: Session, contract: ServingItemV1) -> models.CatalogueServingPublication:
    """Persist an approved Serving Item and approved commercial-history rows."""

    existing = _serving_publication(db, contract.serving_item_id)
    if existing is not None:
        return existing

    candidate = _mastering_candidate(db, contract.lineage.mastering_candidate_id)
    if candidate is None:
        raise CatalogueLineageError(f"Mastering Candidate {contract.lineage.mastering_candidate_id} does not exist")
    if candidate.review_status not in {ReviewStatus.APPROVED.value, ReviewStatus.APPROVED_WITH_OVERRIDE.value}:
        raise CataloguePublicationError("Serving publication requires an approved Mastering Candidate")
    _raise_for_open_blocking_issues(db, catalogue_item_uuid=contract.lineage.catalogue_item_id)

    supplier_product = _ensure_supplier_product(db, contract)
    _persist_packaging_configuration(db, contract, supplier_product)
    _persist_supplier_price(db, contract, supplier_product)
    _persist_mbb_terms(db, contract, supplier_product)

    publication_key = _publication_key(contract)
    now = _aware_iso(contract.published_at)
    for current in db.query(models.CatalogueServingPublication).filter_by(publication_key=publication_key, is_current=1).all():
        current.is_current = 0
        current.superseded_at = now

    row = models.CatalogueServingPublication(
        serving_item_uuid=str(contract.serving_item_id),
        contract_version=contract.contract_version,
        publication_key=publication_key,
        publication_version=contract.lineage.publication_version,
        canonical_sku=contract.canonical_sku,
        product_variant_key=contract.product_variant_id,
        product_variant_name=contract.product_variant_name,
        product_id=_product_id_for_sku(db, contract.canonical_sku),
        supplier_id=contract.supplier_offering.supplier_id,
        supplier_product_id=supplier_product.id,
        supplier_product_key=supplier_product.supplier_product_key,
        supplier_sku=contract.supplier_offering.supplier_sku,
        barcode=contract.supplier_offering.barcode,
        current_approved_cost_amount=contract.current_approved_cost.amount,
        current_approved_cost_currency=contract.current_approved_cost.currency,
        current_approved_cost_basis_uom_code=contract.current_approved_cost.price_basis.code.value,
        current_approved_cost_basis_uom_label=contract.current_approved_cost.price_basis.label,
        cost_per_sellable_unit_amount=contract.cost_per_sellable_unit.amount if contract.cost_per_sellable_unit else None,
        cost_per_sellable_unit_currency=contract.cost_per_sellable_unit.currency if contract.cost_per_sellable_unit else None,
        review_status=contract.review_status.value,
        published_at=now,
        mastering_candidate_uuid=str(contract.lineage.mastering_candidate_id),
        catalogue_item_uuid=str(contract.lineage.catalogue_item_id),
        raw_observation_ids_json=_json_dumps([str(item) for item in contract.lineage.raw_observation_ids]),
        lineage_json=_json_dumps(contract.lineage.model_dump(mode="json")),
        snapshot_json=_json_dumps(contract.model_dump(mode="json")),
        is_current=1,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def serving_item_to_contract(row: models.CatalogueServingPublication) -> ServingItemV1:
    """Reconstruct a Serving Item from its approved publication snapshot."""

    return ServingItemV1.model_validate(_json_loads(row.snapshot_json))


def _persist_candidate_review_decision(db: Session, contract: MasteringCandidateV1) -> None:
    if contract.review_status == ReviewStatus.PENDING_REVIEW:
        return
    if not (contract.reviewed_by and contract.reviewed_at):
        return
    review_decision_uuid = str(contract.review_decision_id or uuid4())
    if db.query(models.CatalogueReviewDecision).filter_by(review_decision_uuid=review_decision_uuid).first():
        return
    db.add(
        models.CatalogueReviewDecision(
            review_decision_uuid=review_decision_uuid,
            mastering_candidate_uuid=str(contract.mastering_candidate_id),
            decision_type="mastering_review",
            review_status=contract.review_status.value,
            actor_id=contract.reviewed_by,
            actor_display_name=contract.reviewed_by,
            decided_at=_aware_iso(contract.reviewed_at),
            reason=contract.override_reason,
            override_reason=contract.override_reason if contract.review_status == ReviewStatus.APPROVED_WITH_OVERRIDE else None,
            details_json=_json_dumps({"catalogue_item_id": str(contract.catalogue_item_id)}),
            created_at=_aware_iso(contract.reviewed_at),
        )
    )
    db.flush()


def _ensure_supplier_product(db: Session, contract: ServingItemV1) -> models.CatalogueSupplierProduct:
    key = contract.supplier_offering.supplier_product_id or _supplier_product_key(contract)
    existing = db.query(models.CatalogueSupplierProduct).filter_by(supplier_product_key=key).first()
    product_id = _product_id_for_sku(db, contract.canonical_sku)
    if existing:
        existing.supplier_sku = contract.supplier_offering.supplier_sku
        existing.barcode = contract.supplier_offering.barcode
        existing.product_variant_id = product_id
        return existing

    row = models.CatalogueSupplierProduct(
        supplier_product_key=key,
        supplier_id=contract.supplier_offering.supplier_id,
        product_variant_id=product_id,
        supplier_sku=contract.supplier_offering.supplier_sku,
        barcode=contract.supplier_offering.barcode,
        status="active",
        approved_review_decision_uuid=str(contract.lineage.review_decision_id) if contract.lineage.review_decision_id else None,
        created_at=_aware_iso(contract.published_at),
        updated_at=_aware_iso(contract.published_at),
    )
    db.add(row)
    db.flush()
    return row


def _persist_packaging_configuration(db: Session, contract: ServingItemV1, supplier_product: models.CatalogueSupplierProduct) -> None:
    packaging = contract.purchasing_packaging
    row = models.CataloguePackagingConfiguration(
        supplier_product_id=supplier_product.id,
        **_uom_columns("purchase", packaging.purchase_uom),
        **_uom_columns("price_basis", packaging.price_basis),
        **_uom_columns("sellable_unit", packaging.sellable_unit_uom),
        sellable_units_per_purchase_unit=packaging.sellable_units_per_purchase_unit,
        content_amount=packaging.content_amount,
        **_uom_columns("content", packaging.content_uom),
        **_quantity_columns("order_increment", packaging.order_increment),
        **_quantity_columns("minimum_order", packaging.minimum_order_quantity),
        break_pack_allowed=None if packaging.break_pack_allowed is None else int(packaging.break_pack_allowed),
        source_text=packaging.source_text,
        review_decision_uuid=str(contract.lineage.review_decision_id) if contract.lineage.review_decision_id else None,
        raw_observation_ids_json=_json_dumps([str(item) for item in contract.lineage.raw_observation_ids]),
        created_at=_aware_iso(contract.published_at),
    )
    db.add(row)
    db.flush()


def _persist_supplier_price(db: Session, contract: ServingItemV1, supplier_product: models.CatalogueSupplierProduct) -> None:
    now = _aware_iso(contract.published_at)
    for current in db.query(models.CatalogueSupplierPrice).filter_by(supplier_product_id=supplier_product.id, is_current=1).all():
        current.is_current = 0
        current.superseded_at = now
        current.effective_to = current.effective_to or now
    row = models.CatalogueSupplierPrice(
        supplier_product_id=supplier_product.id,
        amount=contract.current_approved_cost.amount,
        currency=contract.current_approved_cost.currency,
        price_basis_uom_code=contract.current_approved_cost.price_basis.code.value,
        price_basis_uom_label=contract.current_approved_cost.price_basis.label,
        effective_from=now,
        ingestion_run_uuid=None,
        mastering_candidate_uuid=str(contract.lineage.mastering_candidate_id),
        review_decision_uuid=str(contract.lineage.review_decision_id) if contract.lineage.review_decision_id else None,
        is_current=1,
        created_at=now,
    )
    db.add(row)
    db.flush()


def _persist_mbb_terms(db: Session, contract: ServingItemV1, supplier_product: models.CatalogueSupplierProduct) -> None:
    if not contract.active_mbb_terms:
        return
    now = _aware_iso(contract.published_at)
    for term in contract.active_mbb_terms:
        db.add(
            models.CatalogueSupplierMbbTerm(
                supplier_product_id=supplier_product.id,
                contract_mbb_term_uuid=str(term.mbb_term_id),
                scope=term.scope.value,
                description=term.description,
                effective_from=term.effective_from.isoformat() if term.effective_from else None,
                effective_to=term.effective_to.isoformat() if term.effective_to else None,
                mastering_candidate_uuid=str(contract.lineage.mastering_candidate_id),
                review_decision_uuid=str(contract.lineage.review_decision_id) if contract.lineage.review_decision_id else None,
                created_at=now,
                **_mbb_condition_columns(term),
                **_mbb_benefit_columns(term),
            )
        )
    db.flush()


def _mbb_condition_columns(term: MbbTerm) -> dict[str, Any]:
    condition = term.condition
    if condition.condition_type == "minimum_quantity":
        return {
            "condition_type": "minimum_quantity",
            "condition_quantity_amount": condition.quantity.amount,
            "condition_quantity_uom_code": condition.quantity.uom.code.value,
            "condition_quantity_uom_label": condition.quantity.uom.label,
        }
    return {
        "condition_type": "minimum_spend",
        "condition_spend_amount": condition.spend.amount,
        "condition_spend_currency": condition.spend.currency,
    }


def _mbb_benefit_columns(term: MbbTerm) -> dict[str, Any]:
    benefit = term.benefit
    if benefit.benefit_type == "discounted_unit_price":
        return {
            "benefit_type": "discounted_unit_price",
            "discounted_price_amount": benefit.discounted_price.amount,
            "discounted_price_currency": benefit.discounted_price.currency,
            "discounted_price_basis_uom_code": benefit.discounted_price.price_basis.code.value,
            "discounted_price_basis_uom_label": benefit.discounted_price.price_basis.label,
        }
    if benefit.benefit_type == "percentage_discount":
        return {"benefit_type": "percentage_discount", "percentage_discount": benefit.percentage}
    if benefit.benefit_type == "fixed_discount":
        return {
            "benefit_type": "fixed_discount",
            "fixed_discount_amount": benefit.amount.amount,
            "fixed_discount_currency": benefit.amount.currency,
            "fixed_discount_reduction_basis": benefit.reduction_basis.value,
        }
    return {
        "benefit_type": "free_quantity",
        "free_quantity_amount": benefit.quantity.amount,
        "free_quantity_uom_code": benefit.quantity.uom.code.value,
        "free_quantity_uom_label": benefit.quantity.uom.label,
    }


def _require_extracted_evidences(db: Session, raw_observation_ids: list[UUID]) -> list[models.CatalogueExtractedEvidence]:
    rows: list[models.CatalogueExtractedEvidence] = []
    for raw_id in raw_observation_ids:
        row = _extracted_evidence(db, raw_id)
        if row is None:
            raise CatalogueLineageError(f"extracted evidence observation {raw_id} does not exist")
        rows.append(row)
    return rows


def _assert_observations_match_trace(
    observations: list[models.CatalogueExtractedEvidence],
    ingestion_run_uuid: str,
    supplier_catalogue_uuid: str,
) -> None:
    for row in observations:
        if row.ingestion_run_uuid != ingestion_run_uuid:
            raise CatalogueLineageError("extracted evidence observation lineage cannot cross ingestion runs")
        if row.supplier_catalogue_uuid != supplier_catalogue_uuid:
            raise CatalogueLineageError("extracted evidence observation lineage cannot cross source documents")


def _raise_for_open_blocking_issues(db: Session, *, catalogue_item_uuid: UUID | str) -> None:
    issue = (
        db.query(models.CatalogueValidationIssue)
        .filter_by(
            catalogue_item_uuid=str(catalogue_item_uuid),
            severity=IssueSeverity.BLOCKING.value,
            resolution_status=IssueResolutionStatus.OPEN.value,
        )
        .first()
    )
    if issue is not None:
        raise CataloguePublicationError(f"Open blocking validation issue prevents approval/publication: {issue.issue_code}")


def _ingestion_run(db: Session, ingestion_run_id: UUID):
    return db.query(models.IngestionRun).filter_by(run_uuid=str(ingestion_run_id)).first()


def _source_document(db: Session, supplier_catalogue_id: UUID):
    return db.query(models.CatalogueSourceDocument).filter_by(supplier_catalogue_uuid=str(supplier_catalogue_id)).first()


def _extracted_evidence(db: Session, raw_observation_id: UUID):
    return db.query(models.CatalogueExtractedEvidence).filter_by(raw_observation_uuid=str(raw_observation_id)).first()


def _interpreted_claim(db: Session, catalogue_item_id: UUID):
    return db.query(models.CatalogueInterpretedClaim).filter_by(catalogue_item_uuid=str(catalogue_item_id)).first()


def _validation_issue(db: Session, validation_issue_id: UUID):
    return db.query(models.CatalogueValidationIssue).filter_by(validation_issue_uuid=str(validation_issue_id)).first()


def _mastering_candidate(db: Session, mastering_candidate_id: UUID):
    return db.query(models.CatalogueMasteringCandidate).filter_by(mastering_candidate_uuid=str(mastering_candidate_id)).first()


def _serving_publication(db: Session, serving_item_id: UUID):
    return db.query(models.CatalogueServingPublication).filter_by(serving_item_uuid=str(serving_item_id)).first()


def _product_id_for_sku(db: Session, canonical_sku: str) -> int | None:
    import models

    product = db.query(models.Product).filter_by(sku_code=canonical_sku).first()
    return product.id if product else None


def _supplier_product_key(contract: ServingItemV1) -> str:
    offering = contract.supplier_offering
    identity = offering.supplier_sku or offering.barcode or contract.canonical_sku
    return f"supplier:{offering.supplier_id}:offer:{identity}"


def _publication_key(contract: ServingItemV1) -> str:
    return f"sku:{contract.canonical_sku}:supplier:{contract.supplier_offering.supplier_id}:{_supplier_product_key(contract)}"


def _uom_columns(prefix: str, uom: UnitOfMeasure | None) -> dict[str, str | None]:
    column_prefix = f"{prefix}_uom"
    if uom is None:
        return {f"{column_prefix}_code": None, f"{column_prefix}_label": None}
    return {
        f"{column_prefix}_code": uom.code.value if uom.code else None,
        f"{column_prefix}_label": uom.label,
    }


def _quantity_columns(prefix: str, quantity: Quantity | None) -> dict[str, Any]:
    if quantity is None:
        return {
            f"{prefix}_amount": None,
            f"{prefix}_uom_code": None,
            f"{prefix}_uom_label": None,
        }
    return {
        f"{prefix}_amount": quantity.amount,
        f"{prefix}_uom_code": quantity.uom.code.value,
        f"{prefix}_uom_label": quantity.uom.label,
    }


def _optional_model_json(model) -> str | None:
    return _json_dumps(model.model_dump(mode="json")) if model is not None else None


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _decimal_json(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CataloguePersistenceError("timestamps must be timezone-aware before persistence")
    return value.isoformat()
