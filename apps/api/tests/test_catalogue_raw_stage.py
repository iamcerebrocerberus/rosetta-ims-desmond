"""Raw-stage boundary tests.

The raw stage answers only: what exactly did the supplier send us?
These tests prove it preserves and describes the received file without ever
attempting to understand it — no AI client, OCR, extraction, text parsing,
interpretation or business-record persistence is reachable while it runs.
"""

from __future__ import annotations

import dataclasses
import hashlib
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

import anthropic  # noqa: E402
import database  # noqa: E402
import models  # noqa: E402
from orchestration.catalogue_extraction_adapter import extract_source_evidence  # noqa: E402
from orchestration.catalogue_flows import catalogue_ingestion_flow  # noqa: E402
from orchestration.catalogue_raw_stage import complete_raw_stage  # noqa: E402
from orchestration.catalogue_source_loader import load_and_verify_source_asset  # noqa: E402
from orchestration.catalogue_types import RawStageResult, SourceVerificationError  # noqa: E402
from services import catalogue_evidence_extraction  # noqa: E402
from services import catalogue_interpretation  # noqa: E402
from services import catalogue_pipeline_stages as stages  # noqa: E402
from services import extraction_service  # noqa: E402
from services.catalogue_submission import CatalogueSubmissionCommand, CatalogueSubmissionService  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES", str(1024 * 1024))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    session = database.SessionLocal()
    try:
        _reset(session)
        _seed_supplier(session, 14, "HILLS", "Hill's")
        yield session
        session.rollback()
        _reset(session)
    finally:
        session.close()


@pytest.fixture()
def forbid_understanding(monkeypatch):
    """Poison every operation that tries to understand the file's meaning."""

    def _forbidden(label):
        def _fail(*_a, **_k):
            pytest.fail(f"raw stage must not call {label}")

        return _fail

    monkeypatch.setattr(anthropic, "Anthropic", _forbidden("anthropic.Anthropic"))
    monkeypatch.setattr(catalogue_evidence_extraction, "extract_evidence", _forbidden("evidence extraction"))
    monkeypatch.setattr(catalogue_evidence_extraction, "_call_anthropic_vision", _forbidden("vision OCR"))
    monkeypatch.setattr(catalogue_interpretation, "interpret_observations", _forbidden("interpretation"))
    monkeypatch.setattr(catalogue_interpretation, "_model_interpret_rows", _forbidden("model interpretation"))
    monkeypatch.setattr(extraction_service, "extract", _forbidden("legacy extraction"))
    monkeypatch.setattr(pypdf.PageObject, "extract_text", _forbidden("PDF text extraction"))
    monkeypatch.setattr(stages.ExtractedEvidenceService, "capture", _forbidden("raw observation persistence"))
    monkeypatch.setattr(stages.InterpretedClaimService, "build_item", _forbidden("staging persistence"))
    monkeypatch.setattr(stages.MasteringService, "prepare_candidate", _forbidden("mastering persistence"))


def _reset(session):
    for model in (
        models.CatalogueSubmissionIdempotency,
        models.CatalogueRawStageAttempt,
        models.CatalogueServingPublication,
        models.CatalogueSupplierMbbTerm,
        models.CatalogueSupplierPrice,
        models.CataloguePackagingConfiguration,
        models.CatalogueSupplierProduct,
        models.CatalogueReviewDecision,
        models.CatalogueMasteringCandidate,
        models.CatalogueValidationIssue,
        models.CatalogueInterpretedClaimEvidence,
        models.CatalogueInterpretedClaim,
        models.CatalogueExtractedEvidence,
        models.IngestionRun,
        models.CatalogueSourceDocument,
    ):
        session.query(model).delete()
    session.query(models.CatalogueItem).delete()
    session.query(models.CatalogueImport).delete()
    session.commit()


def _seed_supplier(session, supplier_id: int, code: str, name: str):
    if session.get(models.Supplier, supplier_id) is None:
        session.add(models.Supplier(id=supplier_id, code=code, name=name, created_at="2026-07-24T00:00:00+00:00"))
        session.commit()


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


