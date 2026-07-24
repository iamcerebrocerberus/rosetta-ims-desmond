"""Adapters from evidence and interpretation results into stage service commands."""

from __future__ import annotations

from uuid import UUID

from schemas.catalogue_pipeline.enums import ReviewRequirement
from schemas.catalogue_pipeline.raw_observation_v1 import RawObservationV1
from services import catalogue_pipeline_stages as stages
from services.catalogue_evidence_extraction import ExtractedEvidence
from services.catalogue_interpretation import InterpretedItem

from .catalogue_types import RunIdentity


def raw_input_from_extracted_evidence(evidence: ExtractedEvidence) -> stages.RawObservationInput:
    """Map one evidence observation to one Raw input without semantic mutation."""

    source_metadata = {
        **evidence.source_metadata,
        "observation_key": evidence.observation_key,
        "provider": evidence.provider,
        "provider_version": evidence.provider_version,
        "provider_request_id": evidence.provider_request_id,
        "extraction_warnings": list(evidence.warnings),
    }
    return stages.RawObservationInput(
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


def evidence_from_persisted_observation(contract: RawObservationV1) -> ExtractedEvidence:
    """Reconstruct interpretation input from a PERSISTED step-4 observation.

    The Intermediate layer consumes durable evidence, never the extraction
    task's in-memory output — the persisted record is the truth that
    interpretation must be grounded against.
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


def staging_command_from_interpretation(item: InterpretedItem) -> stages.BuildStagingItemCommand:
    """Create an interpreted-claim (step 6) command from one interpretation.

    Claims whose interpretation was degraded, returned no verdict, or had
    values dropped by the grounding check are explicitly forced to REQUIRED
    review — an empty or trimmed claim must never look reviewable-as-clean.
    """

    provenance = dict(item.provenance or {})
    needs_review = (
        provenance.get("interpreter") == "none"
        or provenance.get("no_verdict")
        or provenance.get("grounding_dropped")
    )
    return stages.BuildStagingItemCommand(
        raw_observation_ids=(item.raw_observation_id,),
        raw_fields=item.raw_fields,
        proposed_fields=item.proposed_fields,
        idempotency_key=item.observation_key,
        review_requirement=ReviewRequirement.REQUIRED if needs_review else None,
        metadata={
            "source_observation_key": item.observation_key,
            "interpretation": provenance,
        },
    )


def mastering_command_for_staging(
    *,
    run_identity: RunIdentity,
    catalogue_item_id: UUID,
    item: InterpretedItem,
) -> stages.PrepareMasteringCandidateCommand:
    """Create a pending-review mastering command from post-Raw interpretation.

    Supplier identity comes from the persisted run; sku/name/barcode come from
    the same interpreted fields that produced the staging item. No approval or
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


def _proposal_or_raw(item: InterpretedItem, field: str) -> str | None:
    proposal = item.proposed_fields.get(field)
    if isinstance(proposal, dict):
        value = proposal.get("value")
        if value is not None and str(value).strip():
            return str(value).strip()
    raw = item.raw_fields.get(field)
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return None
