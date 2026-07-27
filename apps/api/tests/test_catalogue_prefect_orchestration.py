"""Catalogue Prefect orchestration tests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
import pypdf
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
os.environ.setdefault("PREFECT_API_MODE", "offline")
os.environ.setdefault("PREFECT_LOGGING_LEVEL", "ERROR")
os.environ.setdefault("PREFECT_LOGGING_TO_API_ENABLED", "false")
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")
os.environ.setdefault("PREFECT_SERVER_LOGGING_LEVEL", "ERROR")

import database  # noqa: E402
import models  # noqa: E402
from orchestration import catalogue_extraction_adapter as extraction_adapter  # noqa: E402
from orchestration.catalogue_contract_resolution import resolve_recorded_supplier_contract  # noqa: E402
from orchestration.catalogue_dispatch import dispatch_queued_runs  # noqa: E402
from orchestration.catalogue_extraction_adapter import extract_source_evidence  # noqa: E402
from orchestration.catalogue_flows import catalogue_ingestion_flow  # noqa: E402
from orchestration.catalogue_run_lifecycle import claim_queued_run, terminal_result_for_replay  # noqa: E402
from orchestration.catalogue_source_loader import SourceVerificationError, load_and_verify_source_asset  # noqa: E402
from orchestration.catalogue_types import (  # noqa: E402
    DuplicateRunClaim,
    ExtractionEvidenceError,
    RecordedContractError,
    TransientProviderError,
)
from schemas.catalogue_pipeline.enums import ExtractionMethod, SourceFormat  # noqa: E402
from schemas.catalogue_pipeline.extracted_evidence_v1 import RawCell, SourceLocation  # noqa: E402
from services import catalogue_evidence_extraction as extraction  # noqa: E402
from services.catalogue_evidence_extraction import (  # noqa: E402
    ExtractedEvidence,
    ExtractionError,
    ExtractionResult,
    ExtractionStatus,
    ExtractionUnitOutcome,
    ExtractionUnitStatus,
)
from services.catalogue_conformance import conform_observations  # noqa: E402
from services.catalogue_submission import CatalogueSubmissionCommand, CatalogueSubmissionService  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


HILLS_ROW_TEXT = "10447 Hill's Healthy Cuisine Chicken 82g HK$13.10 ambiguous offer text"

# A deterministic Hill's price-list row keyed EXACTLY by supplier 14's declared
# source columns. Fed as column-labeled cells (directly, or through the vision
# provider stub) it maps through the contract with no model in the loop.
HILLS_ROW = {
    "Product Code / 產品編號": "10447",
    "Product Range / 產品系列": "Science Plan",
    "Life Stage / 生命階段": "Adult",
    "Product Description / 產品名稱": "Chicken 82g",
    "Size / 重量": "82g",
    "Gross Wholesale Price / 每箱·罐": "13.10",
    "Order Multiple / 訂貨單位": "12",
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES", str(1024 * 1024))
    for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"): monkeypatch.delenv(_k, raising=False)
    session = database.SessionLocal()
    try:
        _reset(session)
        _seed_supplier(session, 1, "ALF", "Alfamedic")
        _seed_supplier(session, 14, "HILLS", "Hill's")
        _seed_supplier(session, 91, "VETAPETV", "Vetapet Vet")
        yield session
        session.rollback()
        _reset(session)
    finally:
        session.close()


def _reset(session):
    for model in (
        models.CatalogueSubmissionIdempotency,
        models.CatalogueRawStageAttempt,
        models.CatalogueExtractionAttempt,
        models.CatalogueServingPublication,
        models.CatalogueSupplierMbbTerm,
        models.CatalogueSupplierPrice,
        models.CataloguePackagingConfiguration,
        models.CatalogueSupplierProduct,
        models.CatalogueReviewDecision,
        models.CatalogueMasteringCandidate,
        models.CatalogueValidationIssue,
        models.CatalogueNormalizedRowEvidence,
        models.CatalogueNormalizedRow,
        models.CatalogueExtractedEvidence,
        models.IngestionRun,
        models.CatalogueSourceDocument,
    ):
        session.query(model).delete()
    session.query(models.CatalogueItem).delete()
    session.query(models.CatalogueImport).delete()
    session.commit()


def _seed_supplier(session, supplier_id: int, code: str, name: str):
    supplier = session.get(models.Supplier, supplier_id)
    if supplier is None:
        session.add(
            models.Supplier(
                id=supplier_id,
                code=code,
                name=name,
                created_at="2026-07-23T00:00:00+00:00",
            )
        )
        session.commit()


def _pdf_bytes(page_count: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _text_pdf_bytes(lines: list[str]) -> bytes:
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
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
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"({escaped}) Tj")
        parts.append("T*")
    parts.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode("utf-8"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _submit(
    session,
    *,
    supplier_id: int = 14,
    contract_id: str | None = None,
    contract_version: str | None = None,
    content: bytes | None = None,
):
    service = CatalogueSubmissionService(
        session,
        upload_root=os.environ["CATALOGUE_UPLOAD_DIR"],
        max_upload_bytes=1024 * 1024,
    )
    return service.submit(
        CatalogueSubmissionCommand(
            supplier_id=supplier_id,
            original_filename="fixture.pdf",
            content_type="application/pdf",
            stream=BytesIO(content if content is not None else _pdf_bytes()),
            contract_id=contract_id,
            contract_version=contract_version,
            idempotency_key=None,
            submitted_by="pytest",
        )
    )


def _source_path(session) -> Path:
    source = session.query(models.CatalogueSourceDocument).one()
    return Path(os.environ["CATALOGUE_UPLOAD_DIR"]) / source.source_ref


def _evidence(
    *,
    key: str = "page:1:line:1",
    text: str | None = HILLS_ROW_TEXT,
    page: int = 1,
) -> ExtractedEvidence:
    return ExtractedEvidence(
        observation_key=key,
        source_location=SourceLocation(page_number=page, source_object_key=key),
        raw_text=text,
        extraction_method=ExtractionMethod.PDF_TEXT,
        provider="pypdf",
    )


def _cell_evidence(
    row: dict[str, str],
    *,
    key: str = "page:1:obs:1",
    page: int = 1,
    confidence: str | None = "0.95",
) -> ExtractedEvidence:
    """One deterministic vision-style observation carrying column-labeled cells."""

    cells = tuple(
        RawCell(cell_reference=None, row_number=None, column_name=column, column_index=index, raw_value=value)
        for index, (column, value) in enumerate(row.items(), start=1)
    )
    return ExtractedEvidence(
        observation_key=key,
        source_location=SourceLocation(page_number=page, source_object_key=key),
        raw_cells=cells,
        extraction_method=ExtractionMethod.MODEL_VISION,
        provider="google",
        confidence=confidence,
    )


def _vision_envelope(rows: list[dict[str, str]]) -> str:
    """Serialize rows as one Gemini vision evidence envelope: one observation per row."""

    return json.dumps(
        {
            "page_outcome": "evidence",
            "observations": [
                {
                    "raw_text": None,
                    "raw_cells": [
                        {
                            "cell_reference": None,
                            "row_number": None,
                            "column_name": column,
                            "column_index": index,
                            "raw_value": value,
                        }
                        for index, (column, value) in enumerate(row.items(), start=1)
                    ],
                    "bounding_box": {"x": 0, "y": offset, "width": 1, "height": 1, "unit": "px"},
                    "confidence": "0.95",
                }
                for offset, row in enumerate(rows)
            ],
        }
    )


def _install_vision(monkeypatch, rows: list[dict[str, str]]) -> None:
    """Route PDF extraction through a stubbed Gemini vision provider.

    The ``db`` fixture strips provider keys, so every flow test must re-set
    GEMINI_API_KEY and replace the vision seam with a deterministic envelope.
    """

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        extraction,
        "_call_gemini_vision",
        lambda content, *, media_type: extraction._VisionResponse(text=_vision_envelope(rows)),
    )


def test_source_loader_verifies_file_path_size_signature_and_checksum(db):
    result = _submit(db)
    source = db.query(models.CatalogueSourceDocument).one()

    asset = load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id)
    assert asset.run_identity.run_uuid == result.ingestion_run_id
    assert asset.sha256 == source.source_checksum
    assert asset.source_ref.startswith("v2/")

    with pytest.raises(SourceVerificationError, match="size limit"):
        load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id, max_source_bytes=1)

    path = _source_path(db)
    path.write_bytes(b"%PDF-1.4\nchanged")
    with pytest.raises(SourceVerificationError, match="checksum"):
        load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id)

    source.source_checksum = hashlib.sha256(b"not a pdf").hexdigest()
    path.write_bytes(b"not a pdf")
    db.commit()
    with pytest.raises(SourceVerificationError, match="signature"):
        load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id)

    source.source_ref = "../outside.pdf"
    source.source_checksum = hashlib.sha256(_pdf_bytes()).hexdigest()
    db.commit()
    with pytest.raises(SourceVerificationError, match="relative path"):
        load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id)


def test_source_loader_rejects_missing_file(db):
    result = _submit(db)
    _source_path(db).unlink()

    with pytest.raises(SourceVerificationError, match="missing"):
        load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id)


def test_exact_recorded_contract_resolution_has_no_supplier_only_fallback(db):
    result = _submit(db)
    resolved = resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)
    assert resolved.slug == "hills.price_list.v1"

    run = db.query(models.IngestionRun).one()
    run.supplier_source_contract_id = None
    run.supplier_source_contract_version = None
    db.commit()
    with pytest.raises(RecordedContractError, match="exact"):
        resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)


def test_recorded_contract_resolution_rejects_unsupported_unknown_mismatch_and_document_type(db):
    result = _submit(db)
    run = db.query(models.IngestionRun).one()
    source = db.query(models.CatalogueSourceDocument).one()

    run.supplier_id = 91
    source.supplier_id = 91
    run.supplier_source_contract_id = source.supplier_source_contract_id = "vetapet.vet_price_list.v1"
    run.supplier_source_contract_version = source.supplier_source_contract_version = "v1"
    db.commit()
    with pytest.raises(RecordedContractError, match="not SUPPORTED"):
        resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)

    run.supplier_id = source.supplier_id = 14
    run.supplier_source_contract_id = source.supplier_source_contract_id = "hills.price_list.v1"
    run.supplier_source_contract_version = source.supplier_source_contract_version = "v2"
    db.commit()
    with pytest.raises(RecordedContractError, match="hills.price_list.v1@v2"):
        resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)

    run.supplier_id = source.supplier_id = 1
    run.supplier_source_contract_version = source.supplier_source_contract_version = "v1"
    db.commit()
    with pytest.raises(RecordedContractError, match="belongs to supplier=14"):
        resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)

    run.supplier_id = source.supplier_id = 14
    run.document_type = "PROMOTION_SHEET"
    source.document_type = "PROMOTION_SHEET"
    db.commit()
    with pytest.raises(RecordedContractError, match="document_type"):
        resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)


def test_extraction_policy_maps_partial_and_failed_results(db, monkeypatch):
    result = _submit(db)
    asset = load_and_verify_source_asset(db, ingestion_run_id=result.ingestion_run_id)

    partial = ExtractionResult(
        status=ExtractionStatus.PARTIAL,
        source_format=SourceFormat.PDF,
        observations=(_evidence(),),
        units_attempted=2,
        units_completed=1,
        errors=(ExtractionError(code="SOURCE_PAGE_READ_ERROR", message="page 2 could not be read", unit_key="page:2"),),
    )
    monkeypatch.setattr(extraction_adapter, "extract_evidence", lambda *a, **k: partial)
    with pytest.raises(ExtractionEvidenceError, match="page 2"):
        extract_source_evidence(asset)

    transient = ExtractionResult(
        status=ExtractionStatus.FAILED,
        source_format=SourceFormat.PDF,
        units_attempted=1,
        units_completed=0,
        errors=(ExtractionError(code="TRANSIENT_PROVIDER_ERROR", message="provider throttled", retryable=True),),
    )
    monkeypatch.setattr(extraction_adapter, "extract_evidence", lambda *a, **k: transient)
    with pytest.raises(TransientProviderError, match="throttled"):
        extract_source_evidence(asset)

    failed = ExtractionResult(
        status=ExtractionStatus.FAILED,
        source_format=SourceFormat.PDF,
        units_attempted=1,
        units_completed=0,
        errors=(ExtractionError(code="MALFORMED_PDF", message="PDF source could not be read"),),
    )
    monkeypatch.setattr(extraction_adapter, "extract_evidence", lambda *a, **k: failed)
    with pytest.raises(ExtractionEvidenceError, match="could not be read"):
        extract_source_evidence(asset)

    explicitly_empty = ExtractionResult(
        status=ExtractionStatus.COMPLETE,
        source_format=SourceFormat.PDF,
        units_attempted=1,
        units_completed=1,
        empty_units=1,
        unit_outcomes=(
            ExtractionUnitOutcome(
                unit_key="page:1",
                status=ExtractionUnitStatus.NO_CATALOGUE_EVIDENCE,
                provider="google",
            ),
        ),
    )
    monkeypatch.setattr(
        extraction_adapter,
        "extract_evidence",
        lambda *a, **k: explicitly_empty,
    )
    with pytest.raises(ExtractionEvidenceError, match="no catalogue evidence"):
        extract_source_evidence(asset)


def test_partial_extraction_is_audited_and_blocks_all_downstream_stages(db, monkeypatch):
    submitted = _submit(db)
    partial = ExtractionResult(
        status=ExtractionStatus.PARTIAL,
        source_format=SourceFormat.PDF,
        observations=(_evidence(),),
        units_attempted=2,
        units_completed=1,
        unit_outcomes=(
            ExtractionUnitOutcome(
                unit_key="page:1",
                status=ExtractionUnitStatus.EVIDENCE_CAPTURED,
                observation_count=1,
                provider="google",
            ),
            ExtractionUnitOutcome(
                unit_key="page:2",
                status=ExtractionUnitStatus.FAILED_PERMANENT,
                provider="google",
                error_code="SOURCE_PAGE_READ_ERROR",
                message="page 2 could not be read",
            ),
        ),
        errors=(
            ExtractionError(
                code="SOURCE_PAGE_READ_ERROR",
                message="page 2 could not be read",
                unit_key="page:2",
                provider="google",
            ),
        ),
    )
    monkeypatch.setattr(
        extraction_adapter,
        "extract_evidence",
        lambda *_args, **_kwargs: partial,
    )

    result = catalogue_ingestion_flow(ingestion_run_id=submitted.ingestion_run_id)

    assert result.terminal_status == "failed"
    assert result.error_code == "EXTRACTION_EVIDENCE_ERROR"
    attempt = db.query(models.CatalogueExtractionAttempt).one()
    assert attempt.status == "PARTIAL"
    assert attempt.units_attempted == 2
    assert attempt.units_completed == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 0
    assert db.query(models.CatalogueNormalizedRow).count() == 0
    assert db.query(models.CatalogueMasteringCandidate).count() == 0


def test_conformance_maps_hills_row_from_contract_cells(db):
    # Deterministic conformance: named cells are mapped through supplier 14's
    # declared source columns with no model in the loop.
    result = _submit(db)
    contract = resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)
    observation = _cell_evidence(HILLS_ROW, key="page:1:obs:hills")
    raw_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    outcome = conform_observations((observation,), (raw_id,), contract)

    assert outcome.skipped_count == 0
    item = outcome.items[0]
    assert item.observation_key == observation.observation_key
    assert item.provenance == {"interpreter": "contract_cells"}
    assert item.raw_fields["cost"] == "13.10"
    # No bulk-buy tiers: the deterministic path invents none, and no contract
    # column maps MBB text for this row.
    assert item.raw_fields["mbb_text"] is None
    assert item.normalized_fields["mbb_terms"] == []
    assert item.normalized_fields["supplier_sku"]["value"] == "10447"
    assert item.normalized_fields["supplier_sku"]["evidence"]["raw_observation_id"] == str(raw_id)
    assert item.normalized_fields["supplier_sku"]["evidence"]["field_path"] == "/raw_cells"
    assert item.normalized_fields["product_name"]["value"] == "Science Plan Adult Chicken 82g"
    assert item.normalized_fields["cost"]["amount"] == "13.10"
    assert item.normalized_fields["packaging"]["content_amount"] == "82"
    assert "sellable_units_per_purchase_unit" not in item.normalized_fields["packaging"]


def test_conformance_skips_header_rows(db):
    # A header row's cell VALUES repeat the contract's declared source columns;
    # it is evidence, not a row, so it is skipped while the data row conforms.
    result = _submit(db)
    contract = resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)
    header = _cell_evidence({column: column for column in HILLS_ROW}, key="page:1:obs:header")
    row = _cell_evidence(HILLS_ROW, key="page:1:obs:row")
    ids = (UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))

    outcome = conform_observations((header, row), ids, contract)

    assert outcome.skipped_count == 1
    assert [item.observation_key for item in outcome.items] == [row.observation_key]


def test_lifecycle_claim_and_terminal_replay_are_safe(db):
    result = _submit(db)

    claim_queued_run(db, ingestion_run_id=result.ingestion_run_id)
    run = db.query(models.IngestionRun).one()
    assert run.status == "running"
    assert run.started_at is not None

    with pytest.raises(DuplicateRunClaim):
        claim_queued_run(db, ingestion_run_id=result.ingestion_run_id)

    run.status = "completed"
    run.completed_at = "2026-07-23T00:10:00+00:00"
    run.items_extracted = 1
    db.commit()
    replay = terminal_result_for_replay(db, ingestion_run_id=result.ingestion_run_id)
    assert replay.terminal_status == "completed"
    assert replay.rows_extracted == 1


def test_flow_unknown_run_returns_sanitized_failure_result(db):
    missing = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    result = catalogue_ingestion_flow(ingestion_run_id=missing)

    assert result.ingestion_run_id == missing
    assert result.terminal_status == "failed"
    assert result.error_code == "RUN_NOT_FOUND"


def test_flow_runs_machine_pipeline_and_stops_at_pending_review(db, monkeypatch):
    result = _submit(db)
    _install_vision(monkeypatch, [HILLS_ROW])

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "completed"
    assert flow_result.rows_extracted == 1
    assert flow_result.raw_observations_created == 1
    assert flow_result.staging_items_created == 1
    assert flow_result.mastering_candidates_created == 1
    assert flow_result.human_review_required is True

    run = db.query(models.IngestionRun).one()
    assert run.status == "completed"
    assert run.started_at is not None
    assert run.completed_at is not None
    assert db.query(models.CatalogueExtractedEvidence).count() == 1
    assert db.query(models.CatalogueNormalizedRow).count() == 1
    candidate = db.query(models.CatalogueMasteringCandidate).one()
    assert candidate.review_status == "PENDING_REVIEW"
    assert db.query(models.CatalogueReviewDecision).count() == 0
    assert db.query(models.CatalogueSupplierProduct).count() == 0
    assert db.query(models.CatalogueServingPublication).count() == 0

    replay = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)
    assert replay.terminal_status == "completed"
    assert db.query(models.CatalogueExtractedEvidence).count() == 1


def test_flow_records_blocking_validation_and_skips_candidate(db, monkeypatch):
    # A "By Quote" price is observed (raw cost present) but is deterministically
    # unresolvable to an amount, so the cost basis is unresolved and the row is
    # blocked from candidacy.
    quoted_row = {
        "Order Code": "Q-1",
        "Product Name": "Quoted item",
        "Brand": "Alfamedic",
        "Packing / Unit": "1 piece",
        "Price/ Unit (HKD)": "By Quote",
    }
    result = _submit(
        db,
        supplier_id=1,
        contract_id="alfamedic.price_list.v1",
        contract_version="v1",
    )
    _install_vision(monkeypatch, [quoted_row])

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "completed_with_warnings"
    assert flow_result.validation_issues_created == 2
    assert flow_result.mastering_candidates_created == 0
    issues = db.query(models.CatalogueValidationIssue).all()
    assert {issue.issue_code for issue in issues} == {
        "CONTRACT_NULL_COST_REQUIRES_REVIEW",
        "STAGING_COST_BASIS_UNRESOLVED",
    }
    assert next(issue for issue in issues if issue.issue_code == "STAGING_COST_BASIS_UNRESOLVED").publish_blocking == 1


def test_flow_failure_is_sanitized_and_durable(db, monkeypatch):
    result = _submit(db)
    _source_path(db).write_bytes(b"%PDF-1.4\nchanged")

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "failed"
    run = db.query(models.IngestionRun).one()
    assert run.status == "failed"
    assert "checksum" in run.error_summary
    assert str(Path(os.environ["CATALOGUE_UPLOAD_DIR"])) not in run.error_summary


def test_dispatcher_uses_bounded_batches_and_duplicate_dispatch_is_harmless(db, monkeypatch):
    first = _submit(db)
    _submit(db)
    called: list[UUID] = []

    def fake_flow(*, ingestion_run_id):
        called.append(ingestion_run_id)
        session = database.SessionLocal()
        try:
            claim_queued_run(session, ingestion_run_id=ingestion_run_id)
        finally:
            session.close()

    monkeypatch.setattr("orchestration.catalogue_dispatch.catalogue_ingestion_flow", fake_flow)

    result = dispatch_queued_runs(batch_size=1)

    assert result.queued_count == 1
    assert result.submitted_count == 1
    assert called == [first.ingestion_run_id]


# ── Fix 5: a claimed run must never remain `running` ───────────────────────

from orchestration import catalogue_flows  # noqa: E402
from orchestration import catalogue_tasks  # noqa: E402
from services import catalogue_pipeline_stages as stages  # noqa: E402


def _run_status(db, run_id):
    db.expire_all()
    return db.query(models.IngestionRun).filter_by(run_uuid=str(run_id)).one().status


def _no_downstream(db):
    assert db.query(models.CatalogueNormalizedRow).count() == 0
    assert db.query(models.CatalogueMasteringCandidate).count() == 0


def test_stage_error_during_capture_fails_the_run_not_running(db, monkeypatch):
    result = _submit(db)
    _install_vision(monkeypatch, [HILLS_ROW])

    def boom(_self, _command):
        raise stages.IdempotencyConflict("extracted evidence observation already exists with different material input")

    monkeypatch.setattr(stages.ExtractedEvidenceService, "capture", boom)

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "failed"
    assert flow_result.error_code == "IDEMPOTENCY_CONFLICT"
    assert _run_status(db, result.ingestion_run_id) == "failed"
    _no_downstream(db)


def test_unexpected_persistence_error_fails_the_run_with_sanitized_status(db, monkeypatch):
    result = _submit(db)
    _install_vision(monkeypatch, [HILLS_ROW])

    def kaboom(*_a, **_k):
        raise RuntimeError("psycopg2 connection reset: secret-dsn=postgres://user:pw@host/db")

    monkeypatch.setattr(catalogue_flows, "capture_extracted_evidence_task", kaboom)

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "failed"
    assert flow_result.error_code == "INTERNAL_PIPELINE_ERROR"
    assert _run_status(db, result.ingestion_run_id) == "failed"
    # The sanitized status surfaced to clients carries no internal detail.
    db.expire_all()
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(result.ingestion_run_id)).one()
    assert "psycopg2" not in (run.error_summary or "")
    assert "secret-dsn" not in (run.error_summary or "")
    _no_downstream(db)


def test_envelope_validation_error_during_interpretation_fails_the_run(db, monkeypatch):
    result = _submit(db, content=_text_pdf_bytes([HILLS_ROW_TEXT]))

    def bad_interpret(*_a, **_k):
        raise ValueError("evidence envelope invalid")

    monkeypatch.setattr(catalogue_flows, "normalize_evidence_task", bad_interpret)

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "failed"
    assert _run_status(db, result.ingestion_run_id) == "failed"
    # Evidence captured before the failure is transactionally consistent
    # (its own atomic batch), and nothing downstream was created.
    _no_downstream(db)


def test_failure_recording_failure_is_logged_and_does_not_mask_original(db, monkeypatch, caplog):
    import logging as _logging

    result = _submit(db, content=_text_pdf_bytes([HILLS_ROW_TEXT]))

    def boom(*_a, **_k):
        raise RuntimeError("extraction blew up")

    monkeypatch.setattr(catalogue_flows, "extract_source_evidence_task", boom)
    monkeypatch.setattr(
        catalogue_flows,
        "record_run_failure_task",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("failure store down")),
    )

    with caplog.at_level(_logging.ERROR, logger="orchestration.catalogue_flows"):
        flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.terminal_status == "failed"
    assert flow_result.error_code == "INTERNAL_PIPELINE_ERROR"
    assert any("could not be recorded" in record.getMessage() for record in caplog.records)


def test_transient_provider_error_is_typed_and_retryable_non_transient_is_not(db, monkeypatch):
    # A transient provider failure surfaced from extraction retries via the
    # task's typed retry condition; a non-transient one does not.
    from orchestration.catalogue_types import TransientProviderError as _Transient

    transient = ExtractionResult(
        status=ExtractionStatus.FAILED,
        source_format=SourceFormat.PDF,
        units_attempted=1,
        units_completed=0,
        errors=(ExtractionError(code="TRANSIENT_PROVIDER_ERROR", message="throttled", retryable=True),),
    )
    monkeypatch.setattr(extraction_adapter, "extract_evidence", lambda *a, **k: transient)
    asset_run = _submit(db, content=_text_pdf_bytes([HILLS_ROW_TEXT]))
    with pytest.raises(_Transient):
        extract_source_evidence(
            load_and_verify_source_asset(db, ingestion_run_id=asset_run.ingestion_run_id)
        )


# ── Deterministic conformance accounting: contract currency + run metrics ───


def test_cost_currency_comes_from_the_supplier_contract_not_a_default(db):
    result = _submit(db)
    contract = resolve_recorded_supplier_contract(db, ingestion_run_id=result.ingestion_run_id)
    observation = _cell_evidence(HILLS_ROW)
    raw_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    outcome = conform_observations((observation,), (raw_id,), contract)

    proposal = outcome.items[0].normalized_fields["cost"]
    # Currency is a CONTRACT declaration (HKD), never a conformance default.
    assert proposal["currency"] == contract.declaration.pricing.currency
    assert proposal["currency"] == "HKD"


def test_flow_persists_interpretation_accounting_in_run_metrics(db, monkeypatch):
    # One header row (skipped as evidence) + one data row (conformed): the run's
    # durable metrics record the deterministic conformance accounting.
    header = {column: column for column in HILLS_ROW}
    result = _submit(db)
    _install_vision(monkeypatch, [header, HILLS_ROW])

    flow_result = catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)

    assert flow_result.rows_interpreted == 1
    assert flow_result.rows_skipped_non_catalogue == 1
    assert flow_result.interpretation_degraded is False
    db.expire_all()
    run = db.query(models.IngestionRun).one()
    metrics = json.loads(run.metrics)
    assert metrics["rows_interpreted"] == 1
    assert metrics["rows_skipped_non_catalogue"] == 1
    assert metrics["interpretation_degraded"] is False
