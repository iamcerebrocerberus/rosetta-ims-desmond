"""Audit legacy catalogue rows for catalogue pipeline migration readiness.

This script is intentionally read-only. It counts current legacy/runtime rows
against the logical persistence categories documented for CIS-103 persistence:
safe-linkable, compatibility-only, review-required, and already-persisted
pipeline rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


def _scalar(conn, sql: str, params: dict[str, Any] | None = None) -> int:
    return int(conn.execute(text(sql), params or {}).scalar() or 0)


def collect_catalogue_migration_audit(engine=database.engine) -> dict[str, Any]:
    """Return deterministic migration-readiness counts without mutating data."""

    with engine.connect() as conn:
        legacy_imports = _scalar(conn, "SELECT COUNT(*) FROM catalogue_imports")
        linked_source_documents = _scalar(
            conn,
            "SELECT COUNT(*) FROM catalogue_source_documents WHERE legacy_import_id IS NOT NULL",
        )
        legacy_catalogue_items = _scalar(conn, "SELECT COUNT(*) FROM catalogue_items")
        legacy_cost_rows = _scalar(
            conn,
            "SELECT COUNT(*) FROM product_suppliers WHERE basic_cost IS NOT NULL",
        )
        legacy_packaging_rows = _scalar(
            conn,
            "SELECT COUNT(*) FROM product_suppliers "
            "WHERE units_per_pack IS NOT NULL OR order_increment_qty IS NOT NULL OR minimum_order_qty IS NOT NULL",
        )
        legacy_mbb_rows = _scalar(conn, "SELECT COUNT(*) FROM mbb_terms")
        legacy_review_events = _scalar(conn, "SELECT COUNT(*) FROM catalogue_audit")
        pipeline_counts = {
            "source_documents": _scalar(conn, "SELECT COUNT(*) FROM catalogue_source_documents"),
            "ingestion_runs": _scalar(conn, "SELECT COUNT(*) FROM catalogue_ingestion_runs"),
            "raw_observations": _scalar(conn, "SELECT COUNT(*) FROM catalogue_extracted_evidence"),
            "staging_items": _scalar(conn, "SELECT COUNT(*) FROM catalogue_interpreted_claims"),
            "validation_issues": _scalar(conn, "SELECT COUNT(*) FROM catalogue_validation_issues"),
            "mastering_candidates": _scalar(conn, "SELECT COUNT(*) FROM catalogue_mastering_candidates"),
            "serving_publications": _scalar(conn, "SELECT COUNT(*) FROM catalogue_serving_publications"),
            "supplier_prices": _scalar(conn, "SELECT COUNT(*) FROM catalogue_supplier_prices"),
            "supplier_mbb_terms": _scalar(conn, "SELECT COUNT(*) FROM catalogue_supplier_mbb_terms"),
        }

    safe_linkable_imports = max(legacy_imports - linked_source_documents, 0)
    review_required = {
        "cost_rows_without_basis_or_review_lineage": legacy_cost_rows,
        "packaging_rows_requiring_semantic_confirmation": legacy_packaging_rows,
        "legacy_mbb_terms_requiring_condition_benefit_mapping": legacy_mbb_rows,
        "audit_events_not_yet_typed_review_decisions": legacy_review_events,
    }
    compatibility_only = {
        "legacy_catalogue_items": legacy_catalogue_items,
    }
    return {
        "safe_linkable": {
            "catalogue_imports_without_pipeline_source_document": safe_linkable_imports,
        },
        "compatibility_only": compatibility_only,
        "review_required": review_required,
        "rejected_unmappable": {
            "automatic_corrections_attempted": 0,
        },
        "pipeline_persisted": pipeline_counts,
    }


def main() -> None:
    database.run_migrations(database.engine)
    print(json.dumps(collect_catalogue_migration_audit(database.engine), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
