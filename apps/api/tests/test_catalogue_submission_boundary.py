"""v2 catalogue submission boundary tests."""

from __future__ import annotations

import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402
from dependencies import require_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services import catalogue_submission, extraction_service, tagging_service  # noqa: E402
from services.catalogue_submission import (  # noqa: E402
    CatalogueSubmissionCommand,
    CatalogueSubmissionService,
    EmptyUploadError,
    SubmissionPersistenceError,
    SubmissionIdempotencyConflict,
    UnsupportedSourceTypeError,
    UploadTooLargeError,
)


models.Base.metadata.create_all(bind=database.engine)
database.run_migrations(database.engine)


class _Admin:
    id = 99
    username = "submission-admin"
    display_name = "Submission Admin"
    role = "admin"


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    previous_root = main.app.dependency_overrides.get(require_user)
    previous_v2 = main.alias_app.dependency_overrides.get(require_user)
    main.app.dependency_overrides[require_user] = lambda: _Admin()
    main.alias_app.dependency_overrides[require_user] = lambda: _Admin()
    monkeypatch.setattr(extraction_service, "extract", lambda *a, **k: pytest.fail("v2 submission must not extract"))
    monkeypatch.setattr(tagging_service, "suggest_tags", lambda *a, **k: pytest.fail("v2 submission must not tag"))
    yield
    if previous_root is None:
        main.app.dependency_overrides.pop(require_user, None)
    else:
        main.app.dependency_overrides[require_user] = previous_root
    if previous_v2 is None:
        main.alias_app.dependency_overrides.pop(require_user, None)
    else:
        main.alias_app.dependency_overrides[require_user] = previous_v2


@pytest.fixture()
def db():
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


@pytest.fixture()
def client(tmp_path, monkeypatch, db):
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CATALOGUE_SUBMISSION_MAX_BYTES", str(1024 * 1024))
    return TestClient(main.app)


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


def _pdf(name="hills.pdf", body=b"%PDF-1.4\n% fixture\n"):
    return {"file": (name, body, "application/pdf")}


def _command(stream: BytesIO, *, key: str | None = None, supplier_id: int = 14, filename: str = "hills.pdf"):
    return CatalogueSubmissionCommand(
        supplier_id=supplier_id,
        original_filename=filename,
        content_type="application/pdf",
        stream=stream,
        idempotency_key=key,
        submitted_by="test",
    )


def test_submission_service_registers_source_import_and_queued_run(db, tmp_path):
    service = CatalogueSubmissionService(db, upload_root=tmp_path, max_upload_bytes=1024)

    result = service.submit(_command(BytesIO(b"%PDF-1.4\nsample"), key="submit-1"))

    assert result.status == "queued"
    assert result.contract_id == "hills.price_list.v1"
    assert result.contract_version == "v1"
    assert result.document_type == "PRICE_LIST"
    assert db.query(models.CatalogueImport).count() == 1
    assert db.query(models.CatalogueItem).count() == 0
    assert db.query(models.CatalogueSourceDocument).count() == 1
    assert db.query(models.IngestionRun).count() == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 0
    assert db.query(models.CatalogueInterpretedClaim).count() == 0
    assert db.query(models.CatalogueMasteringCandidate).count() == 0
    assert db.query(models.CatalogueServingPublication).count() == 0

    source = db.query(models.CatalogueSourceDocument).one()
    run = db.query(models.IngestionRun).one()
    legacy = db.query(models.CatalogueImport).one()

    assert source.source_checksum and len(source.source_checksum) == 64
    assert source.source_ref.startswith("v2/")
    assert source.filename == "hills.pdf"
    assert Path(tmp_path / source.source_ref).exists()
    assert legacy.status == "queued"
    assert legacy.item_count == 0
    assert legacy.source_ref == source.source_ref
    assert run.run_uuid == str(result.ingestion_run_id)
    assert run.status == "queued"
    assert run.started_at is None
    assert run.completed_at is None
    assert run.items_extracted is None
    assert run.supplier_source_contract_id == "hills.price_list.v1"


