"""Mastering Candidate Contract v1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .base import ContractModel, register_contract
from .common import Cost, ExternalMapping, JsonObject, LineageReference, MbbSelection, MbbTerm, PackagingConfiguration, PipelineTrace
from .enums import ResolutionState, ReviewStatus


CONTRACT_ID = "catalogue.mastering_candidate.v1"

_CONFIRMED_STATES = {ResolutionState.CONFIRMED_MATCH, ResolutionState.CONFIRMED_CREATE}


class ResolutionBase(ContractModel):
    """Base fields shared by all mastered-resolution sections."""

    state: ResolutionState = Field(..., description="Resolution state for this section.")
    confidence: Decimal | None = Field(None, ge=Decimal("0"), le=Decimal("1"), description="Resolution confidence in [0, 1].")
    lineage: LineageReference | None = Field(None, description="Required when the section is confirmed.")
    review_decision_id: UUID | None = Field(None, description="Decision that confirmed or rejected this section.")

    @model_validator(mode="after")
    def _confirmed_requires_lineage(self):
        if self.state in _CONFIRMED_STATES and self.lineage is None:
            raise ValueError(f"{self.__class__.__name__} confirmed states require lineage")
        return self


class SupplierProductResolution(ResolutionBase):
    """Resolution of the supplier-specific commercial offering."""

    supplier_id: int | None = Field(None, gt=0, description="Rosetta supplier ID.")
    supplier_product_id: str | None = Field(None, description="Stable supplier-product identity when matched.")
    supplier_sku: str | None = Field(None, description="Supplier SKU.")
    barcode: str | None = Field(None, description="Supplier/offering barcode.")


class ProductVariantResolution(ResolutionBase):
    """Resolution of the canonical inventory identity / Product Variant."""

    product_variant_id: str | None = Field(None, description="Canonical Product Variant identity.")
    canonical_sku: str | None = Field(None, description="Rosetta canonical SKU code.")
    product_variant_name: str | None = Field(None, description="Canonical variant name.")
    product_family_id: str | None = Field(None, description="Optional Product Family enrichment.")
    proposed_name: str | None = Field(None, description="Name to use when proposing creation.")

    @model_validator(mode="after")
    def _state_has_variant_identity(self):
        if self.state in {ResolutionState.PROPOSED_MATCH, ResolutionState.CONFIRMED_MATCH}:
            if not (self.product_variant_id or self.canonical_sku):
                raise ValueError("matched Product Variant resolution requires product_variant_id or canonical_sku")
        if self.state in {ResolutionState.PROPOSED_CREATE, ResolutionState.CONFIRMED_CREATE}:
            if not (self.canonical_sku or self.proposed_name or self.product_variant_name):
                raise ValueError("created Product Variant resolution requires canonical_sku, proposed_name, or product_variant_name")
        return self


class PackagingConfigurationResolution(ResolutionBase):
    """Resolution of structured purchasing packaging."""

    packaging: PackagingConfiguration | None = Field(None, description="Resolved purchasing packaging.")


class SupplierPriceResolution(ResolutionBase):
    """Resolution of the supplier cost and price basis."""

    current_cost: Cost | None = Field(None, description="Resolved supplier cost.")
    effective_from: datetime | None = Field(None, description="Timezone-aware effective timestamp when known.")
    effective_to: datetime | None = Field(None, description="Timezone-aware end timestamp when known.")


class MbbResolution(ResolutionBase):
    """Resolution of Max Bulk Buy terms and selected term."""

    terms: list[MbbTerm] = Field(default_factory=list, description="Resolved MBB terms or tiers.")
    selected_term: MbbSelection | None = Field(None, description="Selected best/applicable term when known.")


class OptionalTextResolution(ResolutionBase):
    """Optional mastered text resolution, such as Brand or Category."""

    value_id: str | None = Field(None, description="Matched canonical identity when one exists.")
    value: str | None = Field(None, description="Resolved text value.")


class MasteringCandidateV1(ContractModel):
    """Proposal for resolving a staged item into canonical and supplier-commercial entities."""

    contract_id = CONTRACT_ID

    contract_version: Literal["catalogue.mastering_candidate.v1"] = Field(
        ...,
        description="Exact CIS-103 Mastering Candidate contract identifier.",
    )
    mastering_candidate_id: UUID = Field(..., description="Mastering Candidate identity.")
    trace: PipelineTrace = Field(..., description="Common catalogue pipeline trace metadata.")
    catalogue_item_id: UUID = Field(..., description="interpreted claim identity.")
    raw_observation_ids: list[UUID] = Field(..., min_length=1, description="Raw observations supporting this candidate.")
    lineage: LineageReference = Field(..., description="Top-level lineage back to staging and extracted evidence observations.")
    supplier_product_resolution: SupplierProductResolution = Field(..., description="Supplier Product resolution.")
    product_variant_resolution: ProductVariantResolution = Field(..., description="Product Variant resolution.")
    packaging_resolution: PackagingConfigurationResolution = Field(..., description="Packaging resolution.")
    supplier_price_resolution: SupplierPriceResolution = Field(..., description="Supplier Price resolution.")
    mbb_resolution: MbbResolution = Field(..., description="MBB resolution.")
    review_status: ReviewStatus = Field(..., description="Current review status.")
    reviewed_by: str | None = Field(None, description="Reviewer identity when reviewed.")
    reviewed_at: datetime | None = Field(None, description="Timezone-aware review timestamp.")
    override_reason: str | None = Field(None, description="Reason when approval includes an override.")
    review_decision_id: UUID | None = Field(None, description="Review decision identity.")
    product_family_resolution: OptionalTextResolution | None = Field(None, description="Optional Product Family resolution.")
    brand_resolution: OptionalTextResolution | None = Field(None, description="Optional Brand resolution.")
    category_resolution: OptionalTextResolution | None = Field(None, description="Optional Category resolution.")
    external_mappings: list[ExternalMapping] = Field(default_factory=list, description="Optional external product mappings.")
    created_at: datetime = Field(..., description="Timezone-aware creation timestamp.")
    metadata: JsonObject = Field(default_factory=dict, description="Explicit extension point for non-contract metadata.")

    @model_validator(mode="after")
    def _approved_candidate_requires_review_lineage(self):
        if len(self.raw_observation_ids) != len(set(self.raw_observation_ids)):
            raise ValueError("Mastering Candidate raw_observation_ids must be unique")
        if self.review_status in {ReviewStatus.APPROVED, ReviewStatus.APPROVED_WITH_OVERRIDE}:
            if self.lineage is None or not self.raw_observation_ids:
                raise ValueError("approved Mastering Candidate requires lineage and extracted evidence")
            if not (self.reviewed_by and self.reviewed_at):
                raise ValueError("approved Mastering Candidate requires reviewed_by and reviewed_at")
        if self.review_status == ReviewStatus.APPROVED_WITH_OVERRIDE:
            if not (self.review_decision_id or (self.reviewed_by and self.override_reason)):
                raise ValueError("approved override requires review_decision_id or reviewed_by plus override_reason")
        return self


register_contract(MasteringCandidateV1)