def _encrypted_pdf_bytes() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _submit(session, content: bytes):
    service = CatalogueSubmissionService(
        session,
        upload_root=os.environ["CATALOGUE_UPLOAD_DIR"],
        max_upload_bytes=1024 * 1024,
    )
    return service.submit(
        CatalogueSubmissionCommand(
            supplier_id=14,
            original_filename="fixture.pdf",
            content_type="application/pdf",
            stream=BytesIO(content),
            contract_id=None,
            contract_version=None,
            idempotency_key=None,
            submitted_by="pytest",
        )
    )


def _source_row(session, run_id: UUID) -> models.CatalogueSourceDocument:
    run = session.query(models.IngestionRun).filter_by(run_uuid=str(run_id)).one()
    return session.get(models.CatalogueSourceDocument, run.catalogue_source_document_id)


def _stored_path(session, run_id: UUID) -> Path:
    source = _source_row(session, run_id)
    return Path(os.environ["CATALOGUE_UPLOAD_DIR"]) / source.source_ref


def test_raw_stage_preserves_original_and_persists_metadata(db, forbid_understanding):
    content = _text_pdf_bytes(["10447 Healthy Cuisine Chicken 82g HK$13.10"])
    submitted = _submit(db, content)

    stored = _stored_path(db, submitted.ingestion_run_id)
    assert stored.read_bytes() == content, "stored object must be the exact received bytes"

    result = complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)

    assert isinstance(result, RawStageResult)
    assert result.status == "completed"
    assert result.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert result.byte_size == len(content)
    assert result.page_count == 1
    assert result.content_type == "application/pdf"
    assert result.original_filename == "fixture.pdf"
    assert result.source_ref.startswith("v2/")
    assert result.run_identity.run_uuid == submitted.ingestion_run_id
    assert result.catalogue_import_id == db.query(models.CatalogueImport).one().id

    # The result describes the file; it never carries the file or its meaning.
    field_names = {field.name for field in dataclasses.fields(result)}
    assert field_names == {
        "run_identity",
        "catalogue_import_id",
        "original_filename",
        "content_type",
        "byte_size",
        "checksum_sha256",
        "source_ref",
        "page_count",
        "received_at",
        "status",
    }

    db.expire_all()
    source = _source_row(db, submitted.ingestion_run_id)
    assert source.byte_size == len(content)
    assert source.page_count == 1
    assert source.raw_stage_status == "completed"
    assert source.raw_stage_completed_at is not None
    assert db.query(models.CatalogueExtractedEvidence).count() == 0
    assert db.query(models.CatalogueInterpretedClaim).count() == 0


def test_raw_stage_is_idempotent(db, forbid_understanding):
    submitted = _submit(db, _text_pdf_bytes(["row one"]))

    first = complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)
    second = complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)

    assert first == second
    assert db.query(models.CatalogueSourceDocument).count() == 1
    assert db.query(models.IngestionRun).count() == 1


def test_raw_stage_rejects_password_protected_pdf(db, forbid_understanding):
    submitted = _submit(db, _encrypted_pdf_bytes())

    with pytest.raises(SourceVerificationError, match="password protected"):
        complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)

    db.expire_all()
    assert _source_row(db, submitted.ingestion_run_id).raw_stage_status == "failed"


def test_raw_stage_rejects_corrupt_pdf_structure(db, forbid_understanding):
    submitted = _submit(db, _text_pdf_bytes(["row one"]))
    corrupt = b"%PDF-1.4\nnot actually a readable pdf"
    _stored_path(db, submitted.ingestion_run_id).write_bytes(corrupt)
    source = _source_row(db, submitted.ingestion_run_id)
    source.source_checksum = hashlib.sha256(corrupt).hexdigest()
    db.commit()

    with pytest.raises(SourceVerificationError, match="structure cannot be read"):
        complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)

    db.expire_all()
    assert _source_row(db, submitted.ingestion_run_id).raw_stage_status == "failed"