def test_submission_idempotency_replays_same_result_and_conflicts_on_changed_material(db, tmp_path):
    service = CatalogueSubmissionService(db, upload_root=tmp_path, max_upload_bytes=1024)

    first = service.submit(_command(BytesIO(b"%PDF-1.4\nsame"), key="same-key"))
    second = service.submit(_command(BytesIO(b"%PDF-1.4\nsame"), key="same-key"))

    assert second == first
    assert db.query(models.CatalogueImport).count() == 1
    assert db.query(models.CatalogueSourceDocument).count() == 1
    assert db.query(models.IngestionRun).count() == 1
    assert len(list((tmp_path / "v2").iterdir())) == 1

    with pytest.raises(SubmissionIdempotencyConflict):
        service.submit(_command(BytesIO(b"%PDF-1.4\nchanged"), key="same-key"))
    assert db.query(models.IngestionRun).count() == 1
    assert len(list((tmp_path / "v2").iterdir())) == 1


def test_submission_without_idempotency_key_creates_distinct_runs(db, tmp_path):
    service = CatalogueSubmissionService(db, upload_root=tmp_path, max_upload_bytes=1024)

    first = service.submit(_command(BytesIO(b"%PDF-1.4\nsame")))
    second = service.submit(_command(BytesIO(b"%PDF-1.4\nsame")))

    assert second.ingestion_run_id != first.ingestion_run_id
    assert db.query(models.CatalogueImport).count() == 2
    assert db.query(models.CatalogueSourceDocument).count() == 2
    assert db.query(models.IngestionRun).count() == 2


def test_submission_file_validation_and_cleanup(db, tmp_path):
    service = CatalogueSubmissionService(db, upload_root=tmp_path, max_upload_bytes=12)

    with pytest.raises(EmptyUploadError):
        service.submit(_command(BytesIO(b""), key="empty"))
    with pytest.raises(UploadTooLargeError):
        service.submit(_command(BytesIO(b"%PDF-1.4\nthis is too large"), key="large"))
    with pytest.raises(UnsupportedSourceTypeError):
        service.submit(_command(BytesIO(b"%PDF-1.4\nsample"), key="txt", filename="fake.txt"))
    with pytest.raises(UnsupportedSourceTypeError):
        service.submit(_command(BytesIO(b"not a pdf"), key="bad-signature"))

    assert db.query(models.CatalogueImport).count() == 0
    if (tmp_path / "v2").exists():
        assert list((tmp_path / "v2").glob("*")) == []


def test_submission_storage_failure_commits_no_database_rows(db, tmp_path):
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("file")
    service = CatalogueSubmissionService(db, upload_root=blocked_root, max_upload_bytes=1024)

    with pytest.raises(catalogue_submission.StorageUnavailableError):
        service.submit(_command(BytesIO(b"%PDF-1.4\nsample"), key="storage-failure"))

    assert db.query(models.CatalogueImport).count() == 0
    assert db.query(models.IngestionRun).count() == 0


def test_submission_database_failure_cleans_new_file(db, tmp_path, monkeypatch):
    service = CatalogueSubmissionService(db, upload_root=tmp_path, max_upload_bytes=1024)

    def fail_commit():
        raise RuntimeError("commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", fail_commit)
        with pytest.raises(SubmissionPersistenceError):
            service.submit(_command(BytesIO(b"%PDF-1.4\nsample"), key="db-failure"))

    if (tmp_path / "v2").exists():
        assert list((tmp_path / "v2").glob("*")) == []


