"""Run lifecycle transitions owned by catalogue orchestration."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

import models
from models import IngestionRunMetrics, IngestionRunStatus
from models.ingestion_run import TERMINAL_STATUSES

from .catalogue_types import (
    CatalogueFlowResult,
    DuplicateRunClaim,
    InvalidRunTransition,
    RunNotFound,
    TerminalRunReplay,
)


def claim_queued_run(db: Session, *, ingestion_run_id: UUID, started_at: datetime | None = None) -> None:
    """Atomically move one queued run to running."""

    now = _iso(started_at or _now())
    result = db.execute(
        text(
            "UPDATE catalogue_ingestion_runs "
            "SET status = :running, started_at = :started_at, completed_at = NULL "
            "WHERE run_uuid = :run_uuid AND status = :queued"
        ),
        {
            "running": IngestionRunStatus.RUNNING.value,
            "started_at": now,
            "run_uuid": str(ingestion_run_id),
            "queued": IngestionRunStatus.QUEUED.value,
        },
    )
    db.commit()
    if result.rowcount == 1:
        return
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(ingestion_run_id)).first()
    if run is None:
        raise RunNotFound(f"Ingestion run {ingestion_run_id} was not found")
    if run.status in TERMINAL_STATUSES:
        raise TerminalRunReplay(f"Ingestion run {ingestion_run_id} is already terminal: {run.status}")
    if run.status == IngestionRunStatus.RUNNING.value:
        raise DuplicateRunClaim(f"Ingestion run {ingestion_run_id} is already running")
    raise InvalidRunTransition(f"Ingestion run {ingestion_run_id} cannot start from status {run.status}")


def complete_run(db: Session, *, result: CatalogueFlowResult, completed_at: datetime | None = None) -> None:
    """Persist terminal successful/warning state for a running run."""

    if result.terminal_status not in {
        IngestionRunStatus.COMPLETED.value,
        IngestionRunStatus.COMPLETED_WITH_WARNINGS.value,
    }:
        raise InvalidRunTransition("complete_run requires a completed terminal status")
    run = _run(db, result.ingestion_run_id)
    if run.status != IngestionRunStatus.RUNNING.value:
        raise InvalidRunTransition(f"Ingestion run {result.ingestion_run_id} cannot complete from {run.status}")
    run.status = result.terminal_status
    run.completed_at = _iso(completed_at or _now())
    run.items_extracted = result.rows_extracted
    run.metrics = _metrics_json(result)
    run.error_summary = _summary_json(result) if result.warnings else None
    db.commit()


def fail_run(
    db: Session,
    *,
    ingestion_run_id: UUID,
    error_code: str,
    message: str,
    completed_at: datetime | None = None,
) -> None:
    """Record a sanitized failure in a fresh transaction."""

    run = _run(db, ingestion_run_id)
    if run.status in TERMINAL_STATUSES:
        return
    if run.status not in {IngestionRunStatus.QUEUED.value, IngestionRunStatus.RUNNING.value}:
        raise InvalidRunTransition(f"Ingestion run {ingestion_run_id} cannot fail from {run.status}")
    run.status = IngestionRunStatus.FAILED.value
    run.completed_at = _iso(completed_at or _now())
    run.error_summary = json.dumps({"error_code": error_code, "message": _sanitize(message)}, sort_keys=True)
    db.commit()


def cancel_run(db: Session, *, ingestion_run_id: UUID, reason: str, cancelled_at: datetime | None = None) -> None:
    """Cancel a queued or running run without deleting evidence."""

    run = _run(db, ingestion_run_id)
    if run.status in TERMINAL_STATUSES:
        raise InvalidRunTransition(f"Ingestion run {ingestion_run_id} is already terminal")
    if run.status not in {IngestionRunStatus.QUEUED.value, IngestionRunStatus.RUNNING.value}:
        raise InvalidRunTransition(f"Ingestion run {ingestion_run_id} cannot be cancelled from {run.status}")
    run.status = IngestionRunStatus.CANCELLED.value
    run.completed_at = _iso(cancelled_at or _now())
    run.error_summary = json.dumps({"error_code": "RUN_CANCELLED", "message": _sanitize(reason)}, sort_keys=True)
    db.commit()


def terminal_result_for_replay(db: Session, *, ingestion_run_id: UUID) -> CatalogueFlowResult:
    """Return a safe result for a terminal replay."""

    run = _run(db, ingestion_run_id)
    metrics = _json_or_none(run.metrics) or {}
    summary = _json_or_none(run.error_summary) or {}
    return CatalogueFlowResult(
        ingestion_run_id=ingestion_run_id,
        terminal_status=run.status,
        rows_extracted=run.items_extracted or int(metrics.get("rows_seen") or 0),
        raw_observations_created=int(metrics.get("raw_observations_created") or 0),
        raw_observations_reused=int(metrics.get("raw_observations_reused") or 0),
        staging_items_created=int(metrics.get("staging_items_created") or 0),
        staging_items_reused=int(metrics.get("staging_items_reused") or 0),
        validation_issues_created=int(metrics.get("validation_issues_created") or 0),
        validation_issues_reused=int(metrics.get("validation_issues_reused") or 0),
        mastering_candidates_created=int(metrics.get("mastering_candidates_created") or 0),
        mastering_candidates_reused=int(metrics.get("mastering_candidates_reused") or 0),
        rows_rejected=int(metrics.get("rows_rejected") or 0),
        warnings=tuple(summary.get("warnings") or []),
        human_review_required=bool(metrics.get("human_review_required")),
        error_code=summary.get("error_code"),
    )


def _run(db: Session, ingestion_run_id: UUID) -> models.IngestionRun:
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(ingestion_run_id)).first()
    if run is None:
        raise RunNotFound(f"Ingestion run {ingestion_run_id} was not found")
    return run


def _metrics_json(result: CatalogueFlowResult) -> str:
    metrics = IngestionRunMetrics(
        rows_seen=result.rows_extracted,
        warnings_count=len(result.warnings),
        rejected_count=result.rows_rejected,
    )
    payload = asdict(metrics)
    payload.update(
        {
            "raw_observations_created": result.raw_observations_created,
            "raw_observations_reused": result.raw_observations_reused,
            "staging_items_created": result.staging_items_created,
            "staging_items_reused": result.staging_items_reused,
            "validation_issues_created": result.validation_issues_created,
            "validation_issues_reused": result.validation_issues_reused,
            "mastering_candidates_created": result.mastering_candidates_created,
            "mastering_candidates_reused": result.mastering_candidates_reused,
            "rows_rejected": result.rows_rejected,
            "rows_interpreted": result.rows_interpreted,
            "rows_skipped_non_catalogue": result.rows_skipped_non_catalogue,
            "interpretation_degraded": result.interpretation_degraded,
            "human_review_required": result.human_review_required,
        }
    )
    return json.dumps({key: value for key, value in payload.items() if value is not None}, sort_keys=True)


def _summary_json(result: CatalogueFlowResult) -> str:
    return json.dumps({"warnings": list(result.warnings)}, sort_keys=True)


def _json_or_none(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _sanitize(message: str) -> str:
    return " ".join(str(message).split())[:500]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
