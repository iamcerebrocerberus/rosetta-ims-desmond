"""Per-layer read API over the catalogue pipeline (raw / staging / intermediate).

Exercises a real submission + deterministic pipeline run (vision extraction is
stubbed to return contract-labeled cells; the supplier contract then maps them
with no AI) and reads each layer back.
"""

from __future__ import annotations

import json
import os
import tempfile
from io import BytesIO
from uuid import UUID

import pytest
import pypdf

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
from services import catalogue_evidence_extraction as extraction  # noqa: E402
from services.catalogue_submission import CatalogueSubmissionCommand, CatalogueSubmissionService  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)

# One Hill's product row, labeled by the supplier contract's source columns.
HILLS_ROW = {
    "Product Code / 產品編號": "10447",
    "Product Range / 產品系列": "Science Plan",
    "Life Stage / 生命階段": "Adult",
    "Product Description / 產品名稱": "Chicken 82g",
    "Size / 重量": "82g",
    "Gross Wholesale Price / 每箱·罐": "13.10",
    "Order Multiple / 訂貨單位": "12",
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
        models.CatalogueNormalizedRowEvidence,
        models.CatalogueNormalizedRow,
        models.CatalogueExtractedEvidence,
        models.IngestionRun,
        models.CatalogueSourceDocument,
    ):
        session.query(model).delete()
    session.query(models.CatalogueImport).delete()
    session.commit()


def _pdf_bytes(page_count: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _vision_envelope(rows: list[dict[str, str]]) -> str:
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
                            "column_index": index + 1,
                            "raw_value": value,
                        }
                        for index, (column, value) in enumerate(row.items())
                    ],
                    "bounding_box": {"x": 0, "y": 0, "width": 1, "height": 1, "unit": "px"},
                    "confidence": "0.95",
                }
                for row in rows
            ],
        }
    )


def _run_pipeline(db, monkeypatch) -> UUID:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        extraction,
        "_call_gemini_vision",
        lambda content, *, media_type: extraction._VisionResponse(text=_vision_envelope([HILLS_ROW])),
    )
    service = CatalogueSubmissionService(db, upload_root=os.environ["CATALOGUE_UPLOAD_DIR"], max_upload_bytes=1024 * 1024)
    result = service.submit(
        CatalogueSubmissionCommand(
            supplier_id=14,
            original_filename="hills.pdf",
            content_type="application/pdf",
            stream=BytesIO(_pdf_bytes()),
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


def test_staging_layer_returns_evidence_and_normalized_rows(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/staging").json()

    assert body["layer"] == "staging"
    # Verbatim evidence (audit record) + the contract-conformed normalized rows.
    assert body["evidence_count"] == 1
    assert body["evidence"][0]["extraction_method"] == "MODEL_VISION"
    assert body["normalized_row_count"] == 1
    row = body["normalized_rows"][0]
    assert row["raw_fields"]["supplier_sku"] == "10447"
    fields = row["normalized_fields"]
    assert fields["supplier_sku"]["value"] == "10447"
    assert fields["product_name"]["value"] == "Science Plan Adult Chicken 82g"
    assert fields["brand"]["value"] == "Hill's"
    assert fields["cost"]["currency"] == "HKD"
    assert fields["cost"]["amount"] == "13.10"


def test_intermediate_layer_returns_validation_and_candidates(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/intermediate").json()

    assert body["layer"] == "intermediate"
    # Business interpretation only — normalized rows live under /staging.
    assert "normalized_rows" not in body and "claims" not in body
    assert len(body["mastering_candidates"]) == 1
    assert body["mastering_candidates"][0]["review_status"] == "PENDING_REVIEW"
    assert isinstance(body["validation_issues"], list)


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
