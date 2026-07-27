"""interpreted claim Contract v1."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .base import ContractModel, register_contract
from .common import (
    CostProposal,
    DateProposal,
    JsonObject,
    MbbTerm,
    MoneyProposal,
    PackagingProposal,
    PipelineTrace,
    TextProposal,
)
from .enums import ReviewRequirement


CONTRACT_ID = "catalogue.normalized_row.v1"


class ClaimRawFields(ContractModel):
    """Raw source strings preserved separately from Rosetta's proposals."""

    supplier_sku: str | None = Field(None, description="Supplier SKU exactly as printed.")
    product_name: str | None = Field(None, description="Product name/description exactly as printed or translated.")
    original_product_name: str | None = Field(None, description="Original source-language name when translated.")
    brand: str | None = Field(None, description="Brand text exactly as printed.")
    category: str | None = Field(None, description="Category/section text exactly as printed.")
    cost: str | None = Field(None, description="Cost text exactly as printed, including symbols or notes.")
    rrp: str | None = Field(None, description="Recommended retail price exactly as printed.")
    packaging: str | None = Field(None, description="Packaging text exactly as printed.")
    mbb_text: str | None = Field(None, description="Raw Max Bulk Buy / discount text.")
    barcode: str | None = Field(None, description="Barcode exactly as printed.")
    variant: str | None = Field(None, description="Variant/size/flavour text exactly as printed.")
    species: str | None = Field(None, description="Species text exactly as printed.")
    segment: str | None = Field(None, description="Supplier segment text exactly as printed.")
    effective_date: str | None = Field(None, description="Effective-date text exactly as printed.")
    content_measure: str | None = Field(None, description="Content measure exactly as printed.")
    row_eligibility: str | None = Field(None, description="Source value used to establish row eligibility.")
    additional_fields: JsonObject = Field(
        default_factory=dict,
        description="All contract-declared source values preserved by stable field key.",
    )
    source_row_label: str | None = Field(None, description="Human-readable source row label when useful.")


class NormalizedCatalogueFields(ContractModel):
    """Typed interpretation Rosetta proposes from raw source evidence."""

    supplier_sku: TextProposal | None = Field(None, description="Proposed supplier SKU.")
    product_name: TextProposal | None = Field(None, description="Proposed product name.")
    brand: TextProposal | None = Field(None, description="Proposed brand.")
    category: TextProposal | None = Field(None, description="Proposed category.")
    barcode: TextProposal | None = Field(None, description="Proposed barcode.")
    variant: TextProposal | None = Field(None, description="Proposed variant.")
    cost: CostProposal | None = Field(None, description="Proposed basis-aware supplier cost.")
    rrp: MoneyProposal | None = Field(None, description="Proposed recommended retail price.")
    packaging: PackagingProposal | None = Field(None, description="Proposed packaging configuration.")
    species: TextProposal | None = Field(None, description="Proposed species classification.")
    segment: TextProposal | None = Field(None, description="Proposed supplier segment.")
    effective_date: DateProposal | None = Field(None, description="Proposed supplier effective date.")
    mbb_terms: list[MbbTerm] = Field(default_factory=list, description="Proposed MBB terms or tiers.")


class NormalizedRowV1(ContractModel):
    """What business fields Rosetta proposes were present, and what it thinks they mean."""

    contract_id = CONTRACT_ID

    contract_version: Literal["catalogue.normalized_row.v1"] = Field(
        ...,
        description="Exact CIS-103 interpreted claim contract identifier.",
    )
    trace: PipelineTrace = Field(..., description="Common catalogue pipeline trace metadata.")
    catalogue_item_id: UUID = Field(..., description="interpreted claim pipeline identity.")
    raw_observation_ids: list[UUID] = Field(..., min_length=1, description="Raw observations supporting this staged item.")
    raw_fields: ClaimRawFields = Field(..., description="Source strings preserved as evidence.")
    normalized_fields: NormalizedCatalogueFields = Field(..., description="Typed proposals from parser/model/business rules.")
    review_requirement: ReviewRequirement = Field(..., description="Whether business review is needed.")
    validation_issue_ids: list[UUID] = Field(default_factory=list, description="Validation issues associated with the staged item.")
    created_at: datetime = Field(..., description="Timezone-aware creation timestamp.")
    metadata: JsonObject = Field(default_factory=dict, description="Explicit extension point for non-contract metadata.")

    @model_validator(mode="after")
    def _claim_requires_evidence_link(self):
        if not self.raw_observation_ids:
            raise ValueError("interpreted claim requires at least one extracted evidence observation link")
        if len(self.raw_observation_ids) != len(set(self.raw_observation_ids)):
            raise ValueError("interpreted claim raw_observation_ids must be unique")
        return self


register_contract(NormalizedRowV1)
