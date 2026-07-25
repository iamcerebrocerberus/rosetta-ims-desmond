"""Typed supplier-source catalogue contract declarations.

These models describe incoming supplier catalogue formats. They are deliberately
separate from the shared extracted evidence observation, Staging, Mastering, and Serving
pipeline payload contracts.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.catalogue_pipeline.common import UnitOfMeasure
from schemas.catalogue_pipeline.enums import IssueSeverity, MbbScope, SourceFormat


SUPPLIER_SOURCE_SCHEMA_VERSION = "catalogue.supplier_source_contract.v1"


class SupplierSourceModel(BaseModel):
    """Base for supplier-source declaration objects."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    @field_validator("*", mode="after")
    @classmethod
    def _datetimes_must_be_timezone_aware(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must be timezone-aware")
        return value


class SupplierContractSupportStatus(str, Enum):
    """Lifecycle state for supplier-format contract declarations."""

    SUPPORTED = "SUPPORTED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    DEPRECATED = "DEPRECATED"


class SupplierDocumentType(str, Enum):
    """Known supplier document types."""

    PRICE_LIST = "PRICE_LIST"
    PROMOTION_SHEET = "PROMOTION_SHEET"
    PRODUCT_LIST = "PRODUCT_LIST"
    CATALOGUE = "CATALOGUE"
    OTHER = "OTHER"


class SupplierSourceEvidenceType(str, Enum):
    """Evidence classes used to justify supplier-source declarations."""

    REAL_SOURCE_CATALOGUE_SAMPLE = "REAL_SOURCE_CATALOGUE_SAMPLE"
    EXISTING_PRODUCTION_TEST_EXTRACTION_FIXTURE = "EXISTING_PRODUCTION_TEST_EXTRACTION_FIXTURE"
    PARSER_BEHAVIOR = "PARSER_BEHAVIOR"
    BUSINESS_DOMAIN_DOCUMENTATION = "BUSINESS_DOMAIN_DOCUMENTATION"
    MISSING = "MISSING"


class SourceFieldRequirement(str, Enum):
    """Whether a source field is required for the format."""

    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    CONDITIONALLY_REQUIRED = "CONDITIONALLY_REQUIRED"


class SourceFieldRole(str, Enum):
    """Semantic role played by a source field in a supplier document."""

    SUPPLIER_SKU = "SUPPLIER_SKU"
    PRODUCT_NAME = "PRODUCT_NAME"
    BRAND = "BRAND"
    CATEGORY = "CATEGORY"
    SOURCE_PRICE = "SOURCE_PRICE"
    RRP = "RRP"
    PACKAGING = "PACKAGING"
    MBB_TEXT = "MBB_TEXT"
    BARCODE = "BARCODE"
    VARIANT = "VARIANT"
    SPECIES = "SPECIES"
    SEGMENT = "SEGMENT"
    EFFECTIVE_DATE = "EFFECTIVE_DATE"
    ORDER_INCREMENT = "ORDER_INCREMENT"
    CONTENT_MEASURE = "CONTENT_MEASURE"
    ROW_ELIGIBILITY = "ROW_ELIGIBILITY"
    OTHER = "OTHER"


class SemanticResolutionStatus(str, Enum):
    """How strongly a source-format semantic has been established."""

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNRESOLVED = "UNRESOLVED"


class EvidenceReference(SupplierSourceModel):
    """A concrete source of evidence for a supplier-source declaration."""

    evidence_type: SupplierSourceEvidenceType = Field(..., description="Kind of evidence available.")
    reference: str = Field(..., min_length=1, description="Repository path, test name, document, or source citation.")
    note: str | None = Field(None, description="Concise explanation of what this evidence proves or does not prove.")


class SupplierSourceReference(SupplierSourceModel):
    """Supplier identity for source contracts, allowing pre-reconciled supplier codes."""

    supplier_id: int | None = Field(
        None,
        gt=0,
        description="Current Rosetta numeric supplier ID when known.",
    )
    supplier_name: str = Field(..., min_length=1, description="Human-readable supplier name.")
    supplier_code: str | None = Field(None, min_length=1, description="Stable supplier code or abbreviation when known.")

    @field_validator("supplier_name", "supplier_code", mode="before")
    @classmethod
    def _supplier_text_is_meaningful(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("supplier identity text cannot be blank")
        return value

    @model_validator(mode="after")
    def _requires_id_or_code(self):
        if self.supplier_id is None and not self.supplier_code:
            raise ValueError("supplier source reference requires supplier_id or supplier_code")
        return self


class SourceTableRegion(SupplierSourceModel):
    """Expected table or section region within a supplier source document."""

    name: str = Field(..., min_length=1, description="Region name used by operators and tests.")
    selector: str | None = Field(None, description="Sheet name, section heading, page cue, or other locator.")
    header_row: int | None = Field(None, gt=0, description="Expected 1-based header row when known.")
    notes: str | None = Field(None, description="Business-readable location notes.")


class SourceStructure(SupplierSourceModel):
    """Expected physical structure for a supplier catalogue format."""

    source_format: SourceFormat = Field(..., description="Physical source format.")
    expected_sheet_names: list[str] = Field(default_factory=list, description="Expected spreadsheet sheets, if any.")
    expected_sections: list[str] = Field(default_factory=list, description="Expected PDF/document sections, if known.")
    table_regions: list[SourceTableRegion] = Field(default_factory=list, description="Expected source table regions.")
    required_headers: list[str] = Field(default_factory=list, description="Headers required before interpretation.")
    optional_headers: list[str] = Field(default_factory=list, description="Headers that may be present.")
    row_eligibility_rules: list[str] = Field(default_factory=list, description="Rules for deciding whether a source row is a catalogue item.")
    skip_rules: list[str] = Field(default_factory=list, description="Rows or sections intentionally skipped.")
    source_location_expectations: list[str] = Field(
        default_factory=list,
        description="Expected source locations needed by extracted evidence observation, for example page and row.",
    )


class SourceFieldContract(SupplierSourceModel):
    """A source field and its supplier-format-specific semantic role."""

    field_key: str = Field(..., min_length=1, description="Stable key used inside this supplier contract.")
    role: SourceFieldRole = Field(..., description="Business role of this source field.")
    requirement: SourceFieldRequirement = Field(..., description="Whether the field is required in the source.")
    source_column: str | None = Field(None, description="Exact source column/header when tabular.")
    source_path: str | None = Field(None, description="Section, banner, or document path when not a table column.")
    composed_from: list[str] = Field(default_factory=list, description="Source columns joined to form this field.")
    constant_value: str | None = Field(None, description="Supplier-format constant value, if not printed per row.")
    aliases: list[str] = Field(default_factory=list, description="Observed header aliases justified by evidence.")
    description: str | None = Field(None, description="Readable explanation of the mapping.")
    evidence: list[EvidenceReference] = Field(default_factory=list, description="Evidence supporting this field mapping.")

    @model_validator(mode="after")
    def _mapping_has_location_or_constant(self):
        if not (self.source_column or self.source_path or self.composed_from or self.constant_value is not None):
            raise ValueError("source field requires source_column, source_path, composed_from, or constant_value")
        if self.requirement == SourceFieldRequirement.REQUIRED and self.constant_value is not None:
            raise ValueError("required source fields must be observed, not only constant")
        if self.requirement == SourceFieldRequirement.REQUIRED and not (self.source_column or self.source_path or self.composed_from):
            raise ValueError("required source fields require source_column, source_path, or composed_from")
        return self


class PricingSourceSemantics(SupplierSourceModel):
    """Supplier-format rules for interpreting source price fields."""

    cost_source_field: str | None = Field(None, description="Field key containing supplier cost.")
    rrp_source_field: str | None = Field(None, description="Field key containing retail/RRP price, if present.")
    currency: Literal["HKD"] = Field("HKD", description="v1 source contracts accept HKD only.")
    price_basis: UnitOfMeasure | None = Field(None, description="Unit basis that the supplier cost prices, when established.")
    price_basis_status: SemanticResolutionStatus = Field(..., description="Evidence status for the price basis.")
    autoswap_cost_rrp_allowed: bool = Field(False, description="Whether swapped cost/RRP may be deterministically corrected.")
    null_cost_markers: list[str] = Field(default_factory=list, description="Source values that mean cost is unavailable.")
    notes: str | None = Field(None, description="Business-readable price semantics.")

    @model_validator(mode="after")
    def _validate_pricing_semantics(self):
        if self.price_basis_status == SemanticResolutionStatus.UNRESOLVED and self.price_basis is not None:
            raise ValueError("unresolved price basis must leave price_basis null")
        if self.price_basis_status != SemanticResolutionStatus.UNRESOLVED and self.price_basis is None:
            raise ValueError("resolved price basis requires price_basis")
        if self.price_basis is not None:
            self.price_basis.require_known_code("pricing.price_basis")
        if self.autoswap_cost_rrp_allowed and not self.rrp_source_field:
            raise ValueError("autoswap_cost_rrp_allowed requires rrp_source_field")
        return self


class PackagingSourceSemantics(SupplierSourceModel):
    """Supplier-format rules for packaging, content, and ordering semantics."""

    packaging_source_field: str | None = Field(None, description="Field key containing printed packaging text.")
    purchase_uom: UnitOfMeasure | None = Field(None, description="Purchase unit when explicitly known.")
    price_basis: UnitOfMeasure | None = Field(None, description="Price basis repeated for packaging cross-checks.")
    sellable_unit_uom: UnitOfMeasure | None = Field(None, description="Sellable-unit UOM when explicitly known.")
    sellable_units_per_purchase_unit_source_field: str | None = Field(
        None,
        description="Field key proving sellable units per purchase unit, when known.",
    )
    content_measure_source_field: str | None = Field(None, description="Field key proving content amount/UOM.")
    content_measure_uom: UnitOfMeasure | None = Field(None, description="Content-measure UOM when fixed by the format.")
    order_increment_source_field: str | None = Field(None, description="Field key proving supplier order multiple.")
    minimum_order_source_field: str | None = Field(None, description="Field key proving minimum order quantity.")
    break_pack_allowed: bool | None = Field(None, description="Whether break-pack purchasing is known from the source.")
    interpretation_rules: list[str] = Field(default_factory=list, description="Supplier-specific packaging interpretation rules.")
    unresolved_semantics: list[str] = Field(default_factory=list, description="Unknown semantics that must remain null.")

    @model_validator(mode="after")
    def _validate_packaging_semantics(self):
        if (
            self.sellable_units_per_purchase_unit_source_field
            and self.content_measure_source_field
            and self.sellable_units_per_purchase_unit_source_field == self.content_measure_source_field
        ):
            raise ValueError("content measure source cannot be reused as sellable-units-per-purchase-unit proof")
        for field_name in ("purchase_uom", "price_basis", "sellable_unit_uom", "content_measure_uom"):
            uom = getattr(self, field_name)
            if uom is not None and uom.code is not None:
                uom.require_known_code(f"packaging.{field_name}")
        return self


class MbbSourceSemantics(SupplierSourceModel):
    """Supplier-format rules for MBB, discounts, tiers, or free-goods notation."""

    source_fields: list[str] = Field(default_factory=list, description="Field keys containing MBB or promotion text.")
    supported_scopes: list[MbbScope] = Field(default_factory=list, description="MBB scopes this format can express.")
    condition_patterns: list[str] = Field(default_factory=list, description="Known condition notations.")
    benefit_patterns: list[str] = Field(default_factory=list, description="Known benefit notations.")
    requires_validation_issue_when: list[str] = Field(
        default_factory=list,
        description="Ambiguity cases that require ValidationIssueV1 instead of normalization.",
    )
    notes: str | None = Field(None, description="MBB interpretation notes.")


class SupplierValidationRule(SupplierSourceModel):
    """A supplier-specific validation rule declaration."""

    rule_id: str = Field(..., min_length=1, description="Stable rule identifier.")
    description: str = Field(..., min_length=1, description="Business-readable rule description.")
    source_expression: str | None = Field(None, description="Legacy expression or executable rule name.")
    severity: IssueSeverity = Field(..., description="ValidationIssueV1 severity to use when this rule fails.")
    issue_code: str = Field(..., min_length=1, description="Stable ValidationIssueV1 issue code.")
    review_guidance: str = Field(..., min_length=1, description="Instruction a business operator can act on.")
    evidence: list[EvidenceReference] = Field(default_factory=list, description="Evidence supporting this validation rule.")


class AmbiguityRule(SupplierSourceModel):
    """A known ambiguity that must not be guessed by a parser."""

    issue_code: str = Field(..., min_length=1, description="Stable issue code to create when ambiguity is encountered.")
    condition: str = Field(..., min_length=1, description="Business-readable ambiguity condition.")
    review_guidance: str = Field(..., min_length=1, description="Decision needed from BizOps/HITL.")
    blocks_supported_status: bool = Field(
        False,
        description="Whether this open ambiguity prevents setting the contract to SUPPORTED.",
    )


class PipelineContractMapping(SupplierSourceModel):
    """Mapping from a supplier-source contract into shared pipeline payloads."""

    raw_observation_contract_id: Literal["catalogue.extracted_evidence.v1"] = Field(
        "catalogue.extracted_evidence.v1",
        description="Shared extracted evidence observation contract produced from source evidence.",
    )
    staging_item_contract_id: Literal["catalogue.normalized_row.v1"] = Field(
        "catalogue.normalized_row.v1",
        description="Shared interpreted claim contract receiving proposed interpretations.",
    )
    raw_observation_fields: list[str] = Field(..., min_length=1, description="Source field keys preserved as raw evidence.")
    staging_raw_field_keys: list[str] = Field(..., min_length=1, description="Source field keys copied to staging.raw_fields.")
    staging_proposed_field_keys: list[str] = Field(
        ...,
        min_length=1,
        description="Source field keys interpreted into staging.normalized_fields.",
    )


class SupplierSourceContractV1(SupplierSourceModel):
    """Versioned supplier-specific source contract declaration."""

    schema_version: Literal["catalogue.supplier_source_contract.v1"] = Field(
        ...,
        description="Exact Pydantic schema version for supplier-source contract declarations.",
    )
    contract_id: str = Field(
        ...,
        min_length=1,
        description="Stable supplier-format contract identity, for example hills.price_list.v1.",
    )
    contract_version: Literal["v1"] = Field(..., description="Supplier-format major version.")
    supplier: SupplierSourceReference = Field(..., description="Supplier this source format belongs to.")
    document_type: SupplierDocumentType = Field(..., description="Supplier document type.")
    format_name: str = Field(..., min_length=1, description="Human-readable source format name.")
    source_format: SourceFormat = Field(..., description="Physical source format.")
    support_status: SupplierContractSupportStatus = Field(..., description="Lifecycle/support state.")
    effective_from: date | None = Field(None, description="Supplier-format effective date when source verified.")
    effective_to: date | None = Field(None, description="Supplier-format retirement/effective-to date when known.")
    evidence: list[EvidenceReference] = Field(..., min_length=1, description="Evidence used to justify this declaration.")
    source_structure: SourceStructure = Field(..., description="Expected source structure.")
    fields: list[SourceFieldContract] = Field(..., min_length=1, description="Source field declarations.")
    pricing: PricingSourceSemantics = Field(..., description="Supplier price source semantics.")
    packaging: PackagingSourceSemantics = Field(..., description="Packaging and ordering source semantics.")
    mbb: MbbSourceSemantics = Field(default_factory=MbbSourceSemantics, description="MBB or promotion source semantics.")
    validation_rules: list[SupplierValidationRule] = Field(
        default_factory=list,
        description="Supplier-specific validation rules that map to ValidationIssueV1.",
    )
    known_ambiguities: list[AmbiguityRule] = Field(default_factory=list, description="Known source-format ambiguities.")
    pipeline_mapping: PipelineContractMapping = Field(..., description="How this source contract maps into shared contracts.")
    created_at: datetime = Field(..., description="Timezone-aware declaration creation timestamp.")
    created_by: str = Field(..., min_length=1, description="Declaration author.")
    updated_at: datetime | None = Field(None, description="Timezone-aware declaration update timestamp.")
    updated_by: str | None = Field(None, description="Declaration updater.")
    metadata: dict[str, str] = Field(default_factory=dict, description="Explicit extension point for non-contract metadata.")

    @field_validator("contract_id", "format_name", "created_by", "updated_by", mode="before")
    @classmethod
    def _identity_text_is_meaningful(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("supplier source contract identity text cannot be blank")
        return value

    @model_validator(mode="after")
    def _validate_contract(self):
        if not self.contract_id.endswith(f".{self.contract_version}"):
            raise ValueError("contract_id must include the contract_version suffix")
        if self.source_format != self.source_structure.source_format:
            raise ValueError("source_format must match source_structure.source_format")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot be earlier than effective_from")
        if self.updated_at is not None and self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        if self.updated_at is not None and not self.updated_by:
            raise ValueError("updated_by is required when updated_at is supplied")

        field_keys = [field.field_key for field in self.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("field_key values must be unique")
        known_fields = set(field_keys)
        self._assert_field_ref(self.pricing.cost_source_field, known_fields, "pricing.cost_source_field")
        self._assert_field_ref(self.pricing.rrp_source_field, known_fields, "pricing.rrp_source_field")
        self._assert_field_ref(self.packaging.packaging_source_field, known_fields, "packaging.packaging_source_field")
        self._assert_field_ref(
            self.packaging.sellable_units_per_purchase_unit_source_field,
            known_fields,
            "packaging.sellable_units_per_purchase_unit_source_field",
        )
        self._assert_field_ref(
            self.packaging.content_measure_source_field,
            known_fields,
            "packaging.content_measure_source_field",
        )
        self._assert_field_ref(
            self.packaging.order_increment_source_field,
            known_fields,
            "packaging.order_increment_source_field",
        )
        self._assert_field_ref(self.packaging.minimum_order_source_field, known_fields, "packaging.minimum_order_source_field")
        for source_field in self.mbb.source_fields:
            self._assert_field_ref(source_field, known_fields, "mbb.source_fields")
        for source_field in (
            self.pipeline_mapping.raw_observation_fields
            + self.pipeline_mapping.staging_raw_field_keys
            + self.pipeline_mapping.staging_proposed_field_keys
        ):
            self._assert_field_ref(source_field, known_fields, "pipeline_mapping field reference")

        evidence_types = {item.evidence_type for item in self.evidence}
        if self.support_status == SupplierContractSupportStatus.SUPPORTED:
            if self.supplier.supplier_id is None:
                raise ValueError("SUPPORTED supplier contracts require a numeric supplier_id for runtime selection")
            if evidence_types <= {SupplierSourceEvidenceType.MISSING}:
                raise ValueError("SUPPORTED supplier contracts require evidence beyond missing evidence")
            if self.pricing.price_basis_status != SemanticResolutionStatus.VERIFIED:
                raise ValueError("SUPPORTED supplier contracts require VERIFIED price basis semantics")
            blocking_ambiguities = [item.issue_code for item in self.known_ambiguities if item.blocks_supported_status]
            if blocking_ambiguities:
                raise ValueError(f"SUPPORTED supplier contracts cannot have blocking ambiguities: {blocking_ambiguities}")
        return self

    @staticmethod
    def _assert_field_ref(field_ref: str | None, known_fields: set[str], label: str) -> None:
        if field_ref is not None and field_ref not in known_fields:
            raise ValueError(f"{label} references unknown field_key '{field_ref}'")
