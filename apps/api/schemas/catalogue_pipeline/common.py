"""Shared value objects for catalogue pipeline contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import ContractModel
from .enums import (
    FixedDiscountBasis,
    MbbScope,
    MbbSelectionMethod,
    UnitCode,
)


def _decimal_without_float(value):
    if isinstance(value, float):
        raise ValueError("decimal values must be provided as strings, integers, or Decimal instances; floats are not accepted")
    return value


class ExtractionProfileReference(ContractModel):
    """Stable reference to the extraction profile used for a pipeline payload."""

    profile_id: str = Field(..., min_length=1, description="Stable extraction profile identifier.")
    profile_version: str = Field(..., min_length=1, description="Version of the extraction profile used.")


class SupplierReference(ContractModel):
    """Reference compatible with the repository's current integer supplier IDs."""

    supplier_id: int = Field(..., gt=0, description="Current Rosetta numeric supplier ID.")
    supplier_name: str = Field(..., min_length=1, description="Human-readable supplier name.")
    supplier_code: str | None = Field(None, description="Optional short supplier code when known.")


class PipelineTrace(ContractModel):
    """Common identifiers that connect records produced by one catalogue ingestion run."""

    ingestion_run_id: UUID = Field(..., description="Pipeline run identity.")
    supplier_catalogue_id: UUID = Field(..., description="Source catalogue identity for the uploaded document.")
    source_file_id: UUID = Field(..., description="Raw source file identity.")
    extraction_profile: ExtractionProfileReference = Field(..., description="Extraction profile used to interpret the source.")


class UnitOfMeasure(ContractModel):
    """Controlled unit code plus optional label for deliberate OTHER classifications."""

    code: UnitCode | None = Field(None, description="Controlled UOM code. Null means not determined.")
    label: str | None = Field(None, description="Required only when code is OTHER.")

    @model_validator(mode="after")
    def _validate_other_label(self):
        if self.code == UnitCode.OTHER and not (self.label and self.label.strip()):
            raise ValueError("OTHER UOM requires a label")
        if self.code is not None and self.code != UnitCode.OTHER and self.label is not None:
            raise ValueError("UOM label is only allowed when code is OTHER")
        if self.code is None and self.label is not None:
            raise ValueError("UOM label cannot be supplied without a UOM code")
        return self

    def require_known_code(self, field_name: str = "uom") -> None:
        if self.code is None:
            raise ValueError(f"{field_name} requires a UOM code")


class Money(ContractModel):
    """Non-negative HKD monetary amount for v1 contracts."""

    amount: Decimal = Field(..., ge=Decimal("0"), description="Monetary amount. JSON serializes as a string.")
    currency: Literal["HKD"] = Field("HKD", description="Currency. v1 accepts HKD only.")

    _amount_not_float = field_validator("amount", mode="before")(_decimal_without_float)


class PositiveMoney(Money):
    """Positive HKD money amount."""

    amount: Decimal = Field(..., gt=Decimal("0"), description="Positive monetary amount. JSON serializes as a string.")


class Quantity(ContractModel):
    """Positive quantity in an explicitly stated UOM."""

    amount: Decimal = Field(..., gt=Decimal("0"), description="Positive quantity. JSON serializes as a string.")
    uom: UnitOfMeasure = Field(..., description="Quantity unit.")

    _amount_not_float = field_validator("amount", mode="before")(_decimal_without_float)

    @model_validator(mode="after")
    def _quantity_requires_uom(self):
        self.uom.require_known_code("quantity.uom")
        return self


class Cost(ContractModel):
    """Basis-aware supplier cost."""

    amount: Decimal = Field(..., ge=Decimal("0"), description="Cost amount. JSON serializes as a string.")
    currency: Literal["HKD"] = Field("HKD", description="Currency. v1 accepts HKD only.")
    price_basis: UnitOfMeasure = Field(..., description="Unit basis the amount prices.")

    _amount_not_float = field_validator("amount", mode="before")(_decimal_without_float)

    @model_validator(mode="after")
    def _cost_requires_price_basis(self):
        self.price_basis.require_known_code("cost.price_basis")
        return self


