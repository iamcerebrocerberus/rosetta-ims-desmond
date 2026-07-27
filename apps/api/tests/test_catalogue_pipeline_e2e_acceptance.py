"""CIS-104 end-to-end catalogue pipeline acceptance coverage."""

from __future__ import annotations

import json
import os
import re
import tempfile
from decimal import Decimal
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
import main  # noqa: E402
import models  # noqa: E402
from dependencies import require_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from orchestration.catalogue_flows import catalogue_ingestion_flow  # noqa: E402
from orchestration.catalogue_run_lifecycle import claim_queued_run  # noqa: E402
from orchestration.catalogue_types import TerminalRunReplay  # noqa: E402
from schemas.catalogue_pipeline.enums import ExtractionMethod, ReviewStatus  # noqa: E402
from services import catalogue_evidence_extraction as extraction  # noqa: E402
from services import catalogue_pipeline_persistence as persistence  # noqa: E402
from services import catalogue_pipeline_stages as stages  # noqa: E402
from services import extraction_service, tagging_service  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


FIXTURE_TEXT_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "catalogue_pipeline"
    / "e2e"
    / "hills_cis104_acceptance_page1.txt"
)


class _CatalogueOnboardingAdmin:
    id = 104
    username = "cis104-acceptance-admin"
    display_name = "CIS-104 Acceptance Admin"
    role = "admin"


@pytest.fixture(autouse=True)
def _auth_and_no_inline_work(monkeypatch):
    previous_root = main.app.dependency_overrides.get(require_user)
    previous_v2 = main.alias_app.dependency_overrides.get(require_user)
    main.app.dependency_overrides[require_user] = lambda: _CatalogueOnboardingAdmin()
    main.alias_app.dependency_overrides[require_user] = lambda: _CatalogueOnboardingAdmin()
    monkeypatch.setattr(extraction_service, "extract", lambda *a, **k: pytest.fail("submission must not extract"))
    monkeypatch.setattr(tagging_service, "suggest_tags", lambda *a, **k: pytest.fail("submission must not tag"))
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
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CATALOGUE_SUBMISSION_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setenv("CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES", str(1024 * 1024))
    session = database.SessionLocal()
    try:
        _reset(session)
        _seed_supplier(session, 14, "HILLS", "Hill's")
        _seed_product_variant(session)
        yield session
        session.rollback()
        _reset(session)
    finally:
        session.close()


@pytest.fixture()
def client(db):
    return TestClient(main.app)


