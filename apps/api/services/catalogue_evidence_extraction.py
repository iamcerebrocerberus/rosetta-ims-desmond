"""Typed, source-located extraction for the boundary immediately before Raw.

This module deliberately does not parse catalogue evidence into product fields.
It records what was observed and where it was observed. Semantic interpretation
belongs after Raw persistence.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import platform
import re
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import openpyxl
import pypdf
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.catalogue_pipeline.enums import ExtractionMethod, SourceFormat
from schemas.catalogue_pipeline.extracted_evidence_v1 import BoundingBox, RawCell, SourceLocation


GEMINI_MODEL = "gemini-3.1-pro"
MAX_VISION_TOKENS = 8192


class ExtractionStatus(str, Enum):
    """Completeness of one source extraction attempt."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ExtractionError(BaseModel):
    """Sanitized operational error attached to an extraction result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    unit_key: str | None = None
    provider: str | None = None
    retryable: bool = False


class ExtractedEvidence(BaseModel):
    """One verbatim, source-located observation ready for Raw persistence."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    observation_key: str = Field(..., min_length=1)
    source_location: SourceLocation
    raw_text: str | None = None
    raw_cells: tuple[RawCell, ...] = ()
    extraction_method: ExtractionMethod
    provider: str | None = None
    provider_version: str | None = None
    provider_request_id: str | None = None
    model: str | None = None
    model_version: str | None = None
    confidence: Decimal | None = Field(None, ge=Decimal("0"), le=Decimal("1"))
    warnings: tuple[str, ...] = ()
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_not_float(cls, value):
        if isinstance(value, float):
            raise ValueError("confidence must be a decimal string, integer, or Decimal")
        return value

    @model_validator(mode="after")
    def _requires_verbatim_evidence(self):
        has_text = bool(self.raw_text and self.raw_text.strip())
        has_cells = any(cell.raw_value is not None and str(cell.raw_value).strip() for cell in self.raw_cells)
        if not has_text and not has_cells:
            raise ValueError("ExtractedEvidence requires raw_text or at least one non-empty raw cell")
        return self


class ExtractionResult(BaseModel):
    """Typed extraction envelope with explicit completeness accounting.

    A source unit (PDF page, XLSX worksheet, CSV parse, image) only counts as
    completed when it was fully observed or the provider EXPLICITLY classified
    it as containing no catalogue evidence (``empty_units``). "The provider
    call returned" is never equated with "the unit was completely observed".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ExtractionStatus
    source_format: SourceFormat
    observations: tuple[ExtractedEvidence, ...] = ()
    units_attempted: int = Field(..., ge=0)
    units_completed: int = Field(..., ge=0)
    empty_units: int = Field(0, ge=0, description="Units explicitly classified as containing no catalogue evidence.")
    warnings: tuple[str, ...] = ()
    errors: tuple[ExtractionError, ...] = ()

    @model_validator(mode="after")
    def _status_matches_contents(self):
        if self.units_completed > self.units_attempted:
            raise ValueError("units_completed cannot exceed units_attempted")
        if self.empty_units > self.units_completed:
            raise ValueError("empty_units cannot exceed units_completed")
        if self.status == ExtractionStatus.COMPLETE:
            if not self.observations and not self.empty_units:
                raise ValueError("COMPLETE extraction requires observations or explicit empty-unit accounting")
            if self.errors or self.units_completed != self.units_attempted:
                raise ValueError("COMPLETE extraction cannot contain errors or incomplete units")
        elif self.status == ExtractionStatus.PARTIAL:
            if not (self.observations or self.empty_units) or not self.errors:
                raise ValueError("PARTIAL extraction requires observed/empty units and errors")
        elif self.status == ExtractionStatus.FAILED:
            if self.observations or not self.errors:
                raise ValueError("FAILED extraction requires errors and cannot contain observations")
        return self


class _VisionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str | None = None
    raw_cells: tuple[RawCell, ...] = ()
    bounding_box: BoundingBox | None = None
    confidence: Decimal | None = Field(None, ge=Decimal("0"), le=Decimal("1"))

    @field_validator("raw_cells")
    @classmethod
    def _cell_values_are_verbatim_strings(cls, value):
        for cell in value:
            if not isinstance(cell.raw_value, str):
                raise ValueError("vision raw cell values must be verbatim strings")
        return value

    @model_validator(mode="after")
    def _requires_evidence(self):
        has_text = bool(self.raw_text and self.raw_text.strip())
        has_cells = any(cell.raw_value is not None and str(cell.raw_value).strip() for cell in self.raw_cells)
        if not has_text and not has_cells:
            raise ValueError("vision observation requires verbatim text or cells")
        return self


class _VisionEnvelope(BaseModel):
    """Typed page-level provider outcome.

    ``page_outcome`` is REQUIRED: an empty observation array is only
    acceptable when the provider explicitly classifies the page as containing
    no catalogue evidence. Empty-without-classification and
    evidence-with-empty-array are both malformed — they may hide truncation.
    """

    model_config = ConfigDict(extra="forbid")

    page_outcome: Literal["evidence", "no_catalogue_evidence"]
    observations: tuple[_VisionObservation, ...] = ()

    @model_validator(mode="after")
    def _outcome_matches_observations(self):
        if self.page_outcome == "evidence" and not self.observations:
            raise ValueError("page_outcome 'evidence' requires at least one observation")
        if self.page_outcome == "no_catalogue_evidence" and self.observations:
            raise ValueError("page_outcome 'no_catalogue_evidence' cannot carry observations")
        return self


class _VisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    request_id: str | None = None


VISION_EVIDENCE_PROMPT = """Extract only verbatim catalogue evidence from this source.