class PackagingConfiguration(ContractModel):
    """Structured purchasing, price-basis, sellable-unit, content, and ordering semantics."""

    purchase_uom: UnitOfMeasure | None = Field(None, description="Unit Rosetta purchases from the supplier.")
    price_basis: UnitOfMeasure | None = Field(None, description="Unit basis for the quoted supplier price.")
    sellable_unit_uom: UnitOfMeasure | None = Field(None, description="Smallest sellable unit UOM.")
    sellable_units_per_purchase_unit: Decimal | None = Field(
        None,
        gt=Decimal("0"),
        description="Number of sellable units contained in one purchase unit when known.",
    )
    content_amount: Decimal | None = Field(
        None,
        gt=Decimal("0"),
        description="Content measure of one sellable unit, for example 410 or 30.",
    )
    content_uom: UnitOfMeasure | None = Field(None, description="Content-measure unit, for example G or ML.")
    order_increment: Quantity | None = Field(None, description="Supplier order multiple when known.")
    minimum_order_quantity: Quantity | None = Field(None, description="Minimum order quantity when known.")
    break_pack_allowed: bool | None = Field(None, description="Whether supplier allows ordering below a full purchase unit.")
    source_text: str | None = Field(None, description="Raw packaging text this structure was interpreted from.")

    _sellable_not_float = field_validator("sellable_units_per_purchase_unit", mode="before")(_decimal_without_float)
    _content_not_float = field_validator("content_amount", mode="before")(_decimal_without_float)

    @model_validator(mode="after")
    def _validate_packaging(self):
        if (self.content_amount is None) != (self.content_uom is None):
            raise ValueError("content_amount and content_uom must be supplied together")
        if self.content_uom is not None:
            self.content_uom.require_known_code("content_uom")
        return self


class FieldEvidence(ContractModel):
    """Field-level provenance and confidence for a proposed interpretation."""

    raw_observation_id: UUID = Field(..., description="extracted evidence observation that supports this field.")
    field_path: str | None = Field(None, description="JSON-style field path inside the supporting payload.")
    confidence: Decimal | None = Field(None, ge=Decimal("0"), le=Decimal("1"), description="Field confidence in [0, 1].")
    note: str | None = Field(None, description="Short provenance note.")

    _confidence_not_float = field_validator("confidence", mode="before")(_decimal_without_float)


class TextProposal(ContractModel):
    """Proposed text value plus optional field-level evidence."""

    value: str | None = Field(None, description="Proposed interpreted text value.")
    evidence: FieldEvidence | None = Field(None, description="Evidence supporting this proposal.")


class CostProposal(Cost):
    """Proposed cost with optional provenance."""

    evidence: FieldEvidence | None = Field(None, description="Evidence supporting this cost proposal.")


class PackagingProposal(PackagingConfiguration):
    """Proposed packaging configuration with optional provenance."""

    evidence: FieldEvidence | None = Field(None, description="Evidence supporting this packaging proposal.")


class MinimumQuantityCondition(ContractModel):
    """MBB condition unlocked by buying at least a positive quantity."""

    condition_type: Literal["minimum_quantity"] = Field("minimum_quantity", description="Discriminator.")
    quantity: Quantity = Field(..., description="Minimum quantity required to unlock the benefit.")


class MinimumSpendCondition(ContractModel):
    """MBB condition unlocked by spending at least a positive HKD amount."""

    condition_type: Literal["minimum_spend"] = Field("minimum_spend", description="Discriminator.")
    spend: PositiveMoney = Field(..., description="Minimum supplier-order spend in HKD.")


MbbCondition = Annotated[MinimumQuantityCondition | MinimumSpendCondition, Field(discriminator="condition_type")]


class DiscountedUnitPriceBenefit(ContractModel):
    """MBB benefit that changes the unit price to a quoted discounted amount."""

    benefit_type: Literal["discounted_unit_price"] = Field("discounted_unit_price", description="Discriminator.")
    discounted_price: Cost = Field(..., description="Discounted HKD unit price and its price basis.")


