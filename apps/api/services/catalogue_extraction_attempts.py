"""Durable append-only accounting for Staging extraction attempts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

import models
from services.catalogue_evidence_extraction import ExtractionResult


def persist_extraction_attempt(
    db: Session,
    *,
    ingestion_run_id: UUID,
    result: ExtractionResult,
    started_at: datetime,
) -> UUID:
    """Persist one provider invocation, including every unit outcome."""

    completed_at = datetime.now(timezone.utc)
    attempt_id = uuid4()
    source = (
        db.query(models.CatalogueSourceDocument)
        .join(
            models.IngestionRun,
            models.IngestionRun.catalogue_source_document_id
            == models.CatalogueSourceDocument.id,
        )
        .filter(models.IngestionRun.run_uuid == str(ingestion_run_id))
        .first()
    )
    providers = sorted(
        {
            item.provider
            for item in result.unit_outcomes
            if item.provider
        }
    )
    models_used = sorted(
        {
            observation.model
            for observation in result.observations
            if observation.model
        }
    )
    db.add(
        models.CatalogueExtractionAttempt(
            attempt_uuid=str(attempt_id),
            ingestion_run_uuid=str(ingestion_run_id),
            catalogue_source_document_id=source.id if source else None,
            status=result.status.value,
            source_format=result.source_format.value,
            provider=",".join(providers) or None,
            model_name=",".join(models_used) or None,
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            units_attempted=result.units_attempted,
            units_completed=result.units_completed,
            empty_units=result.empty_units,
            observation_count=len(result.observations),
            unit_outcomes_json=json.dumps(
                [item.model_dump(mode="json") for item in result.unit_outcomes],
                sort_keys=True,
            ),
            warnings_json=json.dumps(list(result.warnings)),
            errors_json=json.dumps(
                [item.model_dump(mode="json") for item in result.errors],
                sort_keys=True,
            ),
            created_at=completed_at.isoformat(),
        )
    )
    db.commit()
    return attempt_id


__all__ = ["persist_extraction_attempt"]
