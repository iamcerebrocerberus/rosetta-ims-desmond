"""extracted evidence observation Contract v1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, register_contract
from .common import ExtractionProfileReference, JsonObject
from .enums import ExtractionMethod


CONTRACT_ID = "catalogue.extracted_evidence.v1"


class BoundingBox(ContractModel):
    """Non-negative source bounding box, typically in page/image coordinates."""

    x: Decimal = Field(..., ge=Decimal("0"), description="Left coordinate.")
    y: Decimal = Field(..., ge=Decimal("0"), description="Top coordinate.")
    width: Decimal = Field(..., gt=Decimal("0"), description="Bounding-box width.")
    height: Decimal = Field(..., gt=Decimal("0"), description="Bounding-box height.")
    unit: str | None = Field(None, description="Coordinate unit, for example px or pt.")

    @field_validator("unit", mode="before")
    @classmethod
    def _unit_is_meaningful(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("bounding_box.unit cannot be blank")
        return value


class SourceLocation(ContractModel):
    """Location of evidence inside the source file."""

    page_number: int | None = Field(None, gt=0, description="1-based page number for paged sources.")
    sheet_name: str | None = Field(None, description="Spreadsheet sheet name.")
    row_number: int | None = Field(None, gt=0, description="1-based spreadsheet row number.")
    cell_range: str | None = Field(None, description="Spreadsheet range, for example A2:H2.")
    bounding_box: BoundingBox | None = Field(None, description="Bounding box around the observed object.")
    source_object_key: str | None = Field(None, description="Optional storage/object key or source-specific reference.")

    @field_validator("sheet_name", "cell_range", "source_object_key", mode="before")
    @classmethod
    def _locator_text_is_meaningful(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("source location text locators cannot be blank")
        return value

    @model_validator(mode="after")
    def _requires_meaningful_locator(self):
        if not any(
            (
                self.page_number is not None,
                self.sheet_name is not None,
                self.row_number is not None,
                self.cell_range is not None,
                self.bounding_box is not None,
                self.source_object_key is not None,
            )
        ):
            raise ValueError("SourceLocation requires at least one meaningful locator")
        return self


class RawCell(ContractModel):
    """One raw cell observed in a spreadsheet-like source."""

    cell_reference: str | None = Field(None, description="Cell reference, for example B12.")
    row_number: int | None = Field(None, gt=0, description="1-based row number.")
    column_name: str | None = Field(None, description="Source column/header label.")
    column_index: int | None = Field(None, gt=0, description="1-based source column index.")
    raw_value: Any = Field(..., description="Raw cell value as extracted from the source.")


class ExtractedEvidenceV1(ContractModel):
    """What the extraction system observed in the source catalogue, and where."""

    contract_id = CONTRACT_ID

    contract_version: Literal["catalogue.extracted_evidence.v1"] = Field(
        ...,
        description="Exact CIS-103 extracted evidence observation contract identifier.",
    )
    raw_observation_id: UUID = Field(..., description="extracted evidence observation pipeline identity.")
    ingestion_run_id: UUID = Field(..., description="Pipeline run identity.")
    supplier_catalogue_id: UUID = Field(..., description="Supplier catalogue identity.")
    source_file_id: UUID = Field(..., description="Raw source file identity.")
    extraction_profile: ExtractionProfileReference = Field(..., description="Extraction profile used.")
    source_location: SourceLocation = Field(..., description="Location where the evidence was observed.")
    raw_text: str | None = Field(None, description="Observed raw text, if the source was text-like.")
    raw_cells: list[RawCell] = Field(default_factory=list, description="Observed raw cells for tabular sources.")
    extraction_method: ExtractionMethod = Field(..., description="How this observation was captured.")
    captured_at: datetime = Field(..., description="Timezone-aware capture timestamp.")
    extraction_model: str | None = Field(None, description="Model/provider used for extraction when applicable.")
    extraction_model_version: str | None = Field(None, description="Version of the extraction model when applicable.")
    extraction_confidence: Decimal | None = Field(
        None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description="Observation-level confidence in [0, 1].",
    )
    source_metadata: JsonObject = Field(default_factory=dict, description="Explicit extension point for source metadata.")

    @field_validator("extraction_confidence", mode="before")
    @classmethod
    def _confidence_not_float(cls, value):
        if isinstance(value, float):
            raise ValueError("confidence must be provided as a decimal string, integer, or Decimal")
        return value

    @model_validator(mode="after")
    def _requires_evidence(self):
        has_text = bool(self.raw_text and self.raw_text.strip())
        has_cells = any(cell.raw_value is not None and str(cell.raw_value).strip() for cell in self.raw_cells)
        if not has_text and not has_cells:
            raise ValueError("extracted evidence observation requires raw_text or at least one raw cell with evidence")
        return self


register_contract(ExtractedEvidenceV1)
