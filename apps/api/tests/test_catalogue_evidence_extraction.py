"""Focused tests for the typed extraction boundary immediately before Raw."""

from __future__ import annotations

import io
import json
from decimal import Decimal

import openpyxl
import pypdf
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from orchestration.catalogue_stage_adapter import raw_input_from_extracted_evidence
from schemas.catalogue_pipeline.enums import ExtractionMethod, SourceFormat
from services import catalogue_evidence_extraction as evidence_service
from services import catalogue_evidence_extraction
from services.catalogue_evidence_extraction import ExtractionStatus


def test_pdf_text_extraction_is_verbatim_source_located_and_keeps_duplicates(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    content = _pdf_with_pages(
        [
            "SKU | Description | Wholesale\n"
            "10447 | Healthy Cuisine Chicken 82g | HK$13.10\n"
            "10447 | Healthy Cuisine Chicken 82g | HK$13.10"
        ]
    )

    result = catalogue_evidence_extraction.extract_evidence(content, "hills.pdf", "application/pdf")

    assert result.status == ExtractionStatus.COMPLETE
    assert result.source_format == SourceFormat.PDF
    assert result.units_attempted == result.units_completed == 1
    assert [item.raw_text for item in result.observations] == [
        "SKU | Description | Wholesale",
        "10447 | Healthy Cuisine Chicken 82g | HK$13.10",
        "10447 | Healthy Cuisine Chicken 82g | HK$13.10",
    ]
    assert result.observations[1].observation_key == "page:1:line:2"
    assert result.observations[1].source_location.page_number == 1
    assert result.observations[1].extraction_method == ExtractionMethod.PDF_TEXT
    assert result.observations[1].provider == "pypdf"
    assert "cost_price" not in result.observations[1].model_dump()


def test_pdf_reports_partial_extraction_when_one_page_needs_unconfigured_vision(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    content = _pdf_with_pages(["10447 | Product | HK$13.10", None])

    result = catalogue_evidence_extraction.extract_evidence(content, "mixed.pdf", "application/pdf")

    assert result.status == ExtractionStatus.PARTIAL
    assert result.units_attempted == 2
    assert result.units_completed == 1
    assert len(result.observations) == 1
    assert result.errors[0].code == "EXTRACTION_CONFIGURATION_ERROR"
    assert result.errors[0].unit_key == "page:2"
    assert result.errors[0].provider == "google"


def test_scanned_pdf_failure_is_operational_error_not_fake_catalogue_row(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)

    result = catalogue_evidence_extraction.extract_evidence(
        _pdf_with_pages([None]),
        "scan.pdf",
        "application/pdf",
    )

    assert result.status == ExtractionStatus.FAILED
    assert result.observations == ()
    assert result.errors[0].code == "EXTRACTION_CONFIGURATION_ERROR"


def test_spreadsheet_preserves_all_sheets_rows_cells_formulas_and_duplicates():
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "Price List"
    first.append(["Code", "Description", "Wholesale"])
    first.append(["10447", "Healthy Cuisine 82g", "=10+3.1"])
    first.append(["10447", "Healthy Cuisine 82g", "=10+3.1"])
    second = workbook.create_sheet("Terms")
    second.append(["MOQ", "6 bottles"])
    output = io.BytesIO()
    workbook.save(output)

    result = catalogue_evidence_extraction.extract_evidence(
        output.getvalue(),
        "catalogue.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert result.status == ExtractionStatus.COMPLETE
    assert result.units_attempted == result.units_completed == 2
    assert [item.observation_key for item in result.observations] == [
        "sheet:Price List:row:1",
        "sheet:Price List:row:2",
        "sheet:Price List:row:3",
        "sheet:Terms:row:1",
    ]
    assert result.observations[1].source_location.cell_range == "A2:C2"
    assert result.observations[1].raw_cells[2].cell_reference == "C2"
    assert result.observations[1].raw_cells[2].raw_value == "=10+3.1"
    assert [cell.raw_value for cell in result.observations[2].raw_cells] == [
        cell.raw_value for cell in result.observations[1].raw_cells
    ]
    assert result.observations[3].source_location.sheet_name == "Terms"


def test_csv_preserves_coordinates_raw_values_empty_cells_and_duplicate_rows():
    content = (
        "\ufeffCode,Description,Wholesale,Notes\r\n"
        "10447,Healthy Cuisine 82g,HK$13.10,\r\n"
        "10447,Healthy Cuisine 82g,HK$13.10,\r\n"
    ).encode()

    result = catalogue_evidence_extraction.extract_evidence(content, "catalogue.csv", "text/csv")

    assert result.status == ExtractionStatus.COMPLETE
    assert result.source_format == SourceFormat.CSV
    assert result.units_attempted == result.units_completed == 3
    assert len(result.observations) == 3
    second = result.observations[1]
    assert second.source_location.row_number == 2
    assert second.source_location.cell_range == "A2:D2"
    assert second.raw_cells[2].raw_value == "HK$13.10"
    assert second.raw_cells[3].raw_value == ""
    assert [cell.raw_value for cell in result.observations[2].raw_cells] == [
        cell.raw_value for cell in second.raw_cells
    ]
    assert second.source_metadata == {"encoding": "utf-8-sig", "delimiter": ","}


def test_vision_extraction_records_actual_provider_metadata_and_png_media_type(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    called: dict[str, str] = {}

    def fake_vision(content: bytes, *, media_type: str):
        called["media_type"] = media_type
        assert content == b"png-bytes"
        return evidence_service._VisionResponse(
            text=json.dumps(
                {
                    "page_outcome": "evidence",
                    "observations": [
                        {
                            "raw_text": "ALF-10 | Syringe 10ml | HK$12.50",
                            "raw_cells": [],
                            "bounding_box": {
                                "x": 10,
                                "y": 20,
                                "width": 200,
                                "height": 24,
                                "unit": "px",
                            },
                            "confidence": "0.91",
                        }
                    ]
                }
            ),
            request_id="msg_test_123",
        )

    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)

    result = catalogue_evidence_extraction.extract_evidence(b"png-bytes", "catalogue.png", "image/png")

    assert result.status == ExtractionStatus.COMPLETE
    assert called["media_type"] == "image/png"
    observation = result.observations[0]
    assert observation.extraction_method == ExtractionMethod.MODEL_VISION
    assert observation.provider == "google"
    assert observation.provider_request_id == "msg_test_123"
    assert observation.model == evidence_service.GEMINI_MODEL
    assert observation.confidence == Decimal("0.91")
    assert observation.source_location.bounding_box.width == Decimal("200")


def test_vision_response_rejects_semantic_product_fields(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")

    def fake_vision(_content: bytes, *, media_type: str):
        assert media_type == "image/jpeg"
        return evidence_service._VisionResponse(
            text=json.dumps(
                {
                    "page_outcome": "evidence",
                    "observations": [
                        {
                            "raw_text": "10447 | Product | HK$13.10",
                            "raw_cells": [],
                            "bounding_box": None,
                            "confidence": "0.9",
                            "cost_price": 13.1,
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)

    result = catalogue_evidence_extraction.extract_evidence(b"jpeg-bytes", "catalogue.jpg", "image/jpeg")

    assert result.status == ExtractionStatus.FAILED
    assert result.observations == ()
    assert result.errors[0].code == "MALFORMED_PROVIDER_RESPONSE"


def test_vision_response_rejects_normalized_numeric_raw_cells(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")

    def fake_vision(_content: bytes, *, media_type: str):
        assert media_type == "image/jpeg"
        return evidence_service._VisionResponse(
            text=json.dumps(
                {
                    "page_outcome": "evidence",
                    "observations": [
                        {
                            "raw_text": None,
                            "raw_cells": [
                                {
                                    "cell_reference": None,
                                    "row_number": None,
                                    "column_name": "Wholesale",
                                    "column_index": 1,
                                    "raw_value": 13.1,
                                }
                            ],
                            "bounding_box": None,
                            "confidence": "0.9",
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)

    result = catalogue_evidence_extraction.extract_evidence(b"jpeg-bytes", "catalogue.jpg", "image/jpeg")

    assert result.status == ExtractionStatus.FAILED
    assert result.errors[0].code == "MALFORMED_PROVIDER_RESPONSE"


def test_one_extracted_evidence_maps_to_one_raw_input_without_semantic_mutation():
    result = catalogue_evidence_extraction.extract_evidence(
        b"Code,Description,Wholesale\n10447,Healthy Cuisine 82g,HK$13.10\n",
        "catalogue.csv",
        "text/csv",
    )
    evidence = result.observations[1]

    raw_input = raw_input_from_extracted_evidence(evidence)

    assert raw_input.idempotency_key == evidence.observation_key
    assert raw_input.source_location == evidence.source_location
    assert raw_input.raw_text is None
    assert raw_input.raw_cells == evidence.raw_cells
    assert raw_input.extraction_method == ExtractionMethod.SPREADSHEET_CELL
    assert raw_input.source_metadata["observation_key"] == evidence.observation_key
    assert not hasattr(raw_input, "cost_price")


def test_empty_unknown_and_legacy_xls_sources_fail_explicitly():
    empty = catalogue_evidence_extraction.extract_evidence(b"", "empty.csv", "text/csv")
    unknown = catalogue_evidence_extraction.extract_evidence(b"data", "catalogue.bin", "application/octet-stream")
    legacy_xls = catalogue_evidence_extraction.extract_evidence(b"data", "catalogue.xls", "application/vnd.ms-excel")
    mislabeled_csv = catalogue_evidence_extraction.extract_evidence(
        b"Code,Description\n10447,Product\n",
        "catalogue.csv",
        "application/vnd.ms-excel",
    )

    assert empty.status == ExtractionStatus.FAILED
    assert empty.errors[0].code == "EMPTY_SOURCE"
    assert unknown.status == ExtractionStatus.FAILED
    assert unknown.errors[0].code == "UNSUPPORTED_SOURCE_FORMAT"
    assert legacy_xls.status == ExtractionStatus.FAILED
    assert legacy_xls.errors[0].code == "UNSUPPORTED_LEGACY_XLS"
    assert mislabeled_csv.status == ExtractionStatus.COMPLETE
    assert mislabeled_csv.source_format == SourceFormat.CSV


def _pdf_with_pages(page_texts: list[str | None]) -> bytes:
    writer = pypdf.PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            _write_text_to_page(writer, page, text)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _write_text_to_page(writer: pypdf.PdfWriter, page, text: str) -> None:
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    parts = ["BT", "/F1 10 Tf", "36 750 Td", "14 TL"]
    for line in text.splitlines():
        parts.append(f"({_escape_pdf_text(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


# ── Page extraction policy (Fix 1) + typed vision outcomes (Fix 2) ──────────

from pypdf.generic import NumberObject  # noqa: E402


def _add_image_xobject(writer: pypdf.PdfWriter, page, *, width: int = 1700, height: int = 2200) -> None:
    image = DecodedStreamObject()
    image.set_data(b"\x00")
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width),
            NameObject("/Height"): NumberObject(height),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    reference = writer._add_object(image)
    resources = page.get(NameObject("/Resources"))
    if resources is None:
        resources = DictionaryObject()
        page[NameObject("/Resources")] = resources
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): reference})


def _pdf_pages(pages: list[dict]) -> bytes:
    """Build a PDF from page specs: {"text": str | None, "image": bool}."""

    writer = pypdf.PdfWriter()
    for spec in pages:
        page = writer.add_blank_page(width=612, height=792)
        if spec.get("text"):
            _write_text_to_page(writer, page, spec["text"])
        if spec.get("image"):
            _add_image_xobject(writer, page, width=spec.get("image_width", 1700), height=spec.get("image_height", 2200))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _vision_stub(payloads_by_call: list[dict]):
    calls = {"count": 0}

    def fake_vision(_content: bytes, *, media_type: str):
        index = min(calls["count"], len(payloads_by_call) - 1)
        calls["count"] += 1
        return evidence_service._VisionResponse(
            text=json.dumps(payloads_by_call[index]), request_id=f"msg_{calls['count']}"
        )

    return fake_vision, calls


_EVIDENCE_PAYLOAD = {
    "page_outcome": "evidence",
    "observations": [
        {
            "raw_text": "SCANNED-1 | Scanned Product 500g | HK$99.00",
            "raw_cells": [],
            "bounding_box": {"x": 5, "y": 40, "width": 300, "height": 20, "unit": "px"},
            "confidence": "0.9",
        }
    ],
}


def test_hybrid_page_with_only_page_number_still_uses_vision(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, calls = _vision_stub([_EVIDENCE_PAYLOAD])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages([{"text": "3", "image": True}])

    result = catalogue_evidence_extraction.extract_evidence(content, "hybrid.pdf", "application/pdf")

    assert calls["count"] == 1, "sparse incidental text must not suppress vision"
    assert result.status == ExtractionStatus.COMPLETE
    vision_texts = [o.raw_text for o in result.observations if o.extraction_method == ExtractionMethod.MODEL_VISION]
    assert vision_texts == ["SCANNED-1 | Scanned Product 500g | HK$99.00"]
    text_lines = [o.raw_text for o in result.observations if o.extraction_method == ExtractionMethod.PDF_TEXT]
    assert text_lines == ["3"]  # incidental text is still verbatim evidence
    assert any("hybrid" in warning for warning in result.warnings)


def test_hybrid_page_with_short_footer_is_not_silently_completed_without_vision(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    content = _pdf_pages([{"text": "Page 1 of 3", "image": True}])

    result = catalogue_evidence_extraction.extract_evidence(content, "footer.pdf", "application/pdf")

    # The footer is kept as verbatim evidence, but the image-bearing page must
    # NOT be marked completed without vision — so the page surfaces as
    # incomplete (units_completed == 0) with a config error, never as a
    # silently completed page missing its image-based rows.
    assert result.status == ExtractionStatus.PARTIAL
    assert result.units_completed == 0
    assert result.errors[0].code == "EXTRACTION_CONFIGURATION_ERROR"
    assert result.errors[0].unit_key == "page:1"
    assert [o.raw_text for o in result.observations] == ["Page 1 of 3"]


def test_short_but_meaningful_text_page_without_images_stays_text_only(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")

    def forbidden_vision(*_a, **_k):
        raise AssertionError("text-only page must not call vision")

    monkeypatch.setattr(evidence_service, "_call_gemini_vision", forbidden_vision)
    content = _pdf_pages([{"text": "10447 Healthy Cuisine Chicken 82g HK$13.10", "image": False}])

    result = catalogue_evidence_extraction.extract_evidence(content, "short.pdf", "application/pdf")

    assert result.status == ExtractionStatus.COMPLETE
    assert [o.raw_text for o in result.observations] == ["10447 Healthy Cuisine Chicken 82g HK$13.10"]


def test_garbled_or_unreliable_text_layer_is_classified_for_vision():
    # A text layer dominated by unexpected code points is unreliable and must
    # route to vision with the text discarded — tested directly on the page
    # policy because a synthetic PDF writer cannot reliably round-trip a
    # genuinely garbled glyph stream.
    garbled = chr(0x0450) * 40  # Cyrillic block, outside the expected ranges
    assert evidence_service._pdf_text_is_reliable(garbled) is False
    decision = evidence_service._classify_pdf_page(None, garbled)
    assert decision.keep_text is False
    assert decision.vision_required is True

    empty_decision = evidence_service._classify_pdf_page(None, "   \n  ")
    assert empty_decision.keep_text is False
    assert empty_decision.vision_required is True


def test_multi_page_mixture_of_text_scanned_and_hybrid_pages(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, calls = _vision_stub([_EVIDENCE_PAYLOAD])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages(
        [
            {"text": "SKU | Description | Price\nA-1 | First Product | HK$1.00\nA-2 | Second Product | HK$2.00"},
            {"text": None, "image": True},
            {"text": "7", "image": True},
        ]
    )

    result = catalogue_evidence_extraction.extract_evidence(content, "mixture.pdf", "application/pdf")

    assert result.status == ExtractionStatus.COMPLETE
    assert result.units_attempted == result.units_completed == 3
    assert calls["count"] == 2  # scanned + hybrid pages
    by_method: dict = {}
    for observation in result.observations:
        by_method[observation.extraction_method] = by_method.get(observation.extraction_method, 0) + 1
    assert by_method[ExtractionMethod.PDF_TEXT] == 4  # 3 lines + incidental "7"
    assert by_method[ExtractionMethod.MODEL_VISION] == 2


def test_empty_vision_array_without_outcome_fails_the_page(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, _ = _vision_stub([{"observations": []}])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages([{"text": None, "image": True}])

    result = catalogue_evidence_extraction.extract_evidence(content, "empty.pdf", "application/pdf")

    assert result.status == ExtractionStatus.FAILED
    assert result.units_completed == 0
    assert result.errors[0].code == "MALFORMED_PROVIDER_RESPONSE"
    assert result.errors[0].unit_key == "page:1"


def test_evidence_outcome_with_empty_array_is_malformed_not_empty_page(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, _ = _vision_stub([{"page_outcome": "evidence", "observations": []}])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)

    result = catalogue_evidence_extraction.extract_evidence(b"jpeg-bytes", "catalogue.jpg", "image/jpeg")

    assert result.status == ExtractionStatus.FAILED
    assert result.errors[0].code == "MALFORMED_PROVIDER_RESPONSE"


def test_one_empty_vision_page_in_multipage_pdf_is_partial_not_complete(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, _ = _vision_stub([{"observations": []}])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages(
        [
            {"text": "SKU | Description | Price\nB-1 | Product One | HK$5.00\nB-2 | Product Two | HK$6.00"},
            {"text": None, "image": True},
        ]
    )

    result = catalogue_evidence_extraction.extract_evidence(content, "partial.pdf", "application/pdf")

    assert result.status == ExtractionStatus.PARTIAL
    assert result.units_attempted == 2
    assert result.units_completed == 1
    assert result.errors[0].unit_key == "page:2"


def test_explicit_no_catalogue_evidence_page_is_accounted_without_fake_observations(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, _ = _vision_stub([{"page_outcome": "no_catalogue_evidence", "observations": []}])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages([{"text": None, "image": True}])

    result = catalogue_evidence_extraction.extract_evidence(content, "cover.pdf", "application/pdf")

    assert result.status == ExtractionStatus.COMPLETE
    assert result.observations == ()
    assert result.units_attempted == result.units_completed == 1
    assert result.empty_units == 1
    assert any("no catalogue evidence" in warning for warning in result.warnings)


def test_no_catalogue_evidence_with_observations_is_malformed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    payload = {"page_outcome": "no_catalogue_evidence", "observations": _EVIDENCE_PAYLOAD["observations"]}
    fake_vision, _ = _vision_stub([payload])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)

    result = catalogue_evidence_extraction.extract_evidence(b"jpeg-bytes", "catalogue.jpg", "image/jpeg")

    assert result.status == ExtractionStatus.FAILED
    assert result.errors[0].code == "MALFORMED_PROVIDER_RESPONSE"


def test_vision_observation_identity_is_stable_across_reordered_retries():
    def _payload(rows):
        return json.dumps(
            {
                "page_outcome": "evidence",
                "observations": [
                    {"raw_text": row, "raw_cells": [], "bounding_box": None, "confidence": "0.9"} for row in rows
                ],
            }
        )

    rows = ["ROW-A | Product A | HK$1.00", "ROW-B | Product B | HK$2.00", "ROW-B | Product B | HK$2.00"]
    first = evidence_service._VisionResponse(text=_payload(rows), request_id="msg_a")
    reordered = evidence_service._VisionResponse(text=_payload(list(reversed(rows))), request_id="msg_b")

    observations_a, _ = evidence_service._vision_observations(
        first, extraction_method=ExtractionMethod.MODEL_VISION, unit_key="page:1", page_number=1
    )
    observations_b, _ = evidence_service._vision_observations(
        reordered, extraction_method=ExtractionMethod.MODEL_VISION, unit_key="page:1", page_number=1
    )

    keys_a = {o.observation_key for o in observations_a}
    keys_b = {o.observation_key for o in observations_b}
    assert keys_a == keys_b, "identical evidence must keep identical identities across reordered retries"
    assert len(keys_a) == 3  # the duplicate row keeps a distinct ordinal identity


def test_provider_failure_classification_reads_gemini_http_status():
    # google-genai API errors carry the HTTP status on `.code`; classification
    # keys off that (429/5xx retry, 401/403 config, other 4xx non-retryable).
    class _ApiError(Exception):
        def __init__(self, code):
            super().__init__(f"gemini {code}")
            self.code = code

    rate_limited = evidence_service._classify_provider_failure(_ApiError(429))
    assert rate_limited.retryable is True
    assert rate_limited.code == "TRANSIENT_PROVIDER_ERROR"

    server = evidence_service._classify_provider_failure(_ApiError(503))
    assert server.retryable is True

    unauthorized = evidence_service._classify_provider_failure(_ApiError(401))
    assert unauthorized.retryable is False
    assert unauthorized.code == "EXTRACTION_CONFIGURATION_ERROR"

    bad_request = evidence_service._classify_provider_failure(_ApiError(400))
    assert bad_request.retryable is False
    assert bad_request.code == "PROVIDER_ERROR"

    # Network-level failures have no HTTP status -> conservative message heuristic.
    timeout = evidence_service._classify_provider_failure(TimeoutError("deadline exceeded / connection timeout"))
    assert timeout.retryable is True


# ── Stage 3 architectural boundary: extraction must not reach interpretation
#    or any later stage; Anthropic is allowed only in the provider seam. ─────

import ast  # noqa: E402
from pathlib import Path  # noqa: E402


def _import_closure(seed_modules: list[str]) -> set[str]:
    backend_root = Path(__file__).resolve().parent.parent

    def _local_path(module_name: str) -> Path | None:
        as_file = backend_root / (module_name.replace(".", "/") + ".py")
        if as_file.exists():
            return as_file
        as_package = backend_root / module_name.replace(".", "/") / "__init__.py"
        return as_package if as_package.exists() else None

    def _imports_of(path: Path, module_name: str) -> set[str]:
        package = module_name if path.name == "__init__.py" else module_name.rsplit(".", 1)[0]
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    names.add(f"{package}.{node.module}" if node.module else package)
                elif node.module:
                    names.add(node.module)
        return names

    visited: set[str] = set()
    queue = list(seed_modules)
    while queue:
        module_name = queue.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        path = _local_path(module_name)
        if path is None:
            continue
        queue.extend(_imports_of(path, module_name))
    return visited


def test_stage3_extraction_import_boundary():
    closure = _import_closure(
        [
            "services.catalogue_evidence_extraction",
            "orchestration.catalogue_extraction_adapter",
        ]
    )
    forbidden = {
        "services.catalogue_interpretation",       # Intermediate layer
        "services.extraction_service",             # legacy semantic extraction
        "services.catalogue_pipeline_stages",      # staging/validation/mastering/serving services
        "services.tagging_service",
        "services.sku_service",
        "services.pricing_service",
    }
    hits = forbidden & closure
    assert not hits, f"Stage 3 extraction closure reaches forbidden modules: {sorted(hits)}"


def test_vision_provider_is_reachable_only_through_the_stage3_provider_seam():
    # The extraction ENVELOPE/adapter must not import the vision provider (Gemini)
    # directly; the client lives behind the seam inside catalogue_evidence_extraction.
    adapter_closure = _import_closure(["orchestration.catalogue_extraction_adapter"])
    assert "services.catalogue_evidence_extraction" in adapter_closure

    backend_root = Path(__file__).resolve().parent.parent
    seam = (backend_root / "services" / "catalogue_evidence_extraction.py").read_text()
    tree = ast.parse(seam)
    # No AI provider is imported at module top level — the client (google.genai)
    # loads lazily inside the vision function only.
    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert "google" not in top_level, "vision provider client must stay behind the function-level seam"
    assert "anthropic" not in top_level


# ── Fix 1 follow-up: typed page modes, decorative vs material images ────────

from services.catalogue_evidence_extraction import PdfPageMode  # noqa: E402


def _first_page_decision(content: bytes):
    reader = pypdf.PdfReader(io.BytesIO(content))
    page = reader.pages[0]
    return evidence_service._classify_pdf_page(page, page.extract_text() or "")


_RICH_TEXT = (
    "Supplier Catalogue 2026\n"
    "Wholesale Price List Terms and Conditions\n"
    "All prices quoted exclude delivery charges\n"
    "Contact your account manager for volume enquiries"
)


def test_text_page_with_meaningful_lines_and_no_images_is_text_mode(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")

    def forbidden_vision(*_a, **_k):
        raise AssertionError("text-mode page must not call vision")

    monkeypatch.setattr(evidence_service, "_call_gemini_vision", forbidden_vision)
    content = _pdf_pages([{"text": _RICH_TEXT}])

    decision = _first_page_decision(content)
    assert decision.mode == PdfPageMode.TEXT
    assert decision.reason == "RELIABLE_TEXT_NO_MATERIAL_IMAGES"
    assert decision.vision_required is False

    result = catalogue_evidence_extraction.extract_evidence(content, "text.pdf", "application/pdf")
    assert result.status == ExtractionStatus.COMPLETE


def test_text_page_with_tiny_decorative_logo_stays_text_only(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")

    def forbidden_vision(*_a, **_k):
        raise AssertionError("decorative logo must not force vision on a complete text page")

    monkeypatch.setattr(evidence_service, "_call_gemini_vision", forbidden_vision)
    content = _pdf_pages([{"text": _RICH_TEXT, "image": True, "image_width": 64, "image_height": 64}])

    decision = _first_page_decision(content)
    assert decision.mode == PdfPageMode.TEXT
    assert decision.reason == "RELIABLE_TEXT_NO_MATERIAL_IMAGES"
    assert decision.metrics["decorative_images"] == 1
    assert decision.metrics["material_images"] == 0

    result = catalogue_evidence_extraction.extract_evidence(content, "logo.pdf", "application/pdf")
    assert result.status == ExtractionStatus.COMPLETE
    assert all(o.extraction_method == ExtractionMethod.PDF_TEXT for o in result.observations)


def test_rich_text_page_with_material_image_is_hybrid_and_calls_vision(monkeypatch):
    # Title + column header + footer + full-page image table: three or more
    # valid text lines must NOT mark the page complete from text alone.
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, calls = _vision_stub([_EVIDENCE_PAYLOAD])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages([{"text": _RICH_TEXT, "image": True}])  # default: material scan-size image

    decision = _first_page_decision(content)
    assert decision.mode == PdfPageMode.HYBRID
    assert decision.reason == "TEXT_WITH_MATERIAL_IMAGES"
    assert decision.keep_text is True and decision.vision_required is True

    result = catalogue_evidence_extraction.extract_evidence(content, "hybrid-rich.pdf", "application/pdf")

    assert calls["count"] == 1, "three text lines must not suppress vision on a material-image page"
    assert result.status == ExtractionStatus.COMPLETE
    methods = {o.extraction_method for o in result.observations}
    assert methods == {ExtractionMethod.PDF_TEXT, ExtractionMethod.MODEL_VISION}
    assert any("hybrid" in warning for warning in result.warnings)


def test_rich_text_page_with_material_image_cannot_complete_without_vision(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    content = _pdf_pages([{"text": _RICH_TEXT, "image": True}])

    result = catalogue_evidence_extraction.extract_evidence(content, "hybrid-nokey.pdf", "application/pdf")

    assert result.status == ExtractionStatus.PARTIAL
    assert result.units_completed == 0
    assert result.errors[0].code == "EXTRACTION_CONFIGURATION_ERROR"
    # Text evidence is retained, but the page is honestly not complete.
    assert [o.raw_text for o in result.observations] == _RICH_TEXT.splitlines()


def test_image_with_unknown_dimensions_is_uncertain_and_requires_vision(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    # Build a page whose image XObject has no /Width//Height: coverage unknowable.
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    _write_text_to_page(writer, page, _RICH_TEXT)
    image = DecodedStreamObject()
    image.set_data(b"\x00")
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
        }
    )
    reference = writer._add_object(image)
    resources = page.get(NameObject("/Resources"))
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): reference})
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()

    decision = _first_page_decision(content)
    assert decision.mode == PdfPageMode.UNCERTAIN
    assert decision.reason == "IMAGE_COVERAGE_UNKNOWN"
    assert decision.vision_required is True

    result = catalogue_evidence_extraction.extract_evidence(content, "unknown.pdf", "application/pdf")
    # Three meaningful text lines cannot silently complete the page.
    assert result.status != ExtractionStatus.COMPLETE
    assert result.units_completed == 0


def test_mid_size_image_is_treated_as_coverage_unknown(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    content = _pdf_pages([{"text": _RICH_TEXT, "image": True, "image_width": 450, "image_height": 450}])

    decision = _first_page_decision(content)
    assert decision.mode == PdfPageMode.UNCERTAIN
    assert decision.reason == "IMAGE_COVERAGE_UNKNOWN"


def test_mixed_document_decorative_hybrid_and_scanned_pages(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")
    fake_vision, calls = _vision_stub([_EVIDENCE_PAYLOAD])
    monkeypatch.setattr(evidence_service, "_call_gemini_vision", fake_vision)
    content = _pdf_pages(
        [
            {"text": _RICH_TEXT},                                                        # text-only
            {"text": _RICH_TEXT, "image": True, "image_width": 64, "image_height": 64},  # decorative logo
            {"text": _RICH_TEXT, "image": True},                                         # hybrid (material)
            {"text": None, "image": True},                                               # scanned
        ]
    )

    result = catalogue_evidence_extraction.extract_evidence(content, "mixed-modes.pdf", "application/pdf")

    assert calls["count"] == 2  # hybrid + scanned only
    assert result.status == ExtractionStatus.COMPLETE
    assert result.units_attempted == result.units_completed == 4
    vision_count = sum(1 for o in result.observations if o.extraction_method == ExtractionMethod.MODEL_VISION)
    text_count = sum(1 for o in result.observations if o.extraction_method == ExtractionMethod.PDF_TEXT)
    assert vision_count == 2
    assert text_count == 12  # 4 rich-text lines on each of three text-bearing pages