Identify each visually distinct catalogue row or product line. Do not interpret,
translate, normalize, calculate, split variants, infer product fields, or remove
duplicates. Preserve repeated rows at their separate source locations.

Return one JSON object with exactly this shape:
{
  "page_outcome": "evidence",
  "observations": [
    {
      "raw_text": "the complete row exactly as printed, or null",
      "raw_cells": [
        {
          "cell_reference": null,
          "row_number": null,
          "column_name": "the printed column heading, or null",
          "column_index": 1,
          "raw_value": "the cell value exactly as printed"
        }
      ],
      "bounding_box": {
        "x": 0,
        "y": 0,
        "width": 1,
        "height": 1,
        "unit": "px"
      },
      "confidence": "0.95"
    }
  ]
}

page_outcome is REQUIRED and must be exactly one of:
- "evidence" — the page contains catalogue rows; observations must list every
  visible row without omission.
- "no_catalogue_evidence" — the page genuinely contains no catalogue rows
  (blank page, cover page, pure artwork); observations must be [].

Use strings for confidence values. Omit no visible catalogue row. Use null for
unknown optional values. Return only the JSON object, without Markdown fences.
"""


def extract_evidence(content: bytes, filename: str, content_type: str) -> ExtractionResult:
    """Extract verbatim observations without performing semantic product parsing."""

    source_kind, source_format = _classify_source(filename, content_type)
    if not content:
        return _failed_result(
            source_format,
            code="EMPTY_SOURCE",
            message="Source content is empty",
        )
    if source_kind == "xlsx":
        return _extract_spreadsheet(content)
    if source_kind == "xls":
        return _failed_result(
            source_format,
            code="UNSUPPORTED_LEGACY_XLS",
            message="Legacy .xls files are not supported by the configured spreadsheet reader",
        )
    if source_kind == "csv":
        return _extract_csv(content)
    if source_kind == "pdf":
        return _extract_pdf(content)
    if source_kind in {"jpeg", "png"}:
        media_type = "image/png" if source_kind == "png" else "image/jpeg"
        return _extract_image(content, media_type=media_type, source_format=source_format)
    if source_kind == "text":
        return _extract_text(content, source_format=source_format)
    return _failed_result(
        source_format,
        code="UNSUPPORTED_SOURCE_FORMAT",
        message=f"Unsupported catalogue source type for {Path(filename).name or 'upload'}",
    )


def _extract_spreadsheet(content: bytes) -> ExtractionResult:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except Exception:
        return _failed_result(
            SourceFormat.SPREADSHEET,
            code="MALFORMED_SPREADSHEET",
            message="Spreadsheet source could not be read",
        )

    observations: list[ExtractedEvidence] = []
    warnings: list[str] = []
    errors: list[ExtractionError] = []
    completed = 0
    sheets = tuple(workbook.worksheets)
    try:
        for sheet in sheets:
            observed_rows = 0
            header: dict[int, str | None] = {}
            try:
                for row in sheet.iter_rows():
                    non_empty = [cell for cell in row if cell.value is not None and str(cell.value).strip()]
                    if not non_empty:
                        continue
                    observed_rows += 1
                    if not header:
                        header = {cell.column: (str(cell.value).strip() or None) for cell in non_empty}
                    first_column = min(cell.column for cell in non_empty)
                    last_column = max(cell.column for cell in non_empty)
                    row_number = non_empty[0].row
                    cell_range = (
                        f"{get_column_letter(first_column)}{row_number}:"
                        f"{get_column_letter(last_column)}{row_number}"
                    )
                    key = f"sheet:{sheet.title}:row:{row_number}"
                    raw_cells = tuple(
                        RawCell(
                            cell_reference=cell.coordinate,
                            row_number=cell.row,
                            column_index=cell.column,
                            column_name=header.get(cell.column),
                            raw_value=cell.value,
                        )
                        for cell in row
                    )
                    observations.append(
                        ExtractedEvidence(
                            observation_key=key,
                            source_location=SourceLocation(
                                sheet_name=sheet.title,
                                row_number=row_number,
                                cell_range=cell_range,
                                source_object_key=key,
                            ),
                            raw_cells=raw_cells,
                            extraction_method=ExtractionMethod.SPREADSHEET_CELL,
                            provider="openpyxl",
                            provider_version=openpyxl.__version__,
                        )
                    )
            except Exception:
                errors.append(
                    ExtractionError(
                        code="SPREADSHEET_SHEET_READ_ERROR",
                        message="One spreadsheet sheet could not be read completely",
                        unit_key=f"sheet:{sheet.title}",
                        provider="openpyxl",
                    )
                )
                continue
            completed += 1
            if not observed_rows:
                warnings.append(f"sheet {sheet.title!r} contained no non-empty rows")
    finally:
        workbook.close()

    return _build_result(
        SourceFormat.SPREADSHEET,
        observations=observations,
        units_attempted=len(sheets),
        units_completed=completed,
        warnings=warnings,
        errors=errors,
    )


def _header_labels(rows: list[list[str]]) -> dict[int, str | None]:
    """First non-empty row's values as column labels, keyed by 1-based column index.

    Structural inference only (no semantics): lets the supplier contract map
    each data cell by its column heading. The header row itself is recognised
    and skipped downstream (its cells repeat the contract's source columns).
    """

    for row in rows:
        if any(str(value).strip() for value in row):
            return {index: (str(value).strip() or None) for index, value in enumerate(row, start=1)}
    return {}


def _extract_csv(content: bytes) -> ExtractionResult:
    try:
        text, encoding = _decode_delimited_text(content)
    except UnicodeDecodeError:
        return _failed_result(
            SourceFormat.CSV,
            code="UNSUPPORTED_TEXT_ENCODING",
            message="CSV source is not valid UTF-8 or Big5 text",
        )
    try:
        dialect = _sniff_dialect(text)
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        return _failed_result(
            SourceFormat.CSV,
            code="MALFORMED_CSV",
            message="CSV source could not be parsed",
        )

    header = _header_labels(rows)
    observations: list[ExtractedEvidence] = []
    for row_number, row in enumerate(rows, start=1):
        if not any(value.strip() for value in row):
            continue
        key = f"csv:row:{row_number}"
        last_column = max(1, len(row))
        observations.append(
            ExtractedEvidence(
                observation_key=key,
                source_location=SourceLocation(
                    row_number=row_number,
                    cell_range=f"A{row_number}:{get_column_letter(last_column)}{row_number}",
                    source_object_key=key,
                ),
                raw_cells=tuple(
                    RawCell(
                        cell_reference=f"{get_column_letter(column_index)}{row_number}",
                        row_number=row_number,
                        column_index=column_index,
                        column_name=header.get(column_index),
                        raw_value=value,
                    )
                    for column_index, value in enumerate(row, start=1)
                ),
                extraction_method=ExtractionMethod.SPREADSHEET_CELL,
                provider="python-csv",
                provider_version=platform.python_version(),
                source_metadata={"encoding": encoding, "delimiter": dialect.delimiter},
            )
        )

    return _build_result(
        SourceFormat.CSV,
        observations=observations,
        units_attempted=len(rows),
        units_completed=len(rows),
    )


def _extract_text(content: bytes, *, source_format: SourceFormat) -> ExtractionResult:
    try:
        text, encoding = _decode_delimited_text(content)
    except UnicodeDecodeError:
        return _failed_result(
            source_format,
            code="UNSUPPORTED_TEXT_ENCODING",
            message="Text source is not valid UTF-8 or Big5 text",
        )
    lines = text.splitlines()
    observations = [
        ExtractedEvidence(
            observation_key=f"text:line:{line_number}",
            source_location=SourceLocation(
                source_object_key=f"text:line:{line_number}",
            ),
            raw_text=line,
            extraction_method=ExtractionMethod.OTHER,
            provider="python-text",
            provider_version=platform.python_version(),
            source_metadata={"encoding": encoding, "line_number": line_number},
        )
        for line_number, line in enumerate(lines, start=1)
        if line.strip()
    ]
    return _build_result(
        source_format,
        observations=observations,
        units_attempted=len(lines),
        units_completed=len(lines),
    )


def _extract_pdf(content: bytes) -> ExtractionResult:
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception:
        return _failed_result(
            SourceFormat.PDF,
            code="MALFORMED_PDF",
            message="PDF source could not be read",
        )

    observations: list[ExtractedEvidence] = []
    warnings: list[str] = []
    errors: list[ExtractionError] = []
    completed = 0
    empty_units = 0
    for page_number, page in enumerate(reader.pages, start=1):
        page_key = f"page:{page_number}"
        # Every catalogue page is extracted via the vision provider so tables
        # yield column-labeled cells the supplier contract can map
        # deterministically. The PDF text layer is not used as evidence: it
        # linearizes multi-column tables into per-cell lines the contract
        # cannot map to fields.
        if not _gemini_api_key():
            errors.append(
                ExtractionError(
                    code="EXTRACTION_CONFIGURATION_ERROR",
                    message="PDF evidence extraction requires a configured vision provider",
                    unit_key=page_key,
                    provider="google",
                )
            )
            continue
        try:
            page_content = _single_page_pdf_bytes(page)
            response = _call_gemini_vision(
                page_content,
                media_type="application/pdf",
            )
            vision_observations, page_outcome = _vision_observations(
                response,
                extraction_method=ExtractionMethod.MODEL_VISION,
                unit_key=page_key,
                page_number=page_number,
            )
            if page_outcome == "no_catalogue_evidence":
                warnings.append(
                    f"page {page_number}: provider classified page as containing no catalogue evidence"
                )
                empty_units += 1
            observations.extend(vision_observations)
            completed += 1
        except _VisionExtractionFailure as exc:
            errors.append(
                ExtractionError(
                    code=exc.code,
                    message=exc.public_message,
                    unit_key=page_key,
                    provider="google",
                    retryable=exc.retryable,
                )
            )
        except Exception:
            errors.append(
                ExtractionError(
                    code="SOURCE_PAGE_READ_ERROR",
                    message="PDF page could not be prepared for vision extraction",
                    unit_key=page_key,
                    provider="pypdf",
                )
            )
    if not len(reader.pages):
        warnings.append("PDF contained no pages")
    return _build_result(
        SourceFormat.PDF,
        observations=observations,
        units_attempted=len(reader.pages),
        units_completed=completed,
        empty_units=empty_units,
        warnings=warnings,
        errors=errors,
    )


def _extract_image(
    content: bytes,
    *,
    media_type: str,
    source_format: SourceFormat,
) -> ExtractionResult:
    if not _gemini_api_key():
        return _failed_result(
            source_format,
            code="EXTRACTION_CONFIGURATION_ERROR",
            message="Image evidence extraction requires a configured vision provider",
            unit_key="image:1",
            provider="google",
        )
    try:
        response = _call_gemini_vision(content, media_type=media_type)
        observations, page_outcome = _vision_observations(
            response,
            extraction_method=ExtractionMethod.MODEL_VISION,
            unit_key="image:1",
            page_number=None,
        )
    except _VisionExtractionFailure as exc:
        return _failed_result(
            source_format,
            code=exc.code,
            message=exc.public_message,
            unit_key="image:1",
            provider="google",
            retryable=exc.retryable,
            units_attempted=1,
        )
    warnings: list[str] = []
    empty_units = 0
    if page_outcome == "no_catalogue_evidence":
        warnings.append("image:1: provider classified image as containing no catalogue evidence")
        empty_units = 1
    return _build_result(
        source_format,
        observations=observations,
        units_attempted=1,
        units_completed=1,
        empty_units=empty_units,
        warnings=warnings,
    )


def _pdf_text_observations(text: str, *, page_number: int) -> list[ExtractedEvidence]:
    observations: list[ExtractedEvidence] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        key = f"page:{page_number}:line:{line_number}"
        observations.append(
            ExtractedEvidence(
                observation_key=key,
                source_location=SourceLocation(
                    page_number=page_number,
                    source_object_key=key,
                ),
                raw_text=line,
                extraction_method=ExtractionMethod.PDF_TEXT,
                provider="pypdf",
                provider_version=pypdf.__version__,
            )
        )
    return observations


def _vision_observations(
    response: _VisionResponse,
    *,
    extraction_method: ExtractionMethod,
    unit_key: str,
    page_number: int | None,
) -> tuple[list[ExtractedEvidence], Literal["evidence", "no_catalogue_evidence"]]:
    """Validate the typed provider envelope and mint stable observation keys.

    Identity policy: vision observation keys are derived from the observed
    CONTENT plus SOURCE LOCATION (sha256 of raw_text/raw_cells/bounding box),
    with a per-digest occurrence ordinal — so identical evidence keeps the
    same identity across provider retries regardless of response ORDER, and
    legitimate duplicate rows remain distinct (different bounding boxes yield
    different digests; byte-identical duplicates get interchangeable
    ordinals). Materially changed provider output yields different digests
    and is surfaced downstream as an idempotency conflict rather than
    silently corrupting persisted evidence.
    """

    try:
        payload = _strict_json_object(response.text)
        envelope = _VisionEnvelope.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _VisionExtractionFailure(
            code="MALFORMED_PROVIDER_RESPONSE",
            public_message="Vision provider returned an invalid evidence envelope",
            retryable=False,
        ) from exc

    if envelope.page_outcome == "no_catalogue_evidence":
        return [], "no_catalogue_evidence"

    try:
        observations: list[ExtractedEvidence] = []
        digest_counts: dict[str, int] = {}
        for item in envelope.observations:
            digest = _observation_digest(item)
            ordinal = digest_counts.get(digest, 0) + 1
            digest_counts[digest] = ordinal
            key = f"{unit_key}:obs:{digest}:{ordinal}"
            observations.append(
                ExtractedEvidence(
                    observation_key=key,
                    source_location=SourceLocation(
                        page_number=page_number,
                        bounding_box=item.bounding_box,
                        source_object_key=key,
                    ),
                    raw_text=item.raw_text,
                    raw_cells=item.raw_cells,
                    extraction_method=extraction_method,
                    provider="google",
                    provider_request_id=response.request_id,
                    model=GEMINI_MODEL,
                    model_version=GEMINI_MODEL,
                    confidence=item.confidence,
                )
            )
    except ValueError as exc:
        raise _VisionExtractionFailure(
            code="MALFORMED_PROVIDER_RESPONSE",
            public_message="Vision provider returned invalid source evidence",
            retryable=False,
        ) from exc
    return observations, "evidence"


def _observation_digest(item: _VisionObservation) -> str:
    material = {
        "raw_text": item.raw_text,
        "raw_cells": [
            [cell.cell_reference, cell.row_number, cell.column_name, cell.column_index, str(cell.raw_value)]
            for cell in item.raw_cells
        ],
        "bounding_box": item.bounding_box.model_dump(mode="json") if item.bounding_box else None,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _call_gemini_vision(content: bytes, *, media_type: str) -> _VisionResponse:
    """Vision/OCR provider seam (Gemini).

    The ONLY place a vision provider client is constructed. Sends the source
    page/image inline (PDF or image bytes) with the verbatim-evidence prompt
    and returns the raw text envelope. The SDK import is function-level so the
    provider stays behind this seam and never loads at module import.
    """

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_gemini_api_key())
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=content, mime_type=media_type),
                VISION_EVIDENCE_PROMPT,
            ],
            config=types.GenerateContentConfig(max_output_tokens=MAX_VISION_TOKENS),
        )
        try:
            text = response.text
        except Exception:
            text = None
        if not text or not text.strip():
            raise _VisionExtractionFailure(
                code="MALFORMED_PROVIDER_RESPONSE",
                public_message="Vision provider returned no text response",
                retryable=False,
            )
        return _VisionResponse(text=text, request_id=getattr(response, "response_id", None))
    except _VisionExtractionFailure:
        raise
    except Exception as exc:
        raise _classify_provider_failure(exc) from exc


def _classify_provider_failure(exc: Exception) -> "_VisionExtractionFailure":
    """Retry classification for the Gemini provider seam.

    google-genai API errors carry the HTTP status on ``.code``. 408/409/429
    and 5xx are retryable; 401/403 are configuration errors (never retried as
    transient); other 4xx are non-retryable provider errors. Network-level
    failures (timeouts, connection resets) carry no status and fall back to a
    conservative message heuristic. Raw provider details are never propagated
    to the client — messages stay sanitized.
    """

    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status < 600:
        if status in {408, 409, 429} or status >= 500:
            return _VisionExtractionFailure(
                code="TRANSIENT_PROVIDER_ERROR",
                public_message="Vision provider failed temporarily",
                retryable=True,
            )
        if status in {401, 403}:
            return _VisionExtractionFailure(
                code="EXTRACTION_CONFIGURATION_ERROR",
                public_message="Vision provider rejected the configured credentials",
                retryable=False,
            )
        return _VisionExtractionFailure(
            code="PROVIDER_ERROR",
            public_message="Vision provider could not extract source evidence",
            retryable=False,
        )
    retryable = _looks_transient(exc)
    return _VisionExtractionFailure(
        code="TRANSIENT_PROVIDER_ERROR" if retryable else "PROVIDER_ERROR",
        public_message=(
            "Vision provider failed temporarily"
            if retryable
            else "Vision provider could not extract source evidence"
        ),
        retryable=retryable,
    )


class _VisionExtractionFailure(Exception):
    def __init__(self, *, code: str, public_message: str, retryable: bool):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable


def _single_page_pdf_bytes(page) -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _strict_json_object(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    value = json.loads(stripped, parse_float=Decimal)
    if not isinstance(value, dict):
        raise ValueError("provider response must be one JSON object")
    return value


def _build_result(
    source_format: SourceFormat,
    *,
    observations: list[ExtractedEvidence],
    units_attempted: int,
    units_completed: int,
    empty_units: int = 0,
    warnings: list[str] | None = None,
    errors: list[ExtractionError] | None = None,
) -> ExtractionResult:
    warnings = list(warnings or [])
    errors = list(errors or [])
    # No observations, no explicit empty-unit classification and no recorded
    # errors can never read as success — that combination is NO_EVIDENCE.
    if not observations and not empty_units and not errors:
        errors.append(
            ExtractionError(
                code="NO_EVIDENCE",
                message="Extraction completed without any non-empty source observations",
            )
        )
    observed_or_empty = bool(observations) or empty_units > 0
    if observed_or_empty and errors:
        status = ExtractionStatus.PARTIAL
    elif observed_or_empty:
        status = ExtractionStatus.COMPLETE
    else:
        status = ExtractionStatus.FAILED
    return ExtractionResult(
        status=status,
        source_format=source_format,
        observations=tuple(observations),
        units_attempted=units_attempted,
        units_completed=units_completed,
        empty_units=empty_units,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _failed_result(
    source_format: SourceFormat,
    *,
    code: str,
    message: str,
    unit_key: str | None = None,
    provider: str | None = None,
    retryable: bool = False,
    units_attempted: int = 0,
) -> ExtractionResult:
    return ExtractionResult(
        status=ExtractionStatus.FAILED,
        source_format=source_format,
        observations=(),
        units_attempted=units_attempted,
        units_completed=0,
        errors=(
            ExtractionError(
                code=code,
                message=message,
                unit_key=unit_key,
                provider=provider,
                retryable=retryable,
            ),
        ),
    )


def _decode_delimited_text(content: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "big5"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("catalogue", content, 0, len(content), "unsupported text encoding")


def _sniff_dialect(text: str):
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _classify_source(filename: str, content_type: str) -> tuple[str, SourceFormat]:
    suffix = Path(filename or "").suffix.lower()
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    if suffix == ".xls":
        return "xls", SourceFormat.SPREADSHEET
    if suffix == ".xlsx":
        return "xlsx", SourceFormat.SPREADSHEET
    if suffix in {".csv", ".tsv"}:
        return "csv", SourceFormat.CSV
    if suffix == ".pdf":
        return "pdf", SourceFormat.PDF
    if suffix in {".jpg", ".jpeg"}:
        return "jpeg", SourceFormat.IMAGE
    if suffix == ".png":
        return "png", SourceFormat.IMAGE
    if suffix == ".txt":
        return "text", SourceFormat.TEXT
    if normalized_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return "xlsx", SourceFormat.SPREADSHEET
    if normalized_type == "application/vnd.ms-excel":
        return "xls", SourceFormat.SPREADSHEET
    if normalized_type in {"text/csv", "application/csv"}:
        return "csv", SourceFormat.CSV
    if normalized_type == "application/pdf":
        return "pdf", SourceFormat.PDF
    if normalized_type == "image/jpeg":
        return "jpeg", SourceFormat.IMAGE
    if normalized_type == "image/png":
        return "png", SourceFormat.IMAGE
    if normalized_type.startswith("text/"):
        return "text", SourceFormat.TEXT
    return "unknown", SourceFormat.OTHER


def _pdf_text_is_reliable(text: str) -> bool:
    chars = [character for character in text if not character.isspace()]
    if not chars:
        return False

    def expected(character: str) -> bool:
        codepoint = ord(character)
        return (
            0x20 <= codepoint <= 0x7E
            or 0x00A0 <= codepoint <= 0x00FF
            or 0x2000 <= codepoint <= 0x206F
            or 0x3000 <= codepoint <= 0x303F
            or 0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xFF00 <= codepoint <= 0xFFEF
        )

    suspicious = sum(1 for character in chars if not expected(character))
    return Decimal(suspicious) / Decimal(len(chars)) < Decimal("0.12")


# ── PDF page extraction policy (document-level, never semantic) ──────────────
# A page is classified by text QUALITY plus a STRUCTURAL image summary.
# Text-line count alone can never suppress vision: if material (or
# unknown-coverage) visual content may hold uncaptured evidence, vision runs.
#
#   no text                                   -> VISION      (NO_TEXT)
#   garbled text                              -> VISION      (UNRELIABLE_TEXT)
#   reliable text, no/decorative-only images  -> TEXT        (RELIABLE_TEXT_NO_MATERIAL_IMAGES)
#   reliable text + material image(s)         -> HYBRID      (TEXT_WITH_MATERIAL_IMAGES or
#                                                             SPARSE_TEXT_WITH_MATERIAL_IMAGES)
#   reliable text + undeterminable coverage   -> UNCERTAIN   (IMAGE_COVERAGE_UNKNOWN, vision runs)
#
# Structural metrics only — pixel dimensions of image XObjects, never decoded
# content, never business-field detection. Rendered coverage requires the
# content-stream CTM, which is deliberately not parsed: mid-size or
# dimensionless images are UNCERTAIN and vision runs rather than risking
# silent evidence loss. Vision does not guarantee absolute completeness; the
# decision, reason code and metrics are recorded honestly. Hybrid pages keep
# BOTH evidence sets (distinguishable by method/key/location, no dedup here);
# overlap resolution is interpretation's job downstream.

_MIN_MEANINGFUL_TEXT_LINES = 3
_DECORATIVE_MAX_DIMENSION_PX = 200      # small logos / icons
_MATERIAL_MIN_PIXEL_AREA = 250_000      # ~500x500+: scans, photographed tables

_NOISE_LINE_PATTERNS = (
    re.compile(r"^\d{1,4}$"),                                   # bare page number
    re.compile(r"^page\s*\d+(\s*(of|/)\s*\d+)?$", re.IGNORECASE),
    re.compile(r"^[-–—•·.*_=~|]+$"),                            # ruler / separator noise
    re.compile(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}$"),           # bare date footer
)


class PdfPageMode(str, Enum):
    TEXT = "text"
    VISION = "vision"
    HYBRID = "hybrid"
    UNCERTAIN = "uncertain"


class _PageImageSummary:
    __slots__ = ("image_count", "decorative_count", "material_count", "unknown_count", "largest_pixel_area", "unreadable")

    def __init__(self):
        self.image_count = 0
        self.decorative_count = 0
        self.material_count = 0
        self.unknown_count = 0
        self.largest_pixel_area = 0
        self.unreadable = False


class _PageDecision:
    __slots__ = ("mode", "keep_text", "vision_required", "reason", "metrics")

    def __init__(self, mode: PdfPageMode, keep_text: bool, vision_required: bool, reason: str, metrics: dict | None = None):
        self.mode = mode
        self.keep_text = keep_text
        self.vision_required = vision_required
        self.reason = reason
        self.metrics = metrics or {}


def _classify_pdf_page(page, page_text: str) -> _PageDecision:
    has_text = bool(page_text.strip())
    if not has_text:
        return _PageDecision(PdfPageMode.VISION, keep_text=False, vision_required=True, reason="NO_TEXT")
    if not _pdf_text_is_reliable(page_text):
        return _PageDecision(PdfPageMode.VISION, keep_text=False, vision_required=True, reason="UNRELIABLE_TEXT")

    images = _summarize_page_images(page)
    meaningful = _meaningful_line_count(page_text)
    metrics = {
        "meaningful_lines": meaningful,
        "image_count": images.image_count,
        "decorative_images": images.decorative_count,
        "material_images": images.material_count,
        "unknown_images": images.unknown_count,
        "largest_pixel_area": images.largest_pixel_area,
    }
    if images.unreadable or images.unknown_count:
        return _PageDecision(
            PdfPageMode.UNCERTAIN, keep_text=True, vision_required=True,
            reason="IMAGE_COVERAGE_UNKNOWN", metrics=metrics,
        )
    if images.material_count:
        reason = (
            "TEXT_WITH_MATERIAL_IMAGES"
            if meaningful >= _MIN_MEANINGFUL_TEXT_LINES
            else "SPARSE_TEXT_WITH_MATERIAL_IMAGES"
        )
        return _PageDecision(PdfPageMode.HYBRID, keep_text=True, vision_required=True, reason=reason, metrics=metrics)
    return _PageDecision(
        PdfPageMode.TEXT, keep_text=True, vision_required=False,
        reason="RELIABLE_TEXT_NO_MATERIAL_IMAGES", metrics=metrics,
    )


def _summarize_page_images(page) -> _PageImageSummary:
    """Structural image summary — XObject pixel dimensions only, no decoding.

    An image is decorative when both dimensions are small (logo/icon scale),
    material when its pixel area is scan/photo scale, and unknown when its
    dimensions are missing or in between (rendered coverage cannot be
    determined without parsing the content-stream CTM).
    """

    summary = _PageImageSummary()
    try:
        resources = page.get("/Resources")
        if resources is None:
            return summary
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            return summary
        xobjects = xobjects.get_object()
        for name in xobjects:
            candidate = xobjects[name].get_object()
            if candidate.get("/Subtype") != "/Image":
                continue
            summary.image_count += 1
            try:
                width = int(candidate["/Width"])
                height = int(candidate["/Height"])
            except Exception:
                summary.unknown_count += 1
                continue
            area = width * height
            summary.largest_pixel_area = max(summary.largest_pixel_area, area)
            if width <= _DECORATIVE_MAX_DIMENSION_PX and height <= _DECORATIVE_MAX_DIMENSION_PX:
                summary.decorative_count += 1
            elif area >= _MATERIAL_MIN_PIXEL_AREA:
                summary.material_count += 1
            else:
                summary.unknown_count += 1
    except Exception:
        summary.unreadable = True
    return summary


def _decision_warning(page_number: int, decision: _PageDecision) -> str | None:
    if decision.mode == PdfPageMode.HYBRID:
        return f"page {page_number}: {decision.reason} — using text and vision (hybrid)"
    if decision.mode == PdfPageMode.UNCERTAIN:
        return f"page {page_number}: {decision.reason} — image coverage undeterminable; using text and vision"
    if decision.reason == "UNRELIABLE_TEXT":
        return f"page {page_number}: text layer unreliable; using vision"
    return None


def _meaningful_line_count(text: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return sum(1 for line in lines if not _is_noise_line(line))


def _is_noise_line(line: str) -> bool:
    if len(line) < 4:
        return True
    return any(pattern.match(line) for pattern in _NOISE_LINE_PATTERNS)


def _gemini_api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def _looks_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "rate limit",
            "overloaded",
            "temporar",
            "connection",
            "503",
            "529",
        )
    )


__all__ = [
    "ExtractedEvidence",
    "ExtractionError",
    "ExtractionResult",
    "ExtractionStatus",
    "extract_evidence",
]
