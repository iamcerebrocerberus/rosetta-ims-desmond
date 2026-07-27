"""Per-layer read API over the catalogue pipeline.

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


class _DataEntry:
    id = 502
    username = "data-entry"
    display_name = "Data Entry"
    role = "data_entry"


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
    session.query(models.CatalogueImport).delete()
    session.query(models.Product).filter_by(sku_code="10447").delete()
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


def test_staging_layer_returns_only_extracted_evidence(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/staging").json()

    assert body["layer"] == "staging"
    assert len(body["extraction_attempts"]) == 1
    attempt = body["extraction_attempts"][0]
    assert attempt["status"] == "COMPLETE"
    assert attempt["units_attempted"] == attempt["units_completed"] == 1
    assert attempt["unit_outcomes"][0]["status"] == "EVIDENCE_CAPTURED"
    # Staging preserves what extraction observed; interpretation starts later.
    assert body["evidence_count"] == 1
    assert body["evidence"][0]["extraction_method"] == "MODEL_VISION"
    assert "normalized_rows" not in body


def test_intermediate_layer_returns_normalized_rows_validation_and_candidates(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    body = client.get(f"/catalogues/ingestions/{run}/intermediate").json()

    assert body["layer"] == "intermediate"
    assert len(body["normalized_rows"]) == 1
    row = body["normalized_rows"][0]
    assert row["raw_fields"]["supplier_sku"] == "10447"
    fields = row["normalized_fields"]
    assert fields["supplier_sku"]["value"] == "10447"
    assert fields["product_name"]["value"] == "Science Plan Adult Chicken 82g"
    assert fields["brand"]["value"] == "Hill's"
    assert fields["cost"]["currency"] == "HKD"
    assert fields["cost"]["amount"] == "13.10"
    assert len(body["mastering_candidates"]) == 1
    assert body["mastering_candidates"][0]["review_status"] == "PENDING_REVIEW"
    assert isinstance(body["validation_issues"], list)


def test_serving_layer_exposes_only_explicit_immutable_publications(client, db, monkeypatch):
    product = models.Product(
        sku_code="10447",
        name="Hill's Healthy Cuisine Chicken 82g",
        brand="Hill's",
        category="Food",
        storage_rule="any",
        status="ACTIVE",
        created_at="2026-07-23T00:00:00+00:00",
        updated_at="2026-07-23T00:00:00+00:00",
    )
    db.add(product)
    db.flush()
    # Mastering resolves canonical products through the APPROVED supplier
    # mapping — never by assuming supplier SKU == canonical SKU.
    db.add(
        models.CatalogueSupplierProduct(
            supplier_product_key="supplier:14:offer:10447",
            supplier_id=14,
            product_variant_id=product.id,
            supplier_sku="10447",
            status="active",
            created_at="2026-07-23T00:00:00+00:00",
            updated_at="2026-07-23T00:00:00+00:00",
        )
    )
    db.commit()
    run = _run_pipeline(db, monkeypatch)
    empty = client.get(f"/catalogues/ingestions/{run}/serving").json()

    assert empty == {
        "ingestion_run_id": str(run),
        "layer": "serving",
        "publication_count": 0,
        "current_publications": [],
        "publication_history": [],
    }
    candidate = db.query(models.CatalogueMasteringCandidate).filter_by(
        ingestion_run_uuid=str(run)
    ).one()
    candidate_id = UUID(candidate.mastering_candidate_uuid)
    publish_before_review = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/publish",
        json={"publication_version": "read-api-v1"},
    )
    assert publish_before_review.status_code == 409

    review = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/review",
        headers={"Idempotency-Key": "read-api-approval"},
        json={"review_status": "APPROVED", "reason": "Verified against source evidence."},
    )
    assert review.status_code == 200
    assert review.json()["stage"] == "review_decision"

    publish_before_apply = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/publish",
        json={"publication_version": "read-api-v1"},
    )
    assert publish_before_apply.status_code == 409

    applied = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/apply",
        json={},
    )
    assert applied.status_code == 200
    assert applied.json()["stage"] == "commercial_application"

    publication = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/publish",
        headers={"Idempotency-Key": "read-api-publish"},
        json={"publication_version": "read-api-v1"},
    )
    assert publication.status_code == 200
    assert publication.json()["stage"] == "serving_publication"

    body = client.get(f"/catalogues/ingestions/{run}/serving").json()
    assert body["publication_count"] == 1
    assert len(body["current_publications"]) == 1
    assert body["current_publications"][0]["canonical_sku"] == "10447"
    assert body["publication_history"][0]["is_current"] is True
    assert body["publication_history"][0]["snapshot"] == body["current_publications"][0]

    consumer_page = client.get("/products/serving-publications").json()
    assert consumer_page["total"] == 1
    assert consumer_page["items"][0] == body["current_publications"][0]
    assert client.get("/products/serving-publications?search=10447").json()["total"] == 1
    assert client.get("/products/serving-publications?supplier_id=999").json()["total"] == 0

    second_publication = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/publish",
        headers={"Idempotency-Key": "read-api-publish-v2"},
        json={"publication_version": "read-api-v2"},
    )
    assert second_publication.status_code == 200
    detail = client.get("/products/serving-publications/10447").json()
    assert detail["canonical_sku"] == "10447"
    assert len(detail["current_publications"]) == 1
    assert detail["current_publications"][0]["lineage"]["publication_version"] == "read-api-v2"
    assert len(detail["publication_history"]) == 2
    assert sum(item["is_current"] for item in detail["publication_history"]) == 1
    assert {
        item["snapshot"]["lineage"]["publication_version"]
        for item in detail["publication_history"]
    } == {"read-api-v1", "read-api-v2"}
    current_after_supersession = client.get("/products/serving-publications").json()
    assert current_after_supersession["total"] == 1
    assert current_after_supersession["items"][0]["lineage"]["publication_version"] == "read-api-v2"
    assert client.get("/products/serving-publications/DOES-NOT-EXIST").status_code == 404

    actions = {
        action
        for (action,) in db.query(models.AuditLog.action)
        .filter(models.AuditLog.action.like("catalogue.pipeline_%"))
        .all()
    }
    assert actions == {
        "catalogue.pipeline_candidate_review",
        "catalogue.pipeline_candidate_apply",
        "catalogue.pipeline_serving_publish",
    }


def test_candidate_correction_supersedes_and_publishes_the_corrected_identity(client, db, monkeypatch):
    """HITL correction lifecycle end-to-end through the API.

    machine candidate (unmatched, unapprovable) -> reviewer corrects the
    canonical match -> immutable revision supersedes it -> approve/apply/
    publish the revision -> serving carries the corrected Rosetta identity.
    """
    run = _run_pipeline(db, monkeypatch)
    candidate = db.query(models.CatalogueMasteringCandidate).filter_by(ingestion_run_uuid=str(run)).one()
    candidate_id = candidate.mastering_candidate_uuid

    # Unmatched machine proposal (no supplier mapping exists): not approvable.
    rejected = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/review",
        json={"review_status": "APPROVED"},
    )
    assert rejected.status_code == 409

    product = db.query(models.Product).filter_by(sku_code="RIMS-API-1").first()
    if product is None:
        product = models.Product(
            sku_code="RIMS-API-1",
            name="Hill's Science Plan Adult Chicken 82g",
            brand="Hill's",
            category="Food",
            storage_rule="any",
            status="ACTIVE",
            created_at="2026-07-23T00:00:00+00:00",
            updated_at="2026-07-23T00:00:00+00:00",
        )
        db.add(product)
        db.commit()

    corrected = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/correct",
        json={
            "reason": "Matched to the existing Rosetta product by the reviewer.",
            "product_variant_resolution": {
                "state": "CONFIRMED_MATCH",
                "canonical_sku": "RIMS-API-1",
                "product_variant_id": "RIMS-API-1",
                "product_variant_name": product.name,
                "product_family_id": None,
            },
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["stage"] == "mastering_correction"
    revision_id = corrected.json()["output_ids"][0]
    assert revision_id != candidate_id

    # The superseded candidate accepts no further decisions and is marked in reads.
    superseded_review = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/review",
        json={"review_status": "APPROVED"},
    )
    assert superseded_review.status_code == 409
    intermediate = client.get(f"/catalogues/ingestions/{run}/intermediate").json()
    by_id = {c["mastering_candidate_id"]: c for c in intermediate["mastering_candidates"]}
    assert by_id[candidate_id]["superseded_by"] == revision_id
    assert by_id[revision_id]["superseded_by"] is None
    assert by_id[revision_id]["metadata"]["correction"]["revised_from"] == candidate_id

    # The corrected revision flows to serving with the Rosetta identity —
    # supplier SKU (10447) and canonical SKU (RIMS-API-1) stay distinct.
    approve = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{revision_id}/review",
        json={"review_status": "APPROVED", "reason": "Corrected match verified."},
    )
    assert approve.status_code == 200, approve.text
    assert client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{revision_id}/apply", json={}
    ).status_code == 200
    published = client.post(
        f"/catalogues/ingestions/{run}/mastering-candidates/{revision_id}/publish",
        json={"publication_version": "corrected-v1"},
    )
    assert published.status_code == 200, published.text

    serving = client.get(f"/catalogues/ingestions/{run}/serving").json()
    assert serving["publication_count"] == 1
    snapshot = serving["current_publications"][0]
    assert snapshot["canonical_sku"] == "RIMS-API-1"
    assert snapshot["supplier_offering"]["supplier_sku"] == "10447"


def test_validation_resolution_is_run_scoped_and_audited(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    normalized = db.query(models.CatalogueNormalizedRow).filter_by(
        ingestion_run_uuid=str(run)
    ).one()
    issue_id = UUID("77777777-7777-4777-8777-777777777777")
    db.add(
        models.CatalogueValidationIssue(
            validation_issue_uuid=str(issue_id),
            contract_version="catalogue.validation_issue.v1",
            ingestion_run_uuid=str(run),
            catalogue_item_uuid=normalized.catalogue_item_uuid,
            stage="STAGING",
            issue_code="HITL_CONFIRMATION_REQUIRED",
            severity="WARNING",
            message="Reviewer confirmation is required.",
            created_at="2026-07-27T00:00:00+00:00",
            resolution_status="OPEN",
            publish_blocking=1,
        )
    )
    db.commit()

    wrong_run = "99999999-9999-4999-8999-999999999999"
    assert client.post(
        f"/catalogues/ingestions/{wrong_run}/validation-issues/{issue_id}/resolve",
        json={"resolution_status": "CONFIRMED"},
    ).status_code == 404

    response = client.post(
        f"/catalogues/ingestions/{run}/validation-issues/{issue_id}/resolve",
        headers={"Idempotency-Key": "confirm-warning"},
        json={
            "resolution_status": "CONFIRMED",
            "resolution_note": "Checked against the supplier document.",
        },
    )
    assert response.status_code == 200
    assert response.json()["stage"] == "validation_resolution"
    issue = db.query(models.CatalogueValidationIssue).filter_by(
        validation_issue_uuid=str(issue_id)
    ).one()
    assert issue.resolution_status == "CONFIRMED"
    assert issue.publish_blocking == 0
    assert issue.resolver_id == _Admin.username
    assert db.query(models.CatalogueReviewDecision).filter_by(
        validation_issue_uuid=str(issue_id)
    ).count() == 1
    assert db.query(models.AuditLog).filter_by(
        action="catalogue.pipeline_validation_resolve",
        entity_id=str(issue_id),
    ).count() == 1


def test_read_endpoints_require_authorization(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    candidate_id = db.query(models.CatalogueMasteringCandidate.mastering_candidate_uuid).filter_by(
        ingestion_run_uuid=str(run)
    ).scalar()
    main.app.dependency_overrides.pop(require_user, None)  # drop the admin override
    try:
        for layer in ("raw", "staging", "intermediate", "serving"):
            assert client.get(f"/catalogues/ingestions/{run}/{layer}").status_code in {401, 403}
        assert client.get("/products/serving-publications").status_code in {401, 403}
        assert client.get("/products/serving-publications/10447").status_code in {401, 403}
        assert client.post(
            f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/review",
            json={"review_status": "REJECTED"},
        ).status_code in {401, 403}
    finally:
        main.app.dependency_overrides[require_user] = lambda: _Admin()


def test_data_entry_cannot_apply_or_publish_commercial_state(client, db, monkeypatch):
    run = _run_pipeline(db, monkeypatch)
    candidate_id = db.query(models.CatalogueMasteringCandidate.mastering_candidate_uuid).filter_by(
        ingestion_run_uuid=str(run)
    ).scalar()
    main.app.dependency_overrides[require_user] = lambda: _DataEntry()
    try:
        assert client.post(
            f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/apply",
            json={},
        ).status_code == 403
        assert client.post(
            f"/catalogues/ingestions/{run}/mastering-candidates/{candidate_id}/publish",
            json={"publication_version": "forbidden"},
        ).status_code == 403
    finally:
        main.app.dependency_overrides[require_user] = lambda: _Admin()


def test_unknown_run_returns_404(client):
    missing = "99999999-9999-4999-8999-999999999999"
    for layer in ("raw", "staging", "intermediate", "serving"):
        response = client.get(f"/catalogues/ingestions/{missing}/{layer}")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "INGESTION_RUN_NOT_FOUND"
