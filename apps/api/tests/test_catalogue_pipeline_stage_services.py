"""Catalogue pipeline stage service tests."""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from uuid import UUID

import pytest

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import database  # noqa: E402
import models  # noqa: E402
from schemas.catalogue_pipeline.enums import IssueResolutionStatus, ReviewStatus  # noqa: E402
from services import catalogue_pipeline_stages as stages  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)


RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
SOURCE_ID = UUID("22222222-2222-4222-8222-222222222222")
FILE_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID_2 = UUID("11111111-1111-4111-8111-111111111112")
SOURCE_ID_2 = UUID("22222222-2222-4222-8222-222222222223")
FILE_ID_2 = UUID("33333333-3333-4333-8333-333333333334")


@pytest.fixture()
def db():
    session = database.SessionLocal()
    try:
        _reset(session)
        yield session
        session.rollback()
        _reset(session)
    finally:
        session.close()


def _reset(session):
    for model in (
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
    session.query(models.CatalogueImport).filter(models.CatalogueImport.filename.like("stage-services-%")).delete()
    session.query(models.Product).filter(models.Product.sku_code.in_(("STAGE-SKU-10447", "STAGE-SKU-ALT"))).delete()
    session.commit()


def _seed_context(
    session,
    *,
    run_id: UUID = RUN_ID,
    source_id: UUID = SOURCE_ID,
    file_id: UUID = FILE_ID,
    supplier_id: int = 14,
    contract_id: str = "hills.price_list.v1",
    contract_version: str = "v1",
):
    supplier = session.get(models.Supplier, supplier_id)
    if supplier is None:
        supplier = models.Supplier(
            id=supplier_id,
            code=f"SUP{supplier_id}",
            name="Hill's" if supplier_id == 14 else f"Supplier {supplier_id}",
            created_at="2026-07-23T00:00:00+00:00",
        )
        session.add(supplier)
        session.flush()
    legacy_import = models.CatalogueImport(
        supplier_id=supplier_id,
        filename=f"stage-services-{run_id}.pdf",
        format="pdf",
        imported_at="2026-07-23T00:00:00+00:00",
        status="uploaded",
        item_count=0,
    )
    session.add(legacy_import)
    session.flush()
    source = models.CatalogueSourceDocument(
        supplier_catalogue_uuid=str(source_id),
        source_file_uuid=str(file_id),
        legacy_import_id=legacy_import.id,
        supplier_id=supplier_id,
        filename=legacy_import.filename,
        source_format="PDF",
        received_at="2026-07-23T00:00:00+00:00",
        supplier_source_contract_id=contract_id,
        supplier_source_contract_version=contract_version,
        document_type="PRICE_LIST",
        created_at="2026-07-23T00:00:00+00:00",
    )
    session.add(source)
    session.flush()
    run = models.IngestionRun(
        run_uuid=str(run_id),
        source_document_id=legacy_import.id,
        catalogue_source_document_id=source.id,
        supplier_id=supplier_id,
        supplier_source_contract_id=contract_id,
        supplier_source_contract_version=contract_version,
        document_type="PRICE_LIST",
        extractor_name="stage-test",
        extractor_version="v1",
        status="queued",
        started_at="2026-07-23T00:00:00+00:00",
        created_at="2026-07-23T00:00:00+00:00",
    )
    session.add(run)
    session.commit()
    return run, source


def _raw_input(key="row-1", text='10447 Healthy Cuisine 24/2.9 oz HK$13.10'):
    return stages.ExtractedEvidenceInput(
        idempotency_key=key,
        source_location={"page_number": 1, "source_object_key": key},
        raw_text=text,
        extraction_method="MODEL_TEXT",
        captured_at="2026-07-23T00:01:00+00:00",
        extraction_model="fixture",
        extraction_model_version="v1",
        extraction_confidence="0.96",
        source_metadata={"row_key": key},
    )


def _capture_raw(db, *, run_id=RUN_ID, source_id=SOURCE_ID, file_id=FILE_ID, key="row-1"):
    return stages.ExtractedEvidenceService(db).capture(
        stages.CaptureExtractedEvidenceCommand(
            ingestion_run_id=run_id,
            supplier_catalogue_id=source_id,
            source_file_id=file_id,
            supplier_id=14,
            observations=(_raw_input(key),),
        )
    ).output_ids[0]


def _raw_fields(cost="13.10 HKD per can", packaging="24/2.9 oz"):
    return {
        "supplier_sku": "10447",
        "product_name": "Hill's Healthy Cuisine Chicken 2.9 oz",
        "brand": "Hill's",
        "category": "Food",
        "cost": cost,
        "packaging": packaging,
        "barcode": "052742104470",
        "variant": "2.9 oz",
    }


def _normalized_fields(raw_id: UUID, *, include_cost=True, include_packaging=True):
    evidence = {"raw_observation_id": str(raw_id), "field_path": "/raw_text", "confidence": "0.96"}
    proposed = {
        "supplier_sku": {"value": "10447", "evidence": evidence},
        "product_name": {"value": "Hill's Healthy Cuisine Chicken 2.9 oz", "evidence": evidence},
        "brand": {"value": "Hill's", "evidence": evidence},
        "category": {"value": "Food", "evidence": evidence},
        "barcode": {"value": "052742104470", "evidence": evidence},
        "variant": {"value": "2.9 oz", "evidence": evidence},
        "mbb_terms": [],
    }
    if include_cost:
        proposed["cost"] = {
            "amount": "13.10",
            "currency": "HKD",
            "price_basis": {"code": "CAN"},
            "evidence": evidence,
        }
    if include_packaging:
        proposed["packaging"] = {
            "purchase_uom": {"code": "CASE"},
            "price_basis": {"code": "CAN"},
            "sellable_unit_uom": {"code": "CAN"},
            "sellable_units_per_purchase_unit": "24",
            "content_amount": "82",
            "content_uom": {"code": "G"},
            "order_increment": {"amount": "24", "uom": {"code": "CAN"}},
            "source_text": "24/2.9 oz",
            "evidence": evidence,
        }
    return proposed


def _build_claim(
    db,
    raw_id: UUID,
    *,
    include_cost=True,
    include_packaging=True,
    key="stage-row-1",
    metadata=None,
):
    return stages.NormalizedRowService(db).build_item(
        stages.BuildNormalizedRowCommand(
            raw_observation_ids=(raw_id,),
            raw_fields=_raw_fields(),
            normalized_fields=_normalized_fields(raw_id, include_cost=include_cost, include_packaging=include_packaging),
            idempotency_key=key,
            metadata=metadata or {},
        )
    ).output_ids[0]


def _seed_product(db):
    product = db.query(models.Product).filter_by(sku_code="STAGE-SKU-10447").first()
    if product is None:
        product = models.Product(
            sku_code="STAGE-SKU-10447",
            name="Hill's Healthy Cuisine Chicken 2.9 oz",
            brand="Hill's",
            category="Food",
            storage_rule="any",
            status="ACTIVE",
            created_at="2026-07-23T00:00:00+00:00",
            updated_at="2026-07-23T00:00:00+00:00",
        )
        db.add(product)
        db.commit()
    return product


def _prepare_candidate(db, staging_id: UUID, *, key="candidate-row-1"):
    return stages.MasteringService(db).prepare_candidate(
        stages.PrepareMasteringCandidateCommand(
            catalogue_item_id=staging_id,
            idempotency_key=key,
            supplier_product_resolution={
                "state": "PROPOSED_CREATE",
                "supplier_id": 14,
                "supplier_product_id": "supplier:14:offer:10447",
                "supplier_sku": "10447",
                "barcode": "052742104470",
            },
            product_variant_resolution={
                "state": "PROPOSED_MATCH",
                "product_variant_id": "STAGE-SKU-10447",
                "canonical_sku": "STAGE-SKU-10447",
                "product_variant_name": "Hill's Healthy Cuisine Chicken 2.9 oz",
                "product_family_id": None,
            },
        )
    ).output_ids[0]


def test_raw_capture_uses_supported_contract_and_is_idempotent(db):
    _seed_context(db)

    service = stages.ExtractedEvidenceService(db)
    command = stages.CaptureExtractedEvidenceCommand(
        ingestion_run_id=RUN_ID,
        supplier_catalogue_id=SOURCE_ID,
        source_file_id=FILE_ID,
        supplier_id=14,
        observations=(_raw_input(),),
    )

    first = service.capture(command)
    second = service.capture(command)

    assert first.metrics.created_count == 1
    assert second.metrics.reused_count == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 1
    row = db.query(models.CatalogueExtractedEvidence).one()
    assert row.raw_text == '10447 Healthy Cuisine 24/2.9 oz HK$13.10'
    assert row.extraction_profile_id == "hills.price_list.v1"
    assert row.extraction_confidence == Decimal("0.9600")

    changed = stages.CaptureExtractedEvidenceCommand(
        ingestion_run_id=RUN_ID,
        supplier_catalogue_id=SOURCE_ID,
        source_file_id=FILE_ID,
        supplier_id=14,
        observations=(_raw_input(text="changed text"),),
    )
    with pytest.raises(stages.IdempotencyConflict):
        service.capture(changed)


def test_raw_capture_rejects_unverified_supplier_contract(db):
    _seed_context(
        db,
        supplier_id=91,
        contract_id="vetapet.vet_price_list.v1",
        contract_version="v1",
    )

    with pytest.raises(stages.UnsupportedSupplierContract, match="not SUPPORTED"):
        stages.ExtractedEvidenceService(db).capture(
            stages.CaptureExtractedEvidenceCommand(
                ingestion_run_id=RUN_ID,
                supplier_catalogue_id=SOURCE_ID,
                source_file_id=FILE_ID,
                supplier_id=91,
                observations=(_raw_input(),),
            )
        )


def test_staging_preserves_lineage_and_rejects_cross_run_grouping(db):
    _seed_context(db)
    _seed_context(db, run_id=RUN_ID_2, source_id=SOURCE_ID_2, file_id=FILE_ID_2)
    raw_1 = _capture_raw(db)
    raw_2 = _capture_raw(db, run_id=RUN_ID_2, source_id=SOURCE_ID_2, file_id=FILE_ID_2, key="row-2")

    result = stages.NormalizedRowService(db).build_item(
        stages.BuildNormalizedRowCommand(
            raw_observation_ids=(raw_1,),
            raw_fields=_raw_fields(),
            normalized_fields=_normalized_fields(raw_1),
            idempotency_key="stage-row-1",
        )
    )

    assert result.metrics.created_count == 1
    staging = db.query(models.CatalogueNormalizedRow).one()
    assert staging.raw_fields_json != staging.normalized_fields_json
    assert db.query(models.CatalogueNormalizedRowEvidence).filter_by(raw_observation_uuid=str(raw_1)).count() == 1

    with pytest.raises(stages.MissingOrIncompatibleLineage, match="different ingestion runs"):
        stages.NormalizedRowService(db).build_item(
            stages.BuildNormalizedRowCommand(
                raw_observation_ids=(raw_1, raw_2),
                raw_fields=_raw_fields(),
                normalized_fields=_normalized_fields(raw_1),
                idempotency_key="stage-cross-run",
            )
        )

    with pytest.raises(stages.MissingOrIncompatibleLineage, match="duplicates"):
        stages.NormalizedRowService(db).build_item(
            stages.BuildNormalizedRowCommand(
                raw_observation_ids=(raw_1, raw_1),
                raw_fields=_raw_fields(),
                normalized_fields=_normalized_fields(raw_1),
                idempotency_key="stage-dupe",
            )
        )


def test_validation_dedupes_resolves_and_blocks_mastering_until_resolved(db):
    _seed_context(db)
    raw_id = _capture_raw(db)
    staging_id = _build_claim(db, raw_id, include_cost=False, include_packaging=False)

    validation = stages.CatalogueValidationService(db)
    first = validation.evaluate_claim(stages.EvaluateNormalizedRowCommand(catalogue_item_id=staging_id))
    second = validation.evaluate_claim(stages.EvaluateNormalizedRowCommand(catalogue_item_id=staging_id))

    assert first.metrics.created_count == 2
    assert second.metrics.reused_count == 2
    assert first.metrics.blocking_issue_count == 1
    assert db.query(models.CatalogueValidationIssue).count() == 2
    with pytest.raises(stages.BlockingValidationIssues):
        _prepare_candidate(db, staging_id)

    blocking = (
        db.query(models.CatalogueValidationIssue)
        .filter_by(issue_code="STAGING_COST_BASIS_UNRESOLVED")
        .one()
    )
    validation.resolve_issue(
        stages.ResolveValidationIssueCommand(
            validation_issue_id=UUID(blocking.validation_issue_uuid),
            resolver_id="bizops@example.com",
            resolution_status=IssueResolutionStatus.ACCEPTED_AS_IS,
            resolution_note="Proceeding as a reviewed exception for fixture coverage.",
            idempotency_key="resolve-cost-basis",
        )
    )
    candidate_id = _prepare_candidate(db, staging_id)
    assert candidate_id
    assert db.query(models.CatalogueReviewDecision).filter_by(validation_issue_uuid=blocking.validation_issue_uuid).count() == 1


def test_contract_execution_issues_are_durable_and_resolution_survives_replay(db):
    _seed_context(db)
    raw_id = _capture_raw(db)
    staging_id = _build_claim(
        db,
        raw_id,
        metadata={
            "contract_execution_issues": [
                {
                    "issue_code": "CONTRACT_MBB_REQUIRES_REVIEW",
                    "severity": "WARNING",
                    "message": "Promotion text lacks a proven scope.",
                    "field_key": "mbb_text",
                }
            ]
        },
    )
    validation = stages.CatalogueValidationService(db)

    first = validation.evaluate_claim(stages.EvaluateNormalizedRowCommand(catalogue_item_id=staging_id))
    second = validation.evaluate_claim(stages.EvaluateNormalizedRowCommand(catalogue_item_id=staging_id))

    assert first.metrics.created_count == 1
    assert second.metrics.reused_count == 1
    issue_row = db.query(models.CatalogueValidationIssue).one()
    issue = UUID(issue_row.validation_issue_uuid)
    assert issue_row.issue_code == "CONTRACT_MBB_REQUIRES_REVIEW"
    assert issue_row.field_path == "/normalized_fields/mbb_terms"
    assert issue_row.raw_observation_uuid == str(raw_id)

    validation.resolve_issue(
        stages.ResolveValidationIssueCommand(
            validation_issue_id=issue,
            resolver_id="bizops@example.com",
            resolution_status=IssueResolutionStatus.CONFIRMED,
            resolution_note="Promotion scope was checked against the supplier document.",
            idempotency_key="confirm-mbb-scope",
        )
    )
    replay = validation.evaluate_claim(stages.EvaluateNormalizedRowCommand(catalogue_item_id=staging_id))

    assert replay.metrics.reused_count == 1
    assert replay.metrics.blocking_issue_count == 0
    db.refresh(issue_row)
    assert issue_row.resolution_status == IssueResolutionStatus.CONFIRMED.value
    assert issue_row.resolver_id == "bizops@example.com"
    assert db.query(models.CatalogueValidationIssue).count() == 1


def test_malformed_contract_issue_metadata_fails_closed(db):
    _seed_context(db)
    raw_id = _capture_raw(db)
    staging_id = _build_claim(
        db,
        raw_id,
        metadata={"contract_execution_issues": [{"severity": "NOT_A_SEVERITY"}]},
    )

    result = stages.CatalogueValidationService(db).evaluate_claim(
        stages.EvaluateNormalizedRowCommand(catalogue_item_id=staging_id)
    )

    issue = db.query(models.CatalogueValidationIssue).one()
    assert result.metrics.blocking_issue_count == 1
    assert issue.issue_code == "CONTRACT_ISSUE_METADATA_INVALID"
    assert issue.publish_blocking == 1


def test_stage_services_apply_approved_candidate_and_publish_idempotently(db):
    _seed_context(db)
    _seed_product(db)
    raw_id = _capture_raw(db)
    staging_id = _build_claim(db, raw_id)
    candidate_id = _prepare_candidate(db, staging_id)

    with pytest.raises(stages.PublicationIneligible):
        stages.ServingPublicationService(db).publish(
            stages.PublishServingItemCommand(
                mastering_candidate_id=candidate_id,
                publication_version="2026-07-23T00:10:00Z",
                idempotency_key="publish-before-apply",
            )
        )

    review = stages.ReviewDecisionService(db)
    decision = review.record_decision(
        stages.RecordReviewDecisionCommand(
            mastering_candidate_id=candidate_id,
            actor_id="reviewer@example.com",
            review_status=ReviewStatus.APPROVED,
            decided_at="2026-07-23T00:05:00+00:00",
            reason="Approved fixture candidate.",
            idempotency_key="approve-candidate",
        )
    )
    repeated_decision = review.record_decision(
        stages.RecordReviewDecisionCommand(
            mastering_candidate_id=candidate_id,
            actor_id="reviewer@example.com",
            review_status=ReviewStatus.APPROVED,
            decided_at="2026-07-23T00:05:00+00:00",
            reason="Approved fixture candidate.",
            idempotency_key="approve-candidate",
        )
    )

    assert decision.metrics.created_count == 1
    assert repeated_decision.metrics.reused_count == 1
    applied = stages.ApprovedCommercialStateService(db).apply_approved_candidate(
        stages.ApplyApprovedCandidateCommand(
            mastering_candidate_id=candidate_id,
            applied_at="2026-07-23T00:06:00+00:00",
        )
    )
    applied_again = stages.ApprovedCommercialStateService(db).apply_approved_candidate(
        stages.ApplyApprovedCandidateCommand(
            mastering_candidate_id=candidate_id,
            applied_at="2026-07-23T00:06:00+00:00",
        )
    )

    assert applied.metrics.created_count == 1
    assert applied_again.metrics.reused_count == 1
    supplier_product = db.query(models.CatalogueSupplierProduct).one()
    assert supplier_product.product_family_id is None
    assert supplier_product.supplier_product_key == "supplier:14:offer:10447"
    price = db.query(models.CatalogueSupplierPrice).one()
    assert price.amount == Decimal("13.1000")
    assert price.price_basis_uom_code == "CAN"
    packaging = db.query(models.CataloguePackagingConfiguration).one()
    assert packaging.sellable_units_per_purchase_unit == Decimal("24.000000")
    assert packaging.content_amount == Decimal("82.000000")
    assert packaging.content_uom_code == "G"

    publisher = stages.ServingPublicationService(db)
    publication = publisher.publish(
        stages.PublishServingItemCommand(
            mastering_candidate_id=candidate_id,
            publication_version="2026-07-23T00:10:00Z",
            published_at="2026-07-23T00:10:00+00:00",
            idempotency_key="publish-candidate",
        )
    )
    repeated_publication = publisher.publish(
        stages.PublishServingItemCommand(
            mastering_candidate_id=candidate_id,
            publication_version="2026-07-23T00:10:00Z",
            published_at="2026-07-23T00:10:00+00:00",
            idempotency_key="publish-candidate",
        )
    )

    assert publication.metrics.created_count == 1
    assert repeated_publication.metrics.reused_count == 1
    serving = db.query(models.CatalogueServingPublication).one()
    assert serving.review_status == "APPROVED"
    assert serving.cost_per_sellable_unit_amount == Decimal("13.1000")
    assert serving.is_current == 1


def test_review_rejects_stale_candidate_revision_and_staging_key_conflicts(db):
    _seed_context(db)
    raw_id = _capture_raw(db)
    staging_id = _build_claim(db, raw_id)
    candidate_id = _prepare_candidate(db, staging_id)

    with pytest.raises(stages.StaleCandidateRevision):
        stages.ReviewDecisionService(db).record_decision(
            stages.RecordReviewDecisionCommand(
                mastering_candidate_id=candidate_id,
                actor_id="reviewer@example.com",
                review_status=ReviewStatus.APPROVED,
                expected_candidate_created_at="2026-01-01T00:00:00+00:00",
                idempotency_key="stale-approval",
            )
        )

    with pytest.raises(stages.IdempotencyConflict):
        stages.NormalizedRowService(db).build_item(
            stages.BuildNormalizedRowCommand(
                raw_observation_ids=(raw_id,),
                raw_fields={**_raw_fields(), "product_name": "Changed"},
                normalized_fields=_normalized_fields(raw_id),
                idempotency_key="stage-row-1",
            )
        )


# ── Stage 4: extracted-evidence persistence boundary ───────────────────────

from services import catalogue_pipeline_persistence as persistence  # noqa: E402


def _cell_input(key, *, column_name, raw_value, row_number, page=1):
    return stages.ExtractedEvidenceInput(
        idempotency_key=key,
        source_location={"page_number": page, "source_object_key": key},
        raw_cells=(
            {
                "cell_reference": f"{column_name}{row_number}",
                "row_number": row_number,
                "column_name": column_name,
                "column_index": 1,
                "raw_value": raw_value,
            },
            {
                "cell_reference": f"B{row_number}",
                "row_number": row_number,
                "column_name": "Empty",
                "column_index": 2,
                "raw_value": "",
            },
        ),
        extraction_method="MODEL_VISION",
        extraction_model="claude-haiku-4-5-20251001",
        extraction_model_version="claude-haiku-4-5-20251001",
        extraction_confidence="0.91",
        source_metadata={"provider_request_id": "msg_batch_1", "observation_key": key},
    )


def test_stage4_persists_verbatim_evidence_metadata_and_lineage(db):
    _seed_context(db)

    result = stages.ExtractedEvidenceService(db).capture(
        stages.CaptureExtractedEvidenceCommand(
            ingestion_run_id=RUN_ID,
            supplier_catalogue_id=SOURCE_ID,
            source_file_id=FILE_ID,
            supplier_id=14,
            observations=(_cell_input("page:1:obs:aa:1", column_name="A", raw_value="13.10", row_number=1),),
        )
    )

    row = db.query(models.CatalogueExtractedEvidence).one()
    contract = persistence.extracted_evidence_to_contract(row)
    # Lineage survives persistence + reconstruction.
    assert contract.ingestion_run_id == RUN_ID
    assert contract.supplier_catalogue_id == SOURCE_ID
    assert contract.source_file_id == FILE_ID
    assert contract.raw_observation_id == result.output_ids[0]
    # Provider/model metadata retained.
    assert contract.extraction_model == "claude-haiku-4-5-20251001"
    assert contract.source_metadata.get("provider_request_id") == "msg_batch_1"
    # Cells preserved verbatim, including the empty cell that is part of the row.
    values = [(cell.column_name, cell.raw_value) for cell in contract.raw_cells]
    assert values == [("A", "13.10"), ("Empty", "")]
    # No interpreted/canonical business fields leaked onto the evidence record.
    dumped = contract.model_dump()
    for semantic in ("cost", "currency", "price_basis", "supplier_sku", "product_name", "packaging"):
        assert semantic not in dumped


def test_stage4_keeps_duplicate_rows_at_different_locations_distinct(db):
    _seed_context(db)

    result = stages.ExtractedEvidenceService(db).capture(
        stages.CaptureExtractedEvidenceCommand(
            ingestion_run_id=RUN_ID,
            supplier_catalogue_id=SOURCE_ID,
            source_file_id=FILE_ID,
            supplier_id=14,
            observations=(
                _raw_input(key="page:1:line:5", text="10447 Chicken 82g HK$13.10"),
                _raw_input(key="page:1:line:9", text="10447 Chicken 82g HK$13.10"),
            ),
        )
    )

    # Byte-identical supplier rows at different source locations remain two
    # distinct persisted observations — never deduplicated by text.
    assert result.metrics.created_count == 2
    assert len(set(result.output_ids)) == 2
    texts = [row.raw_text for row in db.query(models.CatalogueExtractedEvidence).all()]
    assert texts == ["10447 Chicken 82g HK$13.10", "10447 Chicken 82g HK$13.10"]


def test_stage4_batch_is_atomic_no_partial_persistence_on_failure(db):
    _seed_context(db)

    # Second observation in the batch is structurally invalid (bad source
    # location) and raises during contract construction.
    bad = stages.ExtractedEvidenceInput(
        idempotency_key="page:1:line:2",
        source_location={"page_number": "not-an-int", "source_object_key": "page:1:line:2"},
        raw_text="10448 Second HK$14.00",
        extraction_method="MODEL_TEXT",
    )
    with pytest.raises(Exception):
        stages.ExtractedEvidenceService(db).capture(
            stages.CaptureExtractedEvidenceCommand(
                ingestion_run_id=RUN_ID,
                supplier_catalogue_id=SOURCE_ID,
                source_file_id=FILE_ID,
                supplier_id=14,
                observations=(_raw_input(key="page:1:line:1", text="10447 First HK$13.10"), bad),
            )
        )

    db.rollback()
    # The earlier observation in the same batch must not remain committed.
    assert db.query(models.CatalogueExtractedEvidence).count() == 0


def test_stage4_replay_reuses_observations_and_material_conflict_is_controlled(db):
    _seed_context(db)
    command = stages.CaptureExtractedEvidenceCommand(
        ingestion_run_id=RUN_ID,
        supplier_catalogue_id=SOURCE_ID,
        source_file_id=FILE_ID,
        supplier_id=14,
        observations=(_raw_input(key="page:1:obs:zz:1", text="10447 Chicken HK$13.10"),),
    )
    service = stages.ExtractedEvidenceService(db)

    first = service.capture(command)
    replay = service.capture(command)
    assert first.output_ids == replay.output_ids
    assert replay.metrics.reused_count == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 1

    # Same identity, materially different evidence -> controlled conflict.
    conflict = stages.CaptureExtractedEvidenceCommand(
        ingestion_run_id=RUN_ID,
        supplier_catalogue_id=SOURCE_ID,
        source_file_id=FILE_ID,
        supplier_id=14,
        observations=(_raw_input(key="page:1:obs:zz:1", text="10447 Chicken HK$99.99"),),
    )
    with pytest.raises(stages.IdempotencyConflict):
        service.capture(conflict)


# ── Fix 2: replay-safe attempt metadata through the REAL mapping path ───────

from decimal import Decimal as _Decimal  # noqa: E402

from orchestration.catalogue_stage_adapter import raw_input_from_extracted_evidence  # noqa: E402
from schemas.catalogue_pipeline.extracted_evidence_v1 import BoundingBox, SourceLocation  # noqa: E402
from services.catalogue_evidence_extraction import ExtractedEvidence  # noqa: E402


def _vision_evidence(
    *,
    key: str = "page:1:obs:abc123def456:1",
    text: str = "SCANNED-1 | Scanned Product 500g | HK$99.00",
    request_id: str = "msg_a",
    confidence: str = "0.91",
    page: int = 1,
    box: tuple = (5, 40, 300, 20),
    warnings: tuple = (),
) -> ExtractedEvidence:
    return ExtractedEvidence(
        observation_key=key,
        source_location=SourceLocation(
            page_number=page,
            bounding_box=BoundingBox(x=box[0], y=box[1], width=box[2], height=box[3], unit="px"),
            source_object_key=key,
        ),
        raw_text=text,
        extraction_method="MODEL_VISION",
        provider="anthropic",
        provider_request_id=request_id,
        model="claude-haiku-4-5-20251001",
        model_version="claude-haiku-4-5-20251001",
        confidence=_Decimal(confidence),
        warnings=warnings,
    )


def _capture_evidence(db, evidence_items):
    return stages.ExtractedEvidenceService(db).capture(
        stages.CaptureExtractedEvidenceCommand(
            ingestion_run_id=RUN_ID,
            supplier_catalogue_id=SOURCE_ID,
            source_file_id=FILE_ID,
            supplier_id=14,
            observations=tuple(raw_input_from_extracted_evidence(item) for item in evidence_items),
        )
    )


def test_replay_with_new_provider_request_id_reuses_the_immutable_observation(db):
    _seed_context(db)

    first = _capture_evidence(db, [_vision_evidence(request_id="msg_a", confidence="0.91")])
    assert first.metrics.created_count == 1

    # Same evidence, new attempt: different request id, confidence and warnings.
    replay = _capture_evidence(
        db,
        [_vision_evidence(request_id="msg_b", confidence="0.87", warnings=("provider retried",))],
    )

    assert replay.metrics.reused_count == 1
    assert replay.metrics.created_count == 0
    assert replay.output_ids == first.output_ids
    assert db.query(models.CatalogueExtractedEvidence).count() == 1

    # The first-persisted observation is immutable: original request id and
    # confidence are retained; the replay's values are never written over it.
    row = db.query(models.CatalogueExtractedEvidence).one()
    contract = persistence.extracted_evidence_to_contract(row)
    assert contract.source_metadata["provider_request_id"] == "msg_a"
    assert contract.extraction_confidence == _Decimal("0.91")


def test_changed_raw_text_under_same_identity_is_a_controlled_conflict(db):
    _seed_context(db)
    _capture_evidence(db, [_vision_evidence(text="SCANNED-1 | Scanned Product 500g | HK$99.00")])

    with pytest.raises(stages.IdempotencyConflict):
        _capture_evidence(db, [_vision_evidence(text="SCANNED-1 | Scanned Product 500g | HK$1.00")])


def test_bounding_box_and_location_changes_are_material(db):
    _seed_context(db)
    _capture_evidence(db, [_vision_evidence()])

    # Real provider output would mint a different content+location digest for
    # a moved row; a changed bounding box is therefore a DISTINCT observation
    # identity, never a silent merge.
    moved = _vision_evidence(key="page:1:obs:abc123def456:2", box=(5, 400, 300, 20))
    result = _capture_evidence(db, [moved])
    assert result.metrics.created_count == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 2

    # Same text on a different page is likewise distinct.
    other_page = _vision_evidence(key="page:2:obs:abc123def456:1", page=2)
    result = _capture_evidence(db, [other_page])
    assert result.metrics.created_count == 1
    assert db.query(models.CatalogueExtractedEvidence).count() == 3


def test_reordered_replay_batch_is_fully_reused(db):
    _seed_context(db)
    row_a = _vision_evidence(key="page:1:obs:aaaa:1", text="ROW-A | Product A | HK$1.00", box=(5, 40, 300, 20))
    row_b = _vision_evidence(key="page:1:obs:bbbb:1", text="ROW-B | Product B | HK$2.00", box=(5, 70, 300, 20))

    first = _capture_evidence(db, [row_a, row_b])
    assert first.metrics.created_count == 2

    replay = _capture_evidence(
        db,
        [
            _vision_evidence(key="page:1:obs:bbbb:1", text="ROW-B | Product B | HK$2.00", box=(5, 70, 300, 20), request_id="msg_z"),
            _vision_evidence(key="page:1:obs:aaaa:1", text="ROW-A | Product A | HK$1.00", box=(5, 40, 300, 20), request_id="msg_z"),
        ],
    )

    assert replay.metrics.reused_count == 2
    assert replay.metrics.created_count == 0
    assert set(replay.output_ids) == set(first.output_ids)
    assert db.query(models.CatalogueExtractedEvidence).count() == 2


# ── Intermediate 5-6: persisted-evidence handoff, atomic claims, claim replay ─

import copy  # noqa: E402

from orchestration.catalogue_stage_adapter import evidence_from_persisted_observation  # noqa: E402
from orchestration.catalogue_tasks import build_normalized_rows_task  # noqa: E402
from services.catalogue_conformance import ConformanceOutcome, ConformedRow  # noqa: E402


def test_interpretation_input_reconstructs_faithfully_from_persisted_evidence(db):
    _seed_context(db)
    raw_id = _capture_raw(db, key="page:1:line:7")

    row = db.query(models.CatalogueExtractedEvidence).one()
    contract = persistence.extracted_evidence_to_contract(row)
    evidence = evidence_from_persisted_observation(contract)

    assert evidence.observation_key == "page:1:line:7"  # from persisted source_metadata/location
    assert evidence.raw_text == '10447 Healthy Cuisine 24/2.9 oz HK$13.10'
    assert evidence.extraction_method.value == "MODEL_TEXT"
    assert evidence.confidence == Decimal("0.96")
    assert evidence.source_metadata.get("row_key") == "page:1:line:7"


def test_claim_batch_is_atomic_no_partial_claims_on_failure(db):
    _seed_context(db)
    raw_id = _capture_raw(db, key="claim-batch-1")

    good = ConformedRow(
        observation_key="claim-batch-1",
        raw_observation_id=raw_id,
        raw_fields=_raw_fields(),
        normalized_fields=_normalized_fields(raw_id),
        provenance={"interpreter": "model"},
    )
    bad = ConformedRow(
        observation_key="claim-batch-2",
        raw_observation_id=raw_id,
        raw_fields=_raw_fields(),
        normalized_fields={"cost": {"amount": "not-a-number", "currency": "HKD"}},
        provenance={"interpreter": "model"},
    )

    with pytest.raises(Exception):
        build_normalized_rows_task.fn(ConformanceOutcome(items=(good, bad)))

    assert db.query(models.CatalogueNormalizedRow).count() == 0


def test_claim_replay_with_confidence_drift_reuses_the_immutable_first_claim(db):
    _seed_context(db)
    raw_id = _capture_raw(db, key="claim-replay-1")
    service = stages.NormalizedRowService(db)

    def _command(confidence: str):
        proposed = copy.deepcopy(_normalized_fields(raw_id))
        for value in proposed.values():
            if isinstance(value, dict) and isinstance(value.get("evidence"), dict):
                value["evidence"]["confidence"] = confidence
        return stages.BuildNormalizedRowCommand(
            raw_observation_ids=(raw_id,),
            raw_fields=_raw_fields(),
            normalized_fields=proposed,
            idempotency_key="claim-replay-1",
            metadata={"source_observation_key": "claim-replay-1", "interpretation": {"interpreter": "model"}},
        )

    first = service.build_item(_command("0.96"))
    replay = service.build_item(_command("0.87"))

    assert first.output_ids == replay.output_ids
    assert replay.metrics.reused_count == 1
    assert db.query(models.CatalogueNormalizedRow).count() == 1
    # Genuine proposal drift under the same identity stays a controlled conflict.
    drifted = copy.deepcopy(_normalized_fields(raw_id))
    drifted["product_name"]["value"] = "A Different Product Name"
    with pytest.raises(stages.IdempotencyConflict):
        service.build_item(
            stages.BuildNormalizedRowCommand(
                raw_observation_ids=(raw_id,),
                raw_fields=_raw_fields(),
                normalized_fields=drifted,
                idempotency_key="claim-replay-1",
                metadata={"source_observation_key": "claim-replay-1", "interpretation": {"interpreter": "model"}},
            )
        )