def test_raw_stage_failure_matrix_persists_failed_state(db, forbid_understanding):
    # Tampered bytes -> checksum mismatch.
    tampered = _submit(db, _text_pdf_bytes(["tampered"]))
    _stored_path(db, tampered.ingestion_run_id).write_bytes(b"%PDF-1.4\nchanged")
    with pytest.raises(SourceVerificationError, match="checksum"):
        complete_raw_stage(db, ingestion_run_id=tampered.ingestion_run_id)
    db.expire_all()
    assert _source_row(db, tampered.ingestion_run_id).raw_stage_status == "failed"

    # Emptied file.
    emptied = _submit(db, _text_pdf_bytes(["emptied"]))
    _stored_path(db, emptied.ingestion_run_id).write_bytes(b"")
    with pytest.raises(SourceVerificationError, match="empty"):
        complete_raw_stage(db, ingestion_run_id=emptied.ingestion_run_id)
    db.expire_all()
    assert _source_row(db, emptied.ingestion_run_id).raw_stage_status == "failed"

    # Missing file.
    missing = _submit(db, _text_pdf_bytes(["missing"]))
    _stored_path(db, missing.ingestion_run_id).unlink()
    with pytest.raises(SourceVerificationError, match="missing"):
        complete_raw_stage(db, ingestion_run_id=missing.ingestion_run_id)
    db.expire_all()
    assert _source_row(db, missing.ingestion_run_id).raw_stage_status == "failed"

    # Oversized file.
    oversized = _submit(db, _text_pdf_bytes(["oversized"]))
    with pytest.raises(SourceVerificationError, match="size limit"):
        complete_raw_stage(db, ingestion_run_id=oversized.ingestion_run_id, max_source_bytes=1)
    db.expire_all()
    assert _source_row(db, oversized.ingestion_run_id).raw_stage_status == "failed"


def test_flow_never_reaches_extraction_when_raw_stage_fails(db, monkeypatch):
    monkeypatch.setattr(
        catalogue_evidence_extraction,
        "extract_evidence",
        lambda *a, **k: pytest.fail("extraction must not run after raw-stage failure"),
    )
    monkeypatch.setattr(
        catalogue_interpretation,
        "_model_interpret_rows",
        lambda *a, **k: pytest.fail("interpretation must not run after raw-stage failure"),
    )
    submitted = _submit(db, _text_pdf_bytes(["row one"]))
    _stored_path(db, submitted.ingestion_run_id).write_bytes(b"%PDF-1.4\nchanged")

    flow_result = catalogue_ingestion_flow(ingestion_run_id=submitted.ingestion_run_id)

    assert flow_result.terminal_status == "failed"
    db.expire_all()
    run = db.query(models.IngestionRun).one()
    assert run.status == "failed"
    assert "checksum" in run.error_summary
    assert _source_row(db, submitted.ingestion_run_id).raw_stage_status == "failed"
    assert db.query(models.CatalogueExtractedEvidence).count() == 0
    assert db.query(models.CatalogueInterpretedClaim).count() == 0


def test_extraction_consumes_durable_reference_after_raw_completes(db):
    line = "10447 Healthy Cuisine Chicken 82g HK$13.10"
    submitted = _submit(db, _text_pdf_bytes([line]))

    raw = complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)
    assert raw.status == "completed"
    del raw  # the handoff is the durable reference, not this in-memory object

    asset = load_and_verify_source_asset(db, ingestion_run_id=submitted.ingestion_run_id)
    outcome = extract_source_evidence(asset)

    assert len(outcome.observations) == 1
    assert outcome.observations[0].raw_text == line
    assert outcome.observations[0].observation_key == "page:1:line:1"


def test_raw_stage_appends_one_completed_attempt_per_execution(db, forbid_understanding):
    content = _text_pdf_bytes(["attempt history row"])
    submitted = _submit(db, content)

    first = complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)
    second = complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)
    assert first == second  # business-record idempotency preserved

    db.expire_all()
    attempts = db.query(models.CatalogueRawStageAttempt).order_by(models.CatalogueRawStageAttempt.id).all()
    assert len(attempts) == 2
    assert db.query(models.CatalogueSourceDocument).count() == 1
    expected_checksum = hashlib.sha256(content).hexdigest()
    assert len({attempt.attempt_uuid for attempt in attempts}) == 2
    for attempt in attempts:
        assert attempt.status == "completed"
        assert attempt.checksum_sha256 == expected_checksum
        assert attempt.byte_size == len(content)
        assert attempt.page_count == 1
        assert attempt.attempted_at is not None
        assert attempt.completed_at is not None
        assert attempt.failure_code is None
        assert attempt.ingestion_run_uuid == str(submitted.ingestion_run_id)


