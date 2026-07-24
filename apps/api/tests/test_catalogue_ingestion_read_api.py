"""Per-layer read API over the catalogue pipeline (raw / staging / intermediate)."""

from __future__ import annotations

import os
import tempfile
from io import BytesIO
from uuid import UUID

import pytest
import pypdf
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
os.environ.setdefault("PREFECT_API_MODE", "offline")
os.environ.setdefault("PREFECT_LOGGING_LEVEL", "ERROR")
os.environ.setdefault("PREFECT_LOGGING_TO_API_ENABLED", "false")
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")

import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402
from dependencies import require_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from orchestration.catalogue_flows import catalogue_ingestion_flow  # noqa: E402
from services import catalogue_interpretation  # noqa: E402
from services.catalogue_submission import CatalogueSubmissionCommand, CatalogueSubmissionService  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)

HILLS_ROW = "10447 Hill's Healthy Cuisine Chicken 82g 82g HKD 13.10"
HILLS_FIELDS = {
    "description": "Hill's Healthy Cuisine Chicken 82g",
    "brand": "Hill's",
    "supplier_sku": "10447",
    "cost_price": 13.1,
    "pack_size": "82g",
    "confidence": "0.96",
}


class _Admin:
    id = 501
    username = "read-admin"
    display_name = "Read Admin"
    role = "admin"


@pytest.fixture(autouse=True)
def _auth():
    previous = main.app.dependency_overrides.get(require_user)
    main.app.dependency_overrides[require_user] = lambda: _Admin()
    yield
    if previous is None:
        main.app.dependency_overrides.pop(require_user, None)
    else:
        main.app.dependency_overrides[require_user] = previous


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES", str(1024 * 1024))
    session = database.SessionLocal()
    try:
        _reset(session)
        if session.get(models.Supplier, 14) is None:
            session.add(models.Supplier(id=14, code="HILLS", name="Hill's", created_at="2026-07-25T00:00:00+00:00"))
            session.commit()
        yield session
        session.rollback()
        _reset(session)
    finally:
        session.close()


@pytest.fixture()
def client(db):
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
    session.query(models.CatalogueImport).delete()
    session.commit()


def _text_pdf(lines: list[str]) -> bytes:
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
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    parts = ["BT", "/F1 10 Tf", "36 740 Td", "16 TL"]
    for line in lines:
        parts.append(f"({line.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj")
        parts.append("T*")
    parts.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode("utf-8"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _run_pipeline(db, monkeypatch) -> UUID:
    monkeypatch.setattr(
        catalogue_interpretation,
        "_model_interpret_rows",
        lambda rows, _contract: {key: dict(HILLS_FIELDS) for key in rows},
    )
    service = CatalogueSubmissionService(db, upload_root=os.environ["CATALOGUE_UPLOAD_DIR"], max_upload_bytes=1024 * 1024)
    result = service.submit(
        CatalogueSubmissionCommand(
            supplier_id=14,
            original_filename="hills.pdf",
            content_type="application/pdf",
            stream=BytesIO(_text_pdf([HILLS_ROW])),
            contract_id=None,
            contract_version=None,
            idempotency_key=None,
            submitted_by="pytest",
        )
    )
    catalogue_ingestion_flow(ingestion_run_id=result.ingestion_run_id)
    return result.ingestion_run_id


def test_raw_layer_returns_file_facts_and_attempts(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/raw").json()

    assert body["layer"] == "raw"
    assert body["source"]["page_count"] == 1
    assert body["source"]["raw_stage_status"] == "completed"
    assert body["source"]["checksum_sha256"]
    assert body["source"]["supplier_source_contract_id"] == "hills.price_list.v1"
    assert body["attempts"][0]["status"] == "completed"
    # File facts only — no bytes, no source_ref path, no extracted content.
    assert "source_ref" not in body["source"]
    assert "raw_text" not in str(body)


def test_staging_layer_returns_verbatim_extracted_evidence(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/staging").json()

    assert body["layer"] == "staging"
    assert body["count"] == 1
    evidence = body["evidence"][0]
    assert evidence["raw_text"] == HILLS_ROW
    assert evidence["extraction_method"] == "PDF_TEXT"
    assert evidence["source_location"]["page_number"] == 1
    # No interpreted/semantic fields on evidence records.
    assert "proposed_fields" not in evidence and "cost" not in evidence


def test_intermediate_layer_returns_claims_and_candidates(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/intermediate").json()

    assert body["layer"] == "intermediate"
    assert len(body["claims"]) == 1
    claim = body["claims"][0]
    assert claim["raw_fields"]["supplier_sku"] == "10447"
    assert claim["proposed_fields"]["supplier_sku"]["value"] == "10447"
    assert claim["proposed_fields"]["cost"]["currency"] == "HKD"
    assert len(body["mastering_candidates"]) == 1
    assert body["mastering_candidates"][0]["review_status"] == "PENDING_REVIEW"


def test_read_endpoints_require_authorization(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    main.app.dependency_overrides.pop(require_user, None)  # drop the admin override
    try:
        for layer in ("raw", "staging", "intermediate"):
            assert client.get(f"/catalogues/ingestions/{run}/{layer}").status_code in {401, 403}
    finally:
        main.app.dependency_overrides[require_user] = lambda: _Admin()


def test_unknown_run_returns_404(client):
    missing = "99999999-9999-4999-8999-999999999999"
    for layer in ("raw", "staging", "intermediate"):
        response = client.get(f"/catalogues/ingestions/{missing}/{layer}")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "INGESTION_RUN_NOT_FOUND"
