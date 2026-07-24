"""Catalogue logical persistence foundation tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")

import database  # noqa: E402
import models  # noqa: E402
from schemas.catalogue_pipeline import (  # noqa: E402
    MasteringCandidateV1,
    ExtractedEvidenceV1,
    ServingItemV1,
    InterpretedClaimV1,
    ValidationIssueV1,
)
from services import catalogue_pipeline_persistence as persistence  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_catalogue_pipeline_migration import collect_catalogue_migration_audit  # noqa: E402


models.Base.metadata.create_all(bind=database.engine)
database.seed_category_rules(database.engine)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "catalogue_pipeline" / "v1" / "valid"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _model_json(model) -> dict:
    return json.loads(model.model_dump_json())


@pytest.fixture()
def db():
    session = database.SessionLocal()
    try:
        _reset(session)
        _cleanup_legacy_fixture_rows(session)
        yield session
        session.rollback()
        _reset(session)
        _cleanup_legacy_fixture_rows(session)
    finally:
        session.close()


def _reset(session) -> None:
    for model in (
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
    session.commit()


def _cleanup_legacy_fixture_rows(session) -> None:
    audit_supplier = session.get(models.Supplier, 914)
    if audit_supplier is not None:
        audit_links = session.query(models.ProductSupplier).filter_by(supplier_id=914).all()
        for link in audit_links:
            session.query(models.MbbTerm).filter_by(product_supplier_id=link.id).delete()
        session.query(models.ProductSupplier).filter_by(supplier_id=914).delete()
        product = session.query(models.Product).filter_by(sku_code="AUDIT-SKU").first()
        if product is not None:
            session.delete(product)
        session.delete(audit_supplier)
    session.query(models.CatalogueImport).filter_by(
        filename="hills.pdf",
        imported_at="2026-07-22T00:00:00+00:00",
    ).delete()
    session.commit()


def _seed_context(session) -> None:
    raw = _load("raw_observation_pdf.json")
    supplier = session.get(models.Supplier, 14)
    if supplier is None:
        supplier = models.Supplier(id=14, code="HILLS", name="Hill's", created_at="2026-07-22T00:00:00+00:00")
        session.add(supplier)
        session.flush()
    product = session.query(models.Product).filter_by(sku_code="10010385").first()
    if product is None:
        product = models.Product(
            sku_code="10010385",
            name="Hill's Science Diet Adult Chicken 2.9 oz",
            brand="Hill's",
            category="Food",
            storage_rule="any",
            status="ACTIVE",
            created_at="2026-07-22T00:00:00+00:00",
            updated_at="2026-07-22T00:00:00+00:00",
        )
        session.add(product)
    catalogue_import = models.CatalogueImport(
        supplier_id=14,
        filename="hills.pdf",
        format="pdf",
        imported_at="2026-07-22T00:00:00+00:00",
        status="review",
        item_count=1,
    )
    session.add(catalogue_import)
    session.flush()
    source_document = models.CatalogueSourceDocument(
        supplier_catalogue_uuid=raw["supplier_catalogue_id"],
        source_file_uuid=raw["source_file_id"],
        legacy_import_id=catalogue_import.id,
        supplier_id=14,
        filename="hills.pdf",
        source_format="PDF_TABLE",
        source_ref="catalogue_uploads/hills-2024.pdf",
        received_at="2026-07-22T00:00:00+00:00",
        supplier_source_contract_id="hills.price_list.v1",
        supplier_source_contract_version="v1",
        document_type="PRICE_LIST",
        created_at="2026-07-22T00:00:00+00:00",
    )
    session.add(source_document)
    session.flush()
    run = models.IngestionRun(
        run_uuid=raw["ingestion_run_id"],
        source_document_id=catalogue_import.id,
        catalogue_source_document_id=source_document.id,
        supplier_id=14,
        contract_version="catalogue.extraction_profile.v1",
        supplier_source_contract_id="hills.price_list.v1",
        supplier_source_contract_version="v1",
        document_type="PRICE_LIST",
        extractor_name="fixture-extractor",
        extractor_version="v1",
        started_at="2026-07-22T00:00:00+00:00",
        created_at="2026-07-22T00:00:00+00:00",
    )
    session.add(run)
    session.commit()


def _persist_happy_path(session):
    _seed_context(session)
    raw = ExtractedEvidenceV1.model_validate(_load("raw_observation_pdf.json"))
    staging = InterpretedClaimV1.model_validate(_load("staging_item_with_mbb.json"))
    issue = ValidationIssueV1.model_validate(_load("validation_issue_ambiguous_cost_basis.json"))
    candidate = MasteringCandidateV1.model_validate(_load("mastering_candidate_no_family.json"))
    serving = ServingItemV1.model_validate(_load("serving_item_inventory.json"))

    raw_row = persistence.persist_extracted_evidence(session, raw)
    staging_row = persistence.persist_interpreted_claim(session, staging)
    issue_row = persistence.persist_validation_issue(session, issue)
    candidate_row = persistence.persist_mastering_candidate(session, candidate)
    serving_row = persistence.persist_serving_item(session, serving)
    session.commit()
    return raw, raw_row, staging, staging_row, issue, issue_row, candidate, candidate_row, serving, serving_row


def test_fresh_schema_contains_logical_persistence_tables_and_indexes():
    inspector = sa.inspect(database.engine)
    tables = set(inspector.get_table_names())

    assert {
        "catalogue_source_documents",
        "catalogue_extracted_evidence",
        "catalogue_interpreted_claims",
        "catalogue_validation_issues",
        "catalogue_mastering_candidates",
        "catalogue_review_decisions",
        "catalogue_supplier_products",
        "catalogue_packaging_configurations",
        "catalogue_supplier_prices",
        "catalogue_supplier_mbb_terms",
        "catalogue_serving_publications",
    } <= tables
    assert {"run_uuid", "supplier_source_contract_id", "supplier_source_contract_version"} <= {
        column["name"] for column in inspector.get_columns("catalogue_ingestion_runs")
    }
    assert any(index["name"] == "ix_validation_issues_blocking" for index in inspector.get_indexes("catalogue_validation_issues"))


def test_sqlite_foreign_key_enforcement_for_pipeline_models(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fk_enabled.db'}")

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    models.Base.metadata.create_all(bind=engine)
    database.seed_category_rules(engine)

    with engine.begin() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    """
                    INSERT INTO catalogue_extracted_evidence (
                        raw_observation_uuid,
                        contract_version,
                        ingestion_run_uuid,
                        supplier_catalogue_uuid,
                        source_file_uuid,
                        extraction_profile_id,
                        extraction_profile_version,
                        source_location_json,
                        page_number,
                        raw_text,
                        extraction_method,
                        captured_at,
                        created_at
                    )
                    VALUES (
                        '99999999-9999-4999-8999-999999999999',
                        'catalogue.extracted_evidence.v1',
                        '11111111-1111-4111-8111-111111111111',
                        '22222222-2222-4222-8222-222222222222',
                        '33333333-3333-4333-8333-333333333333',
                        'hills.price_list',
                        'v1',
                        '{"page_number": 1}',
                        1,
                        'orphan evidence',
                        'OCR',
                        '2026-07-22T00:00:00+00:00',
                        '2026-07-22T00:00:00+00:00'
                    )
                    """
                )
            )


def test_migration_audit_reports_legacy_rows_without_mutating(db):
    supplier = db.get(models.Supplier, 914)
    if supplier is None:
        supplier = models.Supplier(id=914, code="AUDIT914", name="Audit Supplier", created_at="2026-07-22T00:00:00+00:00")
        db.add(supplier)
        db.flush()
    product = db.query(models.Product).filter_by(sku_code="AUDIT-SKU").first()
    if product is None:
        product = models.Product(
            sku_code="AUDIT-SKU",
            name="Audit Product",
            category="Food",
            storage_rule="any",
            status="ACTIVE",
            created_at="2026-07-22T00:00:00+00:00",
            updated_at="2026-07-22T00:00:00+00:00",
        )
        db.add(product)
        db.flush()
    product_supplier = (
        db.query(models.ProductSupplier)
        .filter_by(product_id=product.id, supplier_id=supplier.id)
        .first()
    )
    if product_supplier is None:
        product_supplier = models.ProductSupplier(
            product_id=product.id,
            supplier_id=supplier.id,
            supplier_sku="AUD-1",
            basic_cost=12.34,
            units_per_pack=24,
            updated_at="2026-07-22T00:00:00+00:00",
        )
        db.add(product_supplier)
        db.flush()
    db.add(
        models.MbbTerm(
            product_supplier_id=product_supplier.id,
            kind="tier",
            min_qty=12,
            unit_cost=11.00,
            sort_order=0,
            created_at="2026-07-22T00:00:00+00:00",
        )
    )
    db.commit()

    before_imports = db.query(models.CatalogueImport).count()
    report = collect_catalogue_migration_audit(database.engine)
    after_imports = db.query(models.CatalogueImport).count()

    assert before_imports == after_imports
    assert report["rejected_unmappable"]["automatic_corrections_attempted"] == 0
    assert report["review_required"]["cost_rows_without_basis_or_review_lineage"] >= 1
    assert report["review_required"]["packaging_rows_requiring_semantic_confirmation"] >= 1
    assert report["review_required"]["legacy_mbb_terms_requiring_condition_benefit_mapping"] >= 1
    assert "pipeline_persisted" in report


def test_pipeline_contracts_round_trip_through_persistence(db):
    raw, raw_row, staging, staging_row, issue, issue_row, candidate, candidate_row, serving, serving_row = _persist_happy_path(db)

    assert _model_json(persistence.extracted_evidence_to_contract(raw_row)) == _model_json(raw)
    assert _model_json(persistence.interpreted_claim_to_contract(staging_row)) == _model_json(staging)
    assert _model_json(persistence.validation_issue_to_contract(issue_row)) == _model_json(issue)
    assert _model_json(persistence.mastering_candidate_to_contract(candidate_row)) == _model_json(candidate)
    assert _model_json(persistence.serving_item_to_contract(serving_row)) == _model_json(serving)

    assert raw_row.extraction_confidence == Decimal("0.9600")
    assert db.query(models.CatalogueReviewDecision).filter_by(review_decision_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").count() == 1


def test_cross_run_extracted_evidence_lineage_is_rejected(db):
    _seed_context(db)
    raw = ExtractedEvidenceV1.model_validate(_load("raw_observation_pdf.json"))
    persistence.persist_extracted_evidence(db, raw)
    db.commit()

    staging_payload = _load("staging_item_with_mbb.json")
    staging_payload["trace"]["ingestion_run_id"] = "11111111-1111-4111-8111-111111111112"
    staging = InterpretedClaimV1.model_validate(staging_payload)

    with pytest.raises(persistence.CatalogueLineageError, match="cannot cross ingestion runs"):
        persistence.persist_interpreted_claim(db, staging)


def test_open_blocking_issue_prevents_mastering_approval_and_publication(db):
    _seed_context(db)
    raw = ExtractedEvidenceV1.model_validate(_load("raw_observation_pdf.json"))
    staging = InterpretedClaimV1.model_validate(_load("staging_item_with_mbb.json"))
    persistence.persist_extracted_evidence(db, raw)
    persistence.persist_interpreted_claim(db, staging)

    issue_payload = _load("validation_issue_ambiguous_cost_basis.json")
    issue_payload["severity"] = "BLOCKING"
    blocking_issue = ValidationIssueV1.model_validate(issue_payload)
    persistence.persist_validation_issue(db, blocking_issue)

    candidate = MasteringCandidateV1.model_validate(_load("mastering_candidate_no_family.json"))
    with pytest.raises(persistence.CataloguePublicationError, match="Open blocking validation issue"):
        persistence.persist_mastering_candidate(db, candidate)


def test_approved_serving_item_persists_commercial_history_and_supersedes(db):
    *_, serving, first_publication = _persist_happy_path(db)

    supplier_product = db.query(models.CatalogueSupplierProduct).one()
    assert supplier_product.product_family_id is None
    price = db.query(models.CatalogueSupplierPrice).filter_by(supplier_product_id=supplier_product.id, is_current=1).one()
    assert price.amount == Decimal("13.1000")
    assert price.currency == "HKD"
    assert price.price_basis_uom_code == "CAN"
    packaging = db.query(models.CataloguePackagingConfiguration).filter_by(supplier_product_id=supplier_product.id).one()
    assert packaging.sellable_units_per_purchase_unit == Decimal("24.000000")
    assert packaging.content_amount == Decimal("82.000000")
    assert packaging.content_uom_code == "G"
    assert packaging.order_increment_amount == Decimal("24.000000")
    mbb = db.query(models.CatalogueSupplierMbbTerm).filter_by(supplier_product_id=supplier_product.id).one()
    assert mbb.condition_type == "minimum_quantity"
    assert mbb.benefit_type == "discounted_unit_price"
    assert mbb.discounted_price_amount == Decimal("12.0000")

    second_payload = _model_json(serving)
    second_payload["serving_item_id"] = "88888888-8888-4888-8888-888888888889"
    second_payload["published_at"] = "2026-07-23T02:00:00Z"
    second_payload["lineage"]["publication_version"] = "2026-07-23T02:00:00Z"
    second = ServingItemV1.model_validate(second_payload)
    second_publication = persistence.persist_serving_item(db, second)
    db.commit()

    db.refresh(first_publication)
    assert first_publication.is_current == 0
    assert first_publication.superseded_at == "2026-07-23T02:00:00+00:00"
    assert second_publication.is_current == 1
    assert db.query(models.CatalogueSupplierPrice).filter_by(supplier_product_id=supplier_product.id, is_current=1).count() == 1


def test_database_constraints_reject_invalid_commercial_values(db):
    _seed_context(db)
    supplier_product = models.CatalogueSupplierProduct(
        supplier_product_key="supplier:14:offer:bad-price",
        supplier_id=14,
        supplier_sku="BAD",
        created_at="2026-07-22T00:00:00+00:00",
    )
    db.add(supplier_product)
    db.flush()
    db.add(
        models.CatalogueSupplierPrice(
            supplier_product_id=supplier_product.id,
            amount=Decimal("-1.00"),
            currency="HKD",
            price_basis_uom_code="CAN",
            created_at="2026-07-22T00:00:00+00:00",
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()