def test_raw_stage_failed_first_execution_appends_sanitized_failed_attempt(db, forbid_understanding):
    submitted = _submit(db, _text_pdf_bytes(["tamper me"]))
    _stored_path(db, submitted.ingestion_run_id).write_bytes(b"%PDF-1.4\nchanged")

    with pytest.raises(SourceVerificationError):
        complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)

    db.expire_all()
    attempt = db.query(models.CatalogueRawStageAttempt).one()
    assert attempt.status == "failed"
    assert attempt.completed_at is None
    assert attempt.failure_code == "SOURCE_VERIFICATION_ERROR"
    assert "checksum" in attempt.failure_message
    assert os.environ["CATALOGUE_UPLOAD_DIR"] not in attempt.failure_message
    assert attempt.checksum_sha256 is None  # verification never produced a trusted asset
    source = _source_row(db, submitted.ingestion_run_id)
    assert source.raw_stage_status == "failed"
    assert source.raw_stage_completed_at is None


def test_raw_stage_success_then_failed_reverification_preserves_both_attempts(db, forbid_understanding):
    content = _text_pdf_bytes(["complete then tamper"])
    submitted = _submit(db, content)

    complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)
    _stored_path(db, submitted.ingestion_run_id).write_bytes(b"%PDF-1.4\nchanged")
    with pytest.raises(SourceVerificationError):
        complete_raw_stage(db, ingestion_run_id=submitted.ingestion_run_id)

    db.expire_all()
    attempts = db.query(models.CatalogueRawStageAttempt).order_by(models.CatalogueRawStageAttempt.id).all()
    assert [attempt.status for attempt in attempts] == ["completed", "failed"]
    # Current state mirrors the most recent attempt COMPLETELY — no ambiguous
    # "failed but completed_at populated" hybrid — while history preserves the
    # earlier completed verification instead of silently erasing it.
    source = _source_row(db, submitted.ingestion_run_id)
    assert source.raw_stage_status == "failed"
    assert source.raw_stage_completed_at is None
    assert source.byte_size is None
    assert source.page_count is None
    assert attempts[0].checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert attempts[0].byte_size == len(content)
    assert attempts[0].page_count == 1


def test_raw_stage_attempt_rows_contain_only_file_level_facts():
    columns = {column.name for column in models.CatalogueRawStageAttempt.__table__.columns}
    assert columns == {
        "id",
        "attempt_uuid",
        "ingestion_run_uuid",
        "catalogue_source_document_id",
        "status",
        "attempted_at",
        "completed_at",
        "checksum_sha256",
        "byte_size",
        "source_format",
        "page_count",
        "failure_code",
        "failure_message",
        "created_at",
    }


def test_raw_stage_module_import_boundary():
    """Architectural regression test over the TRANSITIVE import closure.

    Walks every project-local module reachable from the raw-stage modules and
    fails if any of them (or any name they import) depends on AI providers,
    OCR, extraction, interpretation, tagging or stage persistence. Also pins
    that the raw stage no longer depends on the submission service module —
    the shared capability policy lives in the dependency-free
    services.source_capability instead.
    """

    import ast

    forbidden = (
        "anthropic",
        "openai",
        "google",
        "pytesseract",
        "PIL",
        "services.extraction_service",
        "services.catalogue_evidence_extraction",
        "services.catalogue_interpretation",
        "services.tagging_service",
        "services.catalogue_pipeline_stages",
    )
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

    seeds = ["orchestration.catalogue_raw_stage", "orchestration.catalogue_source_loader"]
    visited: set[str] = set()
    offending: dict[str, set[str]] = {}
    queue = list(seeds)
    while queue:
        module_name = queue.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        path = _local_path(module_name)
        if path is None:
            continue  # stdlib / third-party leaf; its name was already screened
        for imported in _imports_of(path, module_name):
            if any(imported == item or imported.startswith(item + ".") for item in forbidden):
                offending.setdefault(module_name, set()).add(imported)
            queue.append(imported)

    assert not offending, f"raw-stage dependency closure contains forbidden imports: {offending}"
    # The transitive submission-service dependency is gone by construction.
    assert "services.catalogue_submission" not in visited
    assert "services.source_capability" in visited
