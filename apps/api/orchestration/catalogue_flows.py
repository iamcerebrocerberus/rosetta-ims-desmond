"""Prefect flows for catalogue ingestion."""

from __future__ import annotations

import logging
import re
from uuid import UUID

from prefect import flow

from services import catalogue_pipeline_stages as stages

from .catalogue_tasks import (
    build_interpreted_claims_task,
    capture_extracted_evidence_task,
    evaluate_interpreted_claims_task,
    extract_source_evidence_task,
    failure_result,
    finalize_run_task,
    interpret_raw_evidence_task,
    load_and_claim_run_task,
    log_flow_result,
    prepare_eligible_candidates_task,
    raw_stage_task,
    record_run_failure_task,
    resolve_recorded_contract_task,
    terminal_replay_result_task,
)
from .catalogue_types import (
    CatalogueFlowResult,
    CatalogueOrchestrationError,
    DuplicateRunClaim,
    RunNotFound,
    TerminalRunReplay,
)

logger = logging.getLogger(__name__)


@flow(name="catalogue-ingestion")
def catalogue_ingestion_flow(*, ingestion_run_id: UUID) -> CatalogueFlowResult:
    """Run the machine portion of catalogue ingestion for one queued run UUID."""

    run_id = str(ingestion_run_id)
    try:
        load_and_claim_run_task(run_id)
    except TerminalRunReplay:
        return terminal_replay_result_task(run_id)
    except DuplicateRunClaim as exc:
        return CatalogueFlowResult(
            ingestion_run_id=ingestion_run_id,
            terminal_status="running",
            warnings=(exc.public_message(),),
            error_code=exc.error_code,
        )
    except RunNotFound as exc:
        return failure_result(run_id, exc)
    except CatalogueOrchestrationError as exc:
        record_run_failure_task(run_id, exc.error_code, exc.public_message())
        return failure_result(run_id, exc)

    try:
        # Raw stage completes (file preserved, verified and audited) before any
        # stage that tries to understand the file becomes reachable.
        raw = raw_stage_task(run_id)
        runtime_contract = resolve_recorded_contract_task(run_id)
        evidence = extract_source_evidence_task(run_id)
        raw_ids, raw_created, raw_reused = capture_extracted_evidence_task(raw.run_identity, evidence.observations)
        interpretation = interpret_raw_evidence_task(raw_ids, runtime_contract)
        staging_ids, staging_created, staging_reused = build_interpreted_claims_task(interpretation)
        validation_created, validation_reused, blocking_count = evaluate_interpreted_claims_task(staging_ids)
        candidate_created, candidate_reused, candidate_warnings = prepare_eligible_candidates_task(
            raw.run_identity,
            staging_ids,
            interpretation,
        )
        warnings = tuple(evidence.warnings) + tuple(interpretation.warnings) + tuple(candidate_warnings)
        if evidence.rejected_units or blocking_count or validation_created or validation_reused or warnings:
            terminal_status = "completed_with_warnings"
        else:
            terminal_status = "completed"
        result = CatalogueFlowResult(
            ingestion_run_id=ingestion_run_id,
            terminal_status=terminal_status,
            rows_extracted=len(evidence.observations),
            raw_observations_created=raw_created,
            raw_observations_reused=raw_reused,
            staging_items_created=staging_created,
            staging_items_reused=staging_reused,
            validation_issues_created=validation_created,
            validation_issues_reused=validation_reused,
            mastering_candidates_created=candidate_created,
            mastering_candidates_reused=candidate_reused,
            rows_rejected=evidence.rejected_units,
            rows_interpreted=len(interpretation.items),
            rows_skipped_non_catalogue=interpretation.skipped_count,
            interpretation_degraded=bool(interpretation.metadata.get("degraded")),
            warnings=warnings,
            human_review_required=True,
        )
        finalized = finalize_run_task(result)
        log_flow_result(finalized)
        return finalized
    except CatalogueOrchestrationError as exc:
        if not isinstance(exc, RunNotFound):
            _record_failure_safely(run_id, exc.error_code, exc.public_message())
        return failure_result(run_id, exc)
    except stages.CatalogueStageError as exc:
        # Expected persistence/idempotency failures: once claimed, the run
        # must never remain `running`. Map to a stable, sanitized stage code.
        code = _stage_error_code(exc)
        message = _stage_error_message(exc, code)
        _record_failure_safely(run_id, code, message)
        return CatalogueFlowResult(
            ingestion_run_id=ingestion_run_id,
            terminal_status="failed",
            warnings=(message,),
            human_review_required=False,
            error_code=code,
        )
    except Exception:
        # Unexpected failure: log full diagnostics internally, persist only a
        # sanitized failure state, and never leave the run `running`.
        logger.exception("catalogue ingestion run %s failed unexpectedly", run_id)
        message = "Catalogue ingestion failed unexpectedly"
        _record_failure_safely(run_id, "INTERNAL_PIPELINE_ERROR", message)
        return CatalogueFlowResult(
            ingestion_run_id=ingestion_run_id,
            terminal_status="failed",
            warnings=(message,),
            human_review_required=False,
            error_code="INTERNAL_PIPELINE_ERROR",
        )


def _record_failure_safely(run_id: str, error_code: str, message: str) -> None:
    """Durably fail the run; a failure-recording failure is logged, never masked."""

    try:
        record_run_failure_task(run_id, error_code, message)
    except Exception:
        logger.exception(
            "catalogue ingestion run %s failed (%s) but the failure state could not be recorded",
            run_id,
            error_code,
        )


def _stage_error_code(exc: stages.CatalogueStageError) -> str:
    name = type(exc).__name__
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def _stage_error_message(exc: stages.CatalogueStageError, code: str) -> str:
    # ConcurrentModification wraps raw database driver text — never expose it.
    if isinstance(exc, stages.ConcurrentModification):
        return "Concurrent modification detected while persisting catalogue evidence"
    return str(exc) or code