class PercentageDiscountBenefit(ContractModel):
    """MBB benefit that applies a percentage discount."""

    benefit_type: Literal["percentage_discount"] = Field("percentage_discount", description="Discriminator.")
    percentage: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"), description="Discount percentage in (0, 100].")

    _percentage_not_float = field_validator("percentage", mode="before")(_decimal_without_float)


class FixedDiscountBenefit(ContractModel):
    """MBB benefit that subtracts a fixed HKD amount from a defined basis."""

    benefit_type: Literal["fixed_discount"] = Field("fixed_discount", description="Discriminator.")
    amount: PositiveMoney = Field(..., description="Positive HKD fixed discount.")
    reduction_basis: FixedDiscountBasis = Field(..., description="What the fixed discount reduces.")


class FreeQuantityBenefit(ContractModel):
    """MBB benefit that grants free goods or free quantity."""

    benefit_type: Literal["free_quantity"] = Field("free_quantity", description="Discriminator.")
    quantity: Quantity = Field(..., description="Positive free quantity and UOM.")


MbbBenefit = Annotated[
    DiscountedUnitPriceBenefit | PercentageDiscountBenefit | FixedDiscountBenefit | FreeQuantityBenefit,
    Field(discriminator="benefit_type"),
]


class MbbTerm(ContractModel):
    """Condition plus benefit for one Max Bulk Buy term or tier."""

    mbb_term_id: UUID = Field(..., description="Stable MBB term identity for this pipeline payload.")
    scope: MbbScope = Field(..., description="Business scope the term applies to.")
    condition: MbbCondition = Field(..., description="Condition that unlocks the term.")
    benefit: MbbBenefit = Field(..., description="Benefit unlocked by the condition.")
    description: str | None = Field(None, description="Business-readable text copied or summarized from the source.")
    effective_from: date | None = Field(None, description="Supplier effective date when known.")
    effective_to: date | None = Field(None, description="End date when known.")
    evidence: FieldEvidence | None = Field(None, description="Evidence supporting this MBB term.")


class MbbSelection(ContractModel):
    """Which MBB term Rosetta selected and whether that selection was overridden."""

    selected_term_id: UUID = Field(..., description="Selected MBB term identity.")
    selection_method: MbbSelectionMethod = Field(..., description="Automatic calculation or reviewed override.")
    selected_by: str | None = Field(None, description="Reviewer identity when manually overridden.")
    override_reason: str | None = Field(None, description="Business reason for an override.")
    review_decision_id: UUID | None = Field(None, description="Review decision that records the override.")

    @model_validator(mode="after")
    def _override_requires_audit_detail(self):
        if self.selection_method == MbbSelectionMethod.OVERRIDDEN:
            if not (self.review_decision_id or (self.selected_by and self.override_reason)):
                raise ValueError("overridden MBB selection requires review_decision_id or selected_by plus override_reason")
        return self


class LineageReference(ContractModel):
    """Trace from mastered or served values back to staging and raw evidence."""

    catalogue_item_id: UUID = Field(..., description="interpreted claim identity.")
    raw_observation_ids: list[UUID] = Field(..., min_length=1, description="Raw observations supporting the assertion.")
    field_paths: list[str] = Field(default_factory=list, description="Optional field paths covered by this lineage.")
    review_decision_id: UUID | None = Field(None, description="Review decision that approved or overrode the assertion.")

    @model_validator(mode="after")
    def _extracted_evidence_ids_are_unique(self):
        if len(self.raw_observation_ids) != len(set(self.raw_observation_ids)):
            raise ValueError("lineage raw_observation_ids must be unique")
        return self


class ExternalMapping(ContractModel):
    """Mapping between Rosetta and an external product/catalogue identifier."""

    system: str = Field(..., min_length=1, description="External system name.")
    external_id: str = Field(..., min_length=1, description="External identifier.")
    external_label: str | None = Field(None, description="External display label when known.")
    lineage: LineageReference | None = Field(None, description="Lineage supporting this mapping.")


JsonObject = dict[str, Any]