def test_v2_submission_endpoint_accepts_and_status_polls(client, db, tmp_path):
    response = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files=_pdf(),
        headers={"Idempotency-Key": "api-submit-1"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["contract_id"] == "hills.price_list.v1"
    assert body["contract_version"] == "v1"
    assert body["status_url"] == f"/catalogues/ingestions/{body['ingestion_run_id']}"

    status_response = client.get(body["status_url"])
    assert status_response.status_code == 200, status_response.text
    status_body = status_response.json()
    assert status_body["ingestion_run_id"] == body["ingestion_run_id"]
    assert status_body["supplier_catalogue_id"] == body["supplier_catalogue_id"]
    assert status_body["source_file_id"] == body["source_file_id"]
    assert status_body["started_at"] is None
    assert status_body["completed_at"] is None

    source = db.query(models.CatalogueSourceDocument).one()
    assert Path(tmp_path / "uploads" / source.source_ref).exists()
    assert db.query(models.CatalogueItem).count() == 0


def test_v2_submission_endpoint_contract_and_file_errors(client):
    partial = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14", "contract_id": "hills.price_list.v1"},
        files=_pdf(),
    )
    assert partial.status_code == 422
    assert partial.json()["detail"]["code"] == "INVALID_CONTRACT_PARAMETERS"

    unknown_version = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14", "contract_id": "hills.price_list.v1", "contract_version": "v2"},
        files=_pdf(),
    )
    assert unknown_version.status_code == 422

    mismatch = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "1", "contract_id": "hills.price_list.v1", "contract_version": "v1"},
        files=_pdf(),
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "SUPPLIER_CONTRACT_MISMATCH"

    unsupported = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "91", "contract_id": "vetapet.vet_price_list.v1", "contract_version": "v1"},
        files=_pdf("vetapet.pdf"),
    )
    assert unsupported.status_code == 422

    unsupported_type = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files={"file": ("fake.txt", b"%PDF-1.4\nsample", "application/pdf")},
    )
    assert unsupported_type.status_code == 415

    traversal = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files={"file": ("../evil.pdf", b"%PDF-1.4\nsample", "application/pdf")},
    )
    assert traversal.status_code == 400