def test_cis104_vertical_slice_submission_orchestration_approval_publication_and_lineage(
    client,
    db,
    monkeypatch,
):
    source_bytes = _pdf_bytes()

    first_submission = _submit(client, source_bytes, idempotency_key="cis104-submit")
    assert first_submission.status_code == 202, first_submission.text
    first_payload = first_submission.json()
    run_id = UUID(first_payload["ingestion_run_id"])
    status_url = first_payload["status_url"]

    replay_submission = _submit(client, source_bytes, idempotency_key="cis104-submit")
    assert replay_submission.status_code == 202, replay_submission.text
    assert replay_submission.json()["ingestion_run_id"] == str(run_id)

    conflict_submission = _submit(client, _pdf_bytes(label="changed"), idempotency_key="cis104-submit")
    assert conflict_submission.status_code == 409
    assert conflict_submission.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    db.expire_all()
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(run_id)).one()
    assert run.status == "queued"
    assert run.started_at is None
    assert db.query(models.CatalogueImport).count() == 1
    assert db.query(models.CatalogueSourceDocument).count() == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 0
    assert db.query(models.CatalogueNormalizedRow).count() == 0
    assert db.query(models.CatalogueMasteringCandidate).count() == 0

    source = db.query(models.CatalogueSourceDocument).one()
    stored_path = Path(os.environ["CATALOGUE_UPLOAD_DIR"]) / source.source_ref
    assert stored_path.exists()
    assert stored_path.read_bytes() == source_bytes
    assert source.source_checksum
    assert source.supplier_source_contract_id == "hills.price_list.v1"
    assert run.supplier_source_contract_id == source.supplier_source_contract_id

    # Extraction now ALWAYS runs the deterministic vision path: the vision
    # provider is stubbed to emit column-labeled cells, and conformance maps
    # those cells through the supplier contract with no model in the loop.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        extraction,
        "_call_gemini_vision",
        lambda content, *, media_type: extraction._VisionResponse(text=_vision_envelope(_VISION_ROWS)),
    )
    flow_result = catalogue_ingestion_flow(ingestion_run_id=run_id)

    assert flow_result.terminal_status == "completed_with_warnings"
    assert flow_result.rows_extracted == 2
    assert flow_result.raw_observations_created == 2
    assert flow_result.staging_items_created == 2
    assert flow_result.validation_issues_created == 1
    assert flow_result.mastering_candidates_created == 1
    assert flow_result.human_review_required is True

    db.expire_all()
    status = client.get(status_url)
    assert status.status_code == 200, status.text
    status_payload = status.json()
    assert status_payload["status"] == "completed_with_warnings"
    assert status_payload["started_at"] is not None
    assert status_payload["completed_at"] is not None
    assert status_payload["items_extracted"] == 2

    raw_rows = db.query(models.CatalogueExtractedEvidence).order_by(models.CatalogueExtractedEvidence.id).all()
    staging_rows = db.query(models.CatalogueNormalizedRow).order_by(models.CatalogueNormalizedRow.id).all()
    assert len(raw_rows) == 2
    assert len(staging_rows) == 2
    for raw_row in raw_rows:
        raw_contract = persistence.extracted_evidence_to_contract(raw_row)
        # Vision extraction records column-labeled cells, not PDF text lines.
        assert raw_contract.extraction_method == ExtractionMethod.MODEL_VISION
        assert raw_contract.raw_cells
        assert raw_contract.source_location.page_number == 1
        assert raw_contract.source_location.source_object_key.startswith("page:1:obs:")
        assert raw_row.ingestion_run_uuid == str(run_id)

    raw_cells_before_review = {row.raw_observation_uuid: row.raw_cells_json for row in raw_rows}

    candidate = db.query(models.CatalogueMasteringCandidate).one()
    candidate_contract = persistence.mastering_candidate_to_contract(candidate)
    assert candidate_contract.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate_contract.product_variant_resolution.product_family_id is None
    assert candidate_contract.supplier_product_resolution.supplier_id == 14
    assert candidate_contract.supplier_product_resolution.supplier_sku == "10447"

    valid_staging = db.query(models.CatalogueNormalizedRow).filter_by(
        catalogue_item_uuid=candidate.catalogue_item_uuid
    ).one()
    invalid_staging = [row for row in staging_rows if row.catalogue_item_uuid != valid_staging.catalogue_item_uuid][0]
    invalid_staging_contract = persistence.normalized_row_to_contract(invalid_staging)
    assert invalid_staging_contract.raw_fields.supplier_sku == "Q-1"

    issue = db.query(models.CatalogueValidationIssue).one()
    issue_contract = persistence.validation_issue_to_contract(issue)
    assert issue.issue_code == "STAGING_COST_BASIS_UNRESOLVED"
    assert issue.publish_blocking == 1
    assert issue.catalogue_item_uuid == invalid_staging.catalogue_item_uuid
    assert issue.raw_observation_uuid in {row.raw_observation_uuid for row in raw_rows}
    assert issue_contract.raw_value == "By Quote"
    assert issue_contract.review_guidance
    invalid_raw = db.query(models.CatalogueExtractedEvidence).filter_by(
        raw_observation_uuid=issue.raw_observation_uuid
    ).one()
    # The blocking issue is grounded on the vision cells that carried the
    # unresolved cost, not on a PDF text line.
    invalid_raw_contract = persistence.extracted_evidence_to_contract(invalid_raw)
    assert invalid_raw_contract.extraction_method == ExtractionMethod.MODEL_VISION
    invalid_cells = {cell.column_name: cell.raw_value for cell in invalid_raw_contract.raw_cells}
    assert invalid_cells.get("Product Code / 產品編號") == "Q-1"
    assert invalid_cells.get("Gross Wholesale Price / 每箱·罐") == "By Quote"
    with pytest.raises(stages.BlockingValidationIssues):
        stages.MasteringService(db).prepare_candidate(
            stages.PrepareMasteringCandidateCommand(
                catalogue_item_id=UUID(invalid_staging.catalogue_item_uuid),
                idempotency_key="invalid-should-not-master",
            )
        )

    with pytest.raises(stages.PublicationIneligible):
        stages.ServingPublicationService(db).publish(
            stages.PublishServingItemCommand(
                mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
                publication_version="cis104-before-approval",
                idempotency_key="publish-before-approval",
            )
        )

    with pytest.raises(stages.StaleCandidateRevision):
        stages.ReviewDecisionService(db).record_decision(
            stages.RecordReviewDecisionCommand(
                mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
                actor_id="acceptance-reviewer@example.com",
                review_status=ReviewStatus.APPROVED,
                expected_candidate_created_at="2026-01-01T00:00:00+00:00",
                idempotency_key="cis104-stale-approval",
            )
        )

    decision = stages.ReviewDecisionService(db).record_decision(
        stages.RecordReviewDecisionCommand(
            mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
            actor_id="acceptance-reviewer@example.com",
            review_status=ReviewStatus.APPROVED,
            decided_at="2026-07-23T10:00:00+00:00",
            reason="Approved deterministic CIS-104 acceptance fixture.",
            expected_candidate_created_at=candidate.created_at,
            idempotency_key="cis104-approve-valid",
        )
    )
    repeated_decision = stages.ReviewDecisionService(db).record_decision(
        stages.RecordReviewDecisionCommand(
            mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
            actor_id="acceptance-reviewer@example.com",
            review_status=ReviewStatus.APPROVED,
            decided_at="2026-07-23T10:00:00+00:00",
            reason="Approved deterministic CIS-104 acceptance fixture.",
            expected_candidate_created_at=candidate.created_at,
            idempotency_key="cis104-approve-valid",
        )
    )
    assert decision.metrics.created_count == 1
    assert repeated_decision.metrics.reused_count == 1

    applied = stages.ApprovedCommercialStateService(db).apply_approved_candidate(
        stages.ApplyApprovedCandidateCommand(
            mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
            applied_at="2026-07-23T10:01:00+00:00",
        )
    )
    applied_again = stages.ApprovedCommercialStateService(db).apply_approved_candidate(
        stages.ApplyApprovedCandidateCommand(
            mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
            applied_at="2026-07-23T10:01:00+00:00",
        )
    )
    assert applied.metrics.created_count == 1
    assert applied_again.metrics.reused_count == 1

    supplier_product = db.query(models.CatalogueSupplierProduct).one()
    assert supplier_product.product_variant_id == db.query(models.Product).filter_by(sku_code="10447").one().id
    assert supplier_product.supplier_sku == "10447"
    assert supplier_product.product_family_id is None
    price = db.query(models.CatalogueSupplierPrice).one()
    assert price.amount == Decimal("13.1000")
    assert price.currency == "HKD"
    assert price.price_basis_uom_code == "UNIT"
    assert price.ingestion_run_uuid == str(run_id)
    packaging = db.query(models.CataloguePackagingConfiguration).one()
    assert packaging.content_amount == Decimal("82.000000")
    assert packaging.content_uom_code == "G"
    assert packaging.sellable_units_per_purchase_unit is None

    publication = stages.ServingPublicationService(db).publish(
        stages.PublishServingItemCommand(
            mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
            publication_version="cis104-acceptance-v1",
            published_at="2026-07-23T10:02:00+00:00",
            idempotency_key="cis104-publish-valid",
        )
    )
    repeated_publication = stages.ServingPublicationService(db).publish(
        stages.PublishServingItemCommand(
            mastering_candidate_id=UUID(candidate.mastering_candidate_uuid),
            publication_version="cis104-acceptance-v1",
            published_at="2026-07-23T10:02:00+00:00",
            idempotency_key="cis104-publish-valid",
        )
    )
    assert publication.metrics.created_count == 1
    assert repeated_publication.metrics.reused_count == 1
    assert db.query(models.CatalogueServingPublication).count() == 1

    serving_row = db.query(models.CatalogueServingPublication).one()
    serving_contract = persistence.serving_item_to_contract(serving_row)
    assert serving_contract.contract_version == "catalogue.serving_item.v1"
    assert serving_contract.review_status == ReviewStatus.APPROVED
    assert serving_contract.canonical_sku == "10447"
    assert serving_contract.product_variant_name == "Hill's Healthy Cuisine Chicken 82g"
    assert serving_contract.supplier_offering.supplier_sku == "10447"
    assert serving_contract.current_approved_cost.amount == Decimal("13.10")
    assert serving_contract.current_approved_cost.currency == "HKD"
    assert serving_contract.current_approved_cost.price_basis.code.value == "UNIT"
    assert serving_contract.purchasing_packaging.content_amount == Decimal("82")
    assert serving_contract.purchasing_packaging.content_uom.code.value == "G"
    assert serving_contract.lineage.mastering_candidate_id == UUID(candidate.mastering_candidate_uuid)
    assert serving_contract.lineage.catalogue_item_id == UUID(valid_staging.catalogue_item_uuid)
    assert UUID(invalid_staging.catalogue_item_uuid) != serving_contract.lineage.catalogue_item_id

    _assert_served_field_lineage(
        db,
        run_id=run_id,
        source=source,
        serving=serving_contract,
        candidate=candidate,
        staging=valid_staging,
        raw_cells_before_review=raw_cells_before_review,
    )

    replay_flow = catalogue_ingestion_flow(ingestion_run_id=run_id)
    assert replay_flow.terminal_status == "completed_with_warnings"
    assert db.query(models.CatalogueExtractedEvidence).count() == 2
    assert db.query(models.CatalogueNormalizedRow).count() == 2
    assert db.query(models.CatalogueMasteringCandidate).count() == 1
    with pytest.raises(TerminalRunReplay):
        claim_queued_run(db, ingestion_run_id=run_id)


