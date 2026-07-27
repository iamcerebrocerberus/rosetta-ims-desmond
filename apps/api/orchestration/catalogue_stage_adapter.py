"""Adapters from evidence and interpretation results into stage service commands."""

from __future__ import annotations

from uuid import UUID

from schemas.catalogue_pipeline.enums import ReviewRequirement
from schemas.catalogue_pipeline.extracted_evidence_v1 import ExtractedEvidenceV1
from services import catalogue_pipeline_stages as stages
from services.catalogue_evidence_extraction import ExtractedEvidence
from services.catalogue_conformance import ConformedRow

from .catalogue_types import RunIdentity


def raw_input_from_extracted_evidence(evidence: ExtractedEvidence) -> stages.ExtractedEvidenceInput:
    """Map one evidence observation to one Raw input without semantic mutation."""

    source_metadata = {
        **evidence.source_metadata,
        "observation_key": evidence.observation_key,
        "provider": evidence.provider,
        "provider_version": evidence.provider_version,
        "provider_request_id": evidence.provider_request_id,
        "extraction_warnings": list(evidence.warnings),
    }
    return stages.ExtractedEvidenceInput(
        idempotency_key=evidence.observation_key,
        source_location=evidence.source_location,
        raw_text=evidence.raw_text,
        raw_cells=evidence.raw_cells,
        extraction_method=evidence.extraction_method,
        extraction_model=evidence.model or evidence.provider,
        extraction_model_version=evidence.model_version or evidence.provider_version,
        extraction_confidence=str(evidence.confidence) if evidence.confidence is not None else None,
        source_metadata=source_metadata,
    )


def evidence_from_persisted_observation(contract: ExtractedEvidenceV1) -> ExtractedEvidence:
    """Reconstruct conformance input from a PERSISTED extracted evidence observation.

    Staging conformance consumes durable evidence, never the extraction task's
    in-memory output — the persisted record is the truth that conformance must
    be grounded against.
    """

    metadata = dict(contract.source_metadata or {})
    observation_key = metadata.get("observation_key") or contract.source_location.source_object_key or str(
        contract.raw_observation_id
    )
    return ExtractedEvidence(
        observation_key=observation_key,
        source_location=contract.source_location,
        raw_text=contract.raw_text,
        raw_cells=tuple(contract.raw_cells),
        extraction_method=contract.extraction_method,
        provider=metadata.get("provider"),
        provider_version=metadata.get("provider_version"),
        provider_request_id=metadata.get("provider_request_id"),
        model=contract.extraction_model,
        model_version=contract.extraction_model_version,
        confidence=contract.extraction_confidence,
        source_metadata=metadata,
    )


def normalized_row_command_from_conformance(item: ConformedRow) -> stages.BuildNormalizedRowCommand:
    """Create an Intermediate normalized-claim command from one conformed row.

    Rows that could not be conformed deterministically (no structured cells to
    map through the contract) are forced to REQUIRED review — an empty row must
    never look reviewable-as-clean.
    """

    provenance = dict(item.provenance or {})
    needs_review = provenance.get("interpreter") == "unconformable"
    return stages.BuildNormalizedRowCommand(
        raw_observation_ids=(item.raw_observation_id,),
        raw_fields=item.raw_fields,
        normalized_fields=item.normalized_fields,
        idempotency_key=item.observation_key,
        review_requirement=ReviewRequirement.REQUIRED if needs_review else None,
        metadata={
            "source_observation_key": item.observation_key,
            "conformance": provenance,
        },
    )


def mastering_command_for_claim(
    *,
    run_identity: RunIdentity,
    catalogue_item_id: UUID,
    item: ConformedRow,
) -> stages.PrepareMasteringCandidateCommand:
    """Create a pending-review mastering command from a conformed normalized row.

    Supplier identity comes from the persisted run; sku/name/barcode come from
    the same normalized fields that produced the normalized row. No approval or
    application semantics are implied here.
    """

    supplier_sku = _proposal_or_raw(item, "supplier_sku")
    product_name = _proposal_or_raw(item, "product_name")
    barcode = _proposal_or_raw(item, "barcode")
    supplier_resolution = {
        "state": "PROPOSED_CREATE" if supplier_sku else "UNRESOLVED",
        "supplier_id": run_identity.supplier_id,
        "supplier_product_id": (
            f"supplier:{run_identity.supplier_id}:offer:{supplier_sku}" if supplier_sku else None
        ),
        "supplier_sku": supplier_sku,
        "barcode": barcode,
    }
    product_resolution = {
        "state": "PROPOSED_CREATE" if product_name else "UNRESOLVED",
        "canonical_sku": supplier_sku,
        "product_variant_id": supplier_sku,
        "product_variant_name": product_name,
        "proposed_name": product_name,
        "product_family_id": None,
    }
    return stages.PrepareMasteringCandidateCommand(
        catalogue_item_id=catalogue_item_id,
        idempotency_key=item.observation_key,
        supplier_product_resolution=supplier_resolution,
        product_variant_resolution=product_resolution,
        metadata={"source_observation_key": item.observation_key, "human_review_required": True},
    )


def _proposal_or_raw(item: ConformedRow, field: str) -> str | None:
    proposal = item.normalized_fields.get(field)
    if isinstance(proposal, dict):
        value = proposal.get("value")
        if value is not None and str(value).strip():
            return str(value).strip()
    raw = item.raw_fields.get(field)
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return None