def test_v2_submission_endpoint_idempotency_conflict_and_unknown_status(client):
    first = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files=_pdf(body=b"%PDF-1.4\nsame"),
        headers={"Idempotency-Key": "api-same"},
    )
    replay = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files=_pdf(body=b"%PDF-1.4\nsame"),
        headers={"Idempotency-Key": "api-same"},
    )
    conflict = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files=_pdf(body=b"%PDF-1.4\nchanged"),
        headers={"Idempotency-Key": "api-same"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["ingestion_run_id"] == first.json()["ingestion_run_id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    missing = client.get("/catalogues/ingestions/99999999-9999-4999-8999-999999999999")
    assert missing.status_code == 404


def test_submission_auth_and_openapi_contract(db, tmp_path, monkeypatch):
    main.app.dependency_overrides.pop(require_user, None)
    main.alias_app.dependency_overrides.pop(require_user, None)
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    client = TestClient(main.app)

    unauthenticated = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files=_pdf(),
    )
    assert unauthenticated.status_code == 401

    main.app.dependency_overrides[require_user] = lambda: _Admin()
    main.alias_app.dependency_overrides[require_user] = lambda: _Admin()
    schema = client.get("/openapi.json").json()
    assert "/catalogues/ingestions" in schema["paths"]
    assert "/catalogues/ingestions/{run_uuid}" in schema["paths"]
    assert client.get("/v2/openapi.json").status_code == 404


def test_submission_migration_relaxes_existing_started_at_not_null(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old_run.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE suppliers (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL)"))
        conn.execute(text("CREATE TABLE catalogue_imports (id INTEGER PRIMARY KEY, supplier_id INTEGER, filename TEXT NOT NULL, format TEXT, imported_at TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER)"))
        conn.execute(text("""
            CREATE TABLE catalogue_ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_document_id INTEGER NOT NULL,
                supplier_id INTEGER,
                contract_version TEXT,
                extractor_name TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                parent_run_id INTEGER,
                status TEXT NOT NULL DEFAULT 'queued',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                items_extracted INTEGER,
                metrics TEXT,
                error_summary TEXT,
                created_at TEXT NOT NULL
            )
        """))
        conn.execute(text("INSERT INTO suppliers VALUES (14, 'HILLS', 'Hill''s', '2026-07-23T00:00:00+00:00')"))
        conn.execute(text("INSERT INTO catalogue_imports VALUES (1, 14, 'hills.pdf', 'pdf', '2026-07-23T00:00:00+00:00', 'queued', 0)"))
        conn.execute(text("""
            INSERT INTO catalogue_ingestion_runs (
                source_document_id, supplier_id, contract_version, extractor_name, extractor_version,
                status, started_at, created_at
            )
            VALUES (1, 14, 'catalogue.extraction_profile.v1', 'old', 'v1', 'queued',
                    '2026-07-23T00:00:00+00:00', '2026-07-23T00:00:00+00:00')
        """))

    database.run_migrations(engine)

    inspector = sa.inspect(engine)
    started_at = next(column for column in inspector.get_columns("catalogue_ingestion_runs") if column["name"] == "started_at")
    assert started_at["nullable"] is True
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO catalogue_ingestion_runs (
                run_uuid, source_document_id, supplier_id, extractor_name, extractor_version, status, started_at, created_at
            )
            VALUES ('99999999-9999-4999-8999-999999999999', 1, 14, 'queued', 'v1', 'queued', NULL,
                    '2026-07-23T00:00:00+00:00')
        """))


def test_post_commit_audit_failure_does_not_fail_the_durable_submission(client, db, monkeypatch, caplog):
    import logging as _logging

    from services import audit_log

    def _audit_down(*_a, **_k):
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(audit_log, "record", _audit_down)

    with caplog.at_level(_logging.ERROR, logger="routers.catalogue_ingestions"):
        response = client.post(
            "/catalogues/ingestions",
            data={"supplier_id": "14"},
            files=_pdf(),
            headers={"Idempotency-Key": "audit-down-key"},
        )

    assert response.status_code == 202, response.text
    body = response.json()
    db.expire_all()
    assert db.query(models.IngestionRun).count() == 1
    run = db.query(models.IngestionRun).one()
    assert run.status == "queued"
    assert run.run_uuid == body["ingestion_run_id"]
    source = db.query(models.CatalogueSourceDocument).one()
    stored = Path(os.environ["CATALOGUE_UPLOAD_DIR"]) / source.source_ref
    assert stored.exists() and stored.read_bytes().startswith(b"%PDF")
    # The failure is observable, sanitized, and no audit row was half-written.
    assert any("audit logging failed" in record.getMessage() for record in caplog.records)
    assert "audit backend unavailable" not in response.text
    assert (
        db.query(models.AuditLog)
        .filter_by(action="catalogue.ingestion_submit", entity_id=run.run_uuid)
        .count()
        == 0
    )

    # Retry with the same idempotency key stays safe: same run, still exactly one.
    retry = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files=_pdf(),
        headers={"Idempotency-Key": "audit-down-key"},
    )
    assert retry.status_code == 202
    assert retry.json()["ingestion_run_id"] == body["ingestion_run_id"]
    db.expire_all()
    assert db.query(models.IngestionRun).count() == 1


def test_legacy_xls_is_rejected_at_submission_with_no_partial_state(client, db, monkeypatch):
    from orchestration import catalogue_raw_stage
    from services import catalogue_evidence_extraction

    monkeypatch.setattr(
        catalogue_raw_stage, "complete_raw_stage", lambda *a, **k: pytest.fail("raw must not run for rejected .xls")
    )
    monkeypatch.setattr(
        catalogue_evidence_extraction,
        "extract_evidence",
        lambda *a, **k: pytest.fail("extraction must not run for rejected .xls"),
    )

    response = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files={"file": ("legacy.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 legacy", "application/vnd.ms-excel")},
    )

    assert response.status_code == 415, response.text
    assert response.json()["detail"]["code"] == "UNSUPPORTED_SOURCE_TYPE"
    db.expire_all()
    assert db.query(models.IngestionRun).count() == 0
    assert db.query(models.CatalogueSourceDocument).count() == 0
    assert db.query(models.CatalogueImport).count() == 0
    upload_root = Path(os.environ["CATALOGUE_UPLOAD_DIR"])
    assert not any((upload_root / "v2").glob("*")) if (upload_root / "v2").exists() else True


def test_xlsx_passes_the_capability_gate_and_ole_signatures_do_not(client, db):
    # Capability policy: .xlsx is a supported format; legacy .xls is not.
    assert catalogue_submission._source_format_from_suffix(".xlsx") == "SPREADSHEET"
    assert catalogue_submission._source_format_from_suffix(".xls") is None
    assert catalogue_submission.signature_matches("SPREADSHEET", b"PK\x03\x04rest")
    assert not catalogue_submission.signature_matches("SPREADSHEET", b"\xd0\xcf\x11\xe0rest")

    # Route level: an .xlsx upload clears the capability gate and is judged by
    # the supplier contract instead (Hill's declares a PDF source), proving the
    # rejection reason differs from the .xls capability rejection.
    response = client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files={
            "file": (
                "catalogue.xlsx",
                b"PK\x03\x04 fixture",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 415
    assert "does not match supplier contract" in response.json()["detail"]["message"]