def test_source_grounding_rejects_extraction_values_absent_from_pdf():
    source_bytes = _pdf_bytes()
    rows, _ = _acceptance_rows(b"", "fixture.pdf", "application/pdf")
    mutated = [dict(row) for row in rows]
    mutated[0]["cost_price"] = "99.99"

    with pytest.raises(AssertionError, match="cost_price"):
        _assert_rows_grounded_on_pdf_page(source_bytes, mutated, page_number=1)


def _submit(client: TestClient, source_bytes: bytes, *, idempotency_key: str):
    return client.post(
        "/catalogues/ingestions",
        data={"supplier_id": "14"},
        files={"file": ("hills-cis104.pdf", source_bytes, "application/pdf")},
        headers={"Idempotency-Key": idempotency_key},
    )


# Two vision observations, each a map of Hill's source-column heading -> printed
# cell value. Conformance maps these named cells through the supplier contract:
#   - row 1 is a complete, resolvable Hill's row (SKU 10447). Its normalized name
#     is the contract's composed_from join (product range + product description,
#     life stage omitted), English only: "Hill's Healthy Cuisine Chicken 82g",
#     matching the seeded product variant so approval/publish resolve it.
#   - row 2 quotes "By Quote" for the wholesale price, so cost cannot be resolved
#     and the row raises the blocking STAGING_COST_BASIS_UNRESOLVED issue.
_VISION_ROWS = [
    {
        "Product Code / 產品編號": "10447",
        "Product Range / 產品系列": "Hill's Healthy Cuisine Chicken",
        "Product Description / 產品名稱": "82g",
        "Size / 重量": "82g",
        "Gross Wholesale Price / 每箱·罐": "13.10",
        "Order Multiple / 訂貨單位": "1",
    },
    {
        "Product Code / 產品編號": "Q-1",
        "Product Description / 產品名稱": "Quoted Special Order Item",
        "Gross Wholesale Price / 每箱·罐": "By Quote",
    },
]


def _vision_envelope(rows: list[dict[str, str]]) -> str:
    """Serialize rows as the typed Gemini vision evidence envelope the seam expects."""

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


def _acceptance_rows(_content, _filename, _content_type, contract=None):
    return (
        [
            {
                "description": "Hill's Healthy Cuisine Chicken 82g",
                "brand": "Hill's",
                "category": "Food",
                "supplier_sku": "10447",
                "barcode": "052742104470",
                "cost_price": "13.10",
                "pack_size": "82g",
                "variant": "82g",
                "confidence": "0.96",
                "_raw_text": "10447 Hill's Healthy Cuisine Chicken 82g 82g HKD 13.10",
            },
            {
                "description": "Quoted Special Order Item",
                "brand": "Hill's",
                "category": "Food",
                "supplier_sku": "Q-1",
                "cost_price": "By Quote",
                "pack_size": "1 unit",
                "variant": "special order",
                "confidence": "0.88",
                "_raw_text": "Q-1 Quoted Special Order Item 1 unit HKD By Quote",
            },
        ],
        "pdf",
    )


def _assert_served_field_lineage(
    db,
    *,
    run_id: UUID,
    source: models.CatalogueSourceDocument,
    serving,
    candidate: models.CatalogueMasteringCandidate,
    staging: models.CatalogueNormalizedRow,
    raw_cells_before_review: dict[str, str | None],
) -> None:
    candidate_contract = persistence.mastering_candidate_to_contract(candidate)
    staging_contract = persistence.normalized_row_to_contract(staging)
    raw_ids = [str(raw_id) for raw_id in serving.lineage.raw_observation_ids]
    raw_rows = (
        db.query(models.CatalogueExtractedEvidence)
        .filter(models.CatalogueExtractedEvidence.raw_observation_uuid.in_(raw_ids))
        .all()
    )
    assert raw_rows
    raw_row = raw_rows[0]
    raw_contract = persistence.extracted_evidence_to_contract(raw_row)

    assert serving.canonical_sku == candidate_contract.product_variant_resolution.canonical_sku
    assert serving.supplier_offering.supplier_sku == candidate_contract.supplier_product_resolution.supplier_sku
    assert serving.product_variant_name == candidate_contract.product_variant_resolution.product_variant_name
    assert serving.current_approved_cost.amount == candidate_contract.supplier_price_resolution.current_cost.amount
    assert serving.current_approved_cost.currency == candidate_contract.supplier_price_resolution.current_cost.currency
    assert serving.purchasing_packaging.content_amount == candidate_contract.packaging_resolution.packaging.content_amount

    assert candidate_contract.catalogue_item_id == staging_contract.catalogue_item_id
    assert candidate_contract.raw_observation_ids == staging_contract.raw_observation_ids
    assert staging_contract.normalized_fields.supplier_sku.value == staging_contract.raw_fields.supplier_sku
    assert staging_contract.normalized_fields.cost.amount == Decimal("13.10")
    assert staging_contract.raw_fields.cost == "13.10"

    assert raw_contract.ingestion_run_id == run_id
    assert raw_contract.supplier_catalogue_id == UUID(source.supplier_catalogue_uuid)
    assert raw_contract.source_file_id == UUID(source.source_file_uuid)
    assert raw_contract.source_location.page_number == 1
    # Vision observation keys derive from cell content + location, not text lines.
    assert raw_contract.source_location.source_object_key.startswith("page:1:obs:")
    # The evidence cells are immutable between review and publication.
    assert raw_row.raw_cells_json == raw_cells_before_review[raw_row.raw_observation_uuid]
    # Every served field is grounded in the verbatim vision cells that produced
    # the normalized row (currency/price-basis are contract declarations, not
    # source cells, so they are asserted structurally above, not here).
    assert raw_contract.raw_cells
    cells_text = " ".join(str(cell.raw_value) for cell in raw_contract.raw_cells)
    assert "13.10" in cells_text
    _assert_text_contains(cells_text, serving.canonical_sku)
    _assert_text_contains(cells_text, serving.supplier_offering.supplier_sku)
    # product_variant_name is a DETERMINISTIC composition (brand + name parts +
    # size, de-duplicated) — not a verbatim source substring — so its source tie
    # is asserted structurally (serving == candidate == staging) above. The raw,
    # unnormalized product name it was composed from stays grounded in the cells:
    _assert_text_contains(cells_text, staging_contract.raw_fields.product_name)
    _assert_decimal_grounded(cells_text, serving.current_approved_cost.amount, label="approved cost")
    _assert_text_contains(
        cells_text,
        f"{serving.purchasing_packaging.content_amount.normalize()}"
        f"{serving.purchasing_packaging.content_uom.code.value.lower()}",
    )

    lineage = json.loads(db.query(models.CatalogueServingPublication).one().lineage_json)
    assert lineage["mastering_candidate_id"] == candidate.mastering_candidate_uuid
    assert lineage["catalogue_item_id"] == staging.catalogue_item_uuid
    assert lineage["raw_observation_ids"] == raw_ids


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
    session.query(models.Product).filter_by(sku_code="10447").delete()
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


def _seed_product_variant(session):
    product = session.query(models.Product).filter_by(sku_code="10447").first()
    if product is None:
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
        session.add(product)
        session.commit()
    return product


def _pdf_bytes(*, label: str = "acceptance") -> bytes:
    fixture_text = FIXTURE_TEXT_PATH.read_text()
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    _write_text_to_page(writer, page, fixture_text)
    writer.add_metadata({"/Title": f"CIS-104 {label}"})
    output = BytesIO()
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
    stream.set_data("\n".join(parts).encode("utf-8"))
    page[NameObject("/Contents")] = writer._add_object(stream)


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _assert_rows_grounded_on_pdf_page(pdf_bytes: bytes, rows: list[dict], *, page_number: int) -> None:
    page_text = _pdf_page_text(pdf_bytes, page_number=page_number)
    for index, row in enumerate(rows, start=1):
        raw_text = row.get("_raw_text") or row.get("raw_text")
        assert raw_text, f"row {index} must expose raw evidence text"
        _assert_text_contains(page_text, raw_text, label=f"row {index} raw_text")
        for field_name in ("supplier_sku", "description", "pack_size", "cost_price"):
            value = row.get(field_name)
            if value is not None:
                _assert_text_contains(page_text, str(value), label=f"row {index} {field_name}")


def _pdf_page_text(pdf_bytes: bytes, *, page_number: int) -> str:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    assert 1 <= page_number <= len(reader.pages), f"cited page {page_number} does not exist"
    return reader.pages[page_number - 1].extract_text() or ""


def _assert_text_contains(container: str, expected: str, *, label: str = "source evidence") -> None:
    assert _fold_text(expected) in _fold_text(container), f"{label} not found on cited source page: {expected!r}"


def _assert_decimal_grounded(container: str, expected: Decimal, *, label: str) -> None:
    for match in re.findall(r"\d+(?:\.\d+)?", container):
        if Decimal(match) == expected:
            return
    raise AssertionError(f"{label} not found on cited source page as decimal value: {expected!r}")


def _fold_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()
