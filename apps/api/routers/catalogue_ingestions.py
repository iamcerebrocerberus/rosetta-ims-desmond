"""Queued catalogue ingestion boundary (evidence-first pipeline)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import database
import models
from permissions import require_capability
from services import audit_log
from services import catalogue_pipeline_persistence as persistence
from services import catalogue_pipeline_stages as stages
from schemas.catalogue_pipeline.enums import IssueResolutionStatus, ReviewStatus
from services.catalogue_submission import (
    CatalogueIngestionStatus,
    CatalogueSubmissionCommand,
    CatalogueSubmissionError,
    CatalogueSubmissionResult,
    CatalogueSubmissionService,
    ContractParameterError,
    EmptyUploadError,
    MalformedFilenameError,
    SubmissionPersistenceError,
    StorageUnavailableError,
    SubmissionIdempotencyConflict,
    SubmissionNotFoundError,
    SupplierContractAmbiguousError,
    SupplierContractMismatchError,
    SupplierContractSelectionError,
    UnknownSupplierError,
    UnsupportedSourceTypeError,
    UploadTooLargeError,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalogues", tags=["catalogue-ingestions"])


class CatalogueSubmissionResponse(BaseModel):
    ingestion_run_id: UUID = Field(..., description="Stable ingestion run UUID.")
    supplier_catalogue_id: UUID = Field(..., description="Stable source catalogue UUID.")
    source_file_id: UUID = Field(..., description="Stable source file UUID.")
    supplier_id: int = Field(..., gt=0, description="Supplier ID submitted by the client.")
    contract_id: str = Field(..., description="Resolved supplier-source contract ID.")
    contract_version: str = Field(..., description="Resolved supplier-source contract version.")
    document_type: str = Field(..., description="Resolved supplier document type.")
    status: str = Field(..., description="Queued ingestion run status.")
    submitted_at: str = Field(..., description="Timezone-aware submission timestamp.")
    status_url: str = Field(..., description="Polling URL for this queued run.")


class CatalogueIngestionStatusResponse(BaseModel):
    ingestion_run_id: UUID
    supplier_catalogue_id: UUID | None = None
    source_file_id: UUID | None = None
    supplier_id: int | None = None
    contract_id: str | None = None
    contract_version: str | None = None
    document_type: str | None = None
    status: str
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    items_extracted: int | None = None
    metrics: dict[str, Any] | None = None
    error_summary: dict[str, Any] | str | None = None


class ValidationIssueResolutionRequest(BaseModel):
    resolution_status: IssueResolutionStatus
    resolution_note: str | None = None
    resolved_at: datetime | None = None


class MasteringReviewRequest(BaseModel):
    review_status: ReviewStatus
    reason: str | None = None
    override_reason: str | None = None
    expected_candidate_created_at: str | None = None
    decided_at: datetime | None = None


class MasteringCorrectionRequest(BaseModel):
    """Human correction of one or more candidate resolution sections.

    Produces an immutable revised candidate superseding this one; the revision
    is what gets approved. Sections are validated by the stage service against
    the mastering-candidate contract.
    """

    reason: str
    expected_candidate_created_at: str | None = None
    revised_at: datetime | None = None
    supplier_product_resolution: dict[str, Any] | None = None
    product_variant_resolution: dict[str, Any] | None = None
    packaging_resolution: dict[str, Any] | None = None
    supplier_price_resolution: dict[str, Any] | None = None
    mbb_resolution: dict[str, Any] | None = None
    product_family_resolution: dict[str, Any] | None = None
    brand_resolution: dict[str, Any] | None = None
    category_resolution: dict[str, Any] | None = None


class CommercialApplicationRequest(BaseModel):
    applied_at: datetime | None = None


class ServingPublicationRequest(BaseModel):
    publication_version: str = Field(..., min_length=1)
    published_at: datetime | None = None


class PipelineActionResponse(BaseModel):
    stage: str
    status: str
    output_ids: list[str]
    metrics: dict[str, int]


@router.post(
    "/ingestions",
    response_model=CatalogueSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_catalogue_ingestion(
    request: Request,
    file: UploadFile = File(...),
    supplier_id: int = Form(..., gt=0),
    contract_id: str | None = Form(None),
    contract_version: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(database.get_db),
    user: models.User = Depends(require_capability("catalogue_onboard")),
):
    service = CatalogueSubmissionService(db)
    try:
        result = service.submit(
            CatalogueSubmissionCommand(
                supplier_id=supplier_id,
                original_filename=file.filename or "",
                content_type=file.content_type,
                stream=file.file,
                contract_id=contract_id,
                contract_version=contract_version,
                idempotency_key=idempotency_key,
                submitted_by=getattr(user, "username", None) or str(getattr(user, "id", "")),
            )
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    # The submission service has already committed the durable source, import
    # and queued run. From here on, audit logging is post-commit observability:
    # a failure here must NOT convert an accepted submission into a 500 (a
    # keyless client retry would create a second run). It stays operationally
    # visible through the application logger instead.
    try:
        audit_log.record(
            db,
            action="catalogue.ingestion_submit",
            actor=user,
            entity_type="ingestion_run",
            entity_id=str(result.ingestion_run_id),
            entity_label=file.filename,
            details={
                "supplier_id": result.supplier_id,
                "contract_id": result.contract_id,
                "contract_version": result.contract_version,
                "status": result.status,
            },
            request=request,
            commit=True,
        )
    except Exception:
        db.rollback()
        logger.exception(
            "catalogue ingestion %s was durably submitted but audit logging failed",
            result.ingestion_run_id,
        )
    return _submission_response(result)

@router.get(
    "/ingestions/run_ids",
    response_model=list[CatalogueIngestionStatusResponse],
)
def get_catalogue_ingestions_run_ids(
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
):
    service = CatalogueSubmissionService(db)
    runs = service.list()
    return [_status_response(run) for run in runs]


@router.get(
    "/ingestions/{run_uuid}",
    response_model=CatalogueIngestionStatusResponse,
)
def get_catalogue_ingestion_status(
    run_uuid: UUID,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
):
    service = CatalogueSubmissionService(db)
    try:
        result = service.get_status(run_uuid)
    except Exception as exc:
        raise _http_error(exc) from exc
    return _status_response(result)


@router.post(
    "/ingestions/{run_uuid}/validation-issues/{validation_issue_id}/resolve",
    response_model=PipelineActionResponse,
)
def resolve_catalogue_validation_issue(
    run_uuid: UUID,
    validation_issue_id: UUID,
    body: ValidationIssueResolutionRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(database.get_db),
    user: models.User = Depends(require_capability("catalogue_onboard")),
):
    _load_run_or_404(db, run_uuid)
    _load_run_validation_issue_or_404(db, run_uuid, validation_issue_id)
    actor_id = _actor_id(user)
    try:
        result = stages.CatalogueValidationService(db, commit=False).resolve_issue(
            stages.ResolveValidationIssueCommand(
                validation_issue_id=validation_issue_id,
                resolver_id=actor_id,
                resolution_status=body.resolution_status,
                resolution_note=body.resolution_note,
                resolved_at=body.resolved_at,
                idempotency_key=idempotency_key,
            )
        )
        _audit_pipeline_action(
            db,
            request=request,
            user=user,
            action="catalogue.pipeline_validation_resolve",
            entity_type="catalogue_validation_issue",
            entity_id=validation_issue_id,
            result=result,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise _stage_http_error(exc) from exc
    return _action_response(result)


@router.post(
    "/ingestions/{run_uuid}/mastering-candidates/{mastering_candidate_id}/review",
    response_model=PipelineActionResponse,
)
def review_catalogue_mastering_candidate(
    run_uuid: UUID,
    mastering_candidate_id: UUID,
    body: MasteringReviewRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(database.get_db),
    user: models.User = Depends(require_capability("catalogue_onboard")),
):
    _load_run_or_404(db, run_uuid)
    _load_run_candidate_or_404(db, run_uuid, mastering_candidate_id)
    try:
        result = stages.ReviewDecisionService(db, commit=False).record_decision(
            stages.RecordReviewDecisionCommand(
                mastering_candidate_id=mastering_candidate_id,
                actor_id=_actor_id(user),
                review_status=body.review_status,
                reason=body.reason,
                override_reason=body.override_reason,
                expected_candidate_created_at=body.expected_candidate_created_at,
                decided_at=body.decided_at,
                idempotency_key=idempotency_key,
            )
        )
        _audit_pipeline_action(
            db,
            request=request,
            user=user,
            action="catalogue.pipeline_candidate_review",
            entity_type="catalogue_mastering_candidate",
            entity_id=mastering_candidate_id,
            result=result,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise _stage_http_error(exc) from exc
    return _action_response(result)


@router.post(
    "/ingestions/{run_uuid}/mastering-candidates/{mastering_candidate_id}/correct",
    response_model=PipelineActionResponse,
)
def correct_catalogue_mastering_candidate(
    run_uuid: UUID,
    mastering_candidate_id: UUID,
    body: MasteringCorrectionRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    user: models.User = Depends(require_capability("catalogue_onboard")),
):
    """Supersede a pending candidate with a human-corrected revision."""
    _load_run_or_404(db, run_uuid)
    _load_run_candidate_or_404(db, run_uuid, mastering_candidate_id)
    try:
        result = stages.MasteringService(db, commit=False).revise_candidate(
            stages.ReviseMasteringCandidateCommand(
                mastering_candidate_id=mastering_candidate_id,
                actor_id=_actor_id(user),
                reason=body.reason,
                expected_candidate_created_at=body.expected_candidate_created_at,
                revised_at=body.revised_at,
                supplier_product_resolution=body.supplier_product_resolution,
                product_variant_resolution=body.product_variant_resolution,
                packaging_resolution=body.packaging_resolution,
                supplier_price_resolution=body.supplier_price_resolution,
                mbb_resolution=body.mbb_resolution,
                product_family_resolution=body.product_family_resolution,
                brand_resolution=body.brand_resolution,
                category_resolution=body.category_resolution,
            )
        )
        _audit_pipeline_action(
            db,
            request=request,
            user=user,
            action="catalogue.pipeline_candidate_correct",
            entity_type="catalogue_mastering_candidate",
            entity_id=mastering_candidate_id,
            result=result,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise _stage_http_error(exc) from exc
    return _action_response(result)


@router.post(
    "/ingestions/{run_uuid}/mastering-candidates/{mastering_candidate_id}/apply",
    response_model=PipelineActionResponse,
)
def apply_catalogue_mastering_candidate(
    run_uuid: UUID,
    mastering_candidate_id: UUID,
    body: CommercialApplicationRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    user: models.User = Depends(require_capability("catalogue_publish")),
):
    _load_run_or_404(db, run_uuid)
    _load_run_candidate_or_404(db, run_uuid, mastering_candidate_id)
    try:
        result = stages.ApprovedCommercialStateService(db, commit=False).apply_approved_candidate(
            stages.ApplyApprovedCandidateCommand(
                mastering_candidate_id=mastering_candidate_id,
                applied_at=body.applied_at,
            )
        )
        _audit_pipeline_action(
            db,
            request=request,
            user=user,
            action="catalogue.pipeline_candidate_apply",
            entity_type="catalogue_mastering_candidate",
            entity_id=mastering_candidate_id,
            result=result,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise _stage_http_error(exc) from exc
    return _action_response(result)


@router.post(
    "/ingestions/{run_uuid}/mastering-candidates/{mastering_candidate_id}/publish",
    response_model=PipelineActionResponse,
)
def publish_catalogue_serving_item(
    run_uuid: UUID,
    mastering_candidate_id: UUID,
    body: ServingPublicationRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(database.get_db),
    user: models.User = Depends(require_capability("catalogue_publish")),
):
    _load_run_or_404(db, run_uuid)
    _load_run_candidate_or_404(db, run_uuid, mastering_candidate_id)
    try:
        result = stages.ServingPublicationService(db, commit=False).publish(
            stages.PublishServingItemCommand(
                mastering_candidate_id=mastering_candidate_id,
                publication_version=body.publication_version,
                published_at=body.published_at,
                idempotency_key=idempotency_key,
            )
        )
        _audit_pipeline_action(
            db,
            request=request,
            user=user,
            action="catalogue.pipeline_serving_publish",
            entity_type="catalogue_serving_publication",
            entity_id=result.output_ids[0] if result.output_ids else mastering_candidate_id,
            result=result,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise _stage_http_error(exc) from exc
    return _action_response(result)


# ── Per-layer read API ───────────────────────────────────────────────────────
# Read-only views over the durable records each pipeline layer produced for one
# run, named by the RAW -> STAGING -> INTERMEDIATE -> SERVING timeline. Records
# are reconstructed from the persistence contracts.


@router.get("/ingestions/{run_uuid}/raw")
def get_raw_layer(
    run_uuid: UUID,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
) -> dict[str, Any]:
    """RAW layer (steps 1-2): the preserved original's file facts and the
    append-only raw-stage verification history. No file content, no meaning."""
    run = _load_run_or_404(db, run_uuid)
    source = run.pipeline_source_document
    if source is None and run.catalogue_source_document_id:
        source = db.get(models.CatalogueSourceDocument, run.catalogue_source_document_id)
    attempts = (
        db.query(models.CatalogueRawStageAttempt)
        .filter_by(ingestion_run_uuid=str(run_uuid))
        .order_by(models.CatalogueRawStageAttempt.id)
        .all()
    )
    return {
        "ingestion_run_id": str(run_uuid),
        "layer": "raw",
        "source": _source_summary(source) if source else None,
        "attempts": [_attempt_summary(a) for a in attempts],
    }


@router.get("/ingestions/{run_uuid}/staging")
def get_staging_layer(
    run_uuid: UUID,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
) -> dict[str, Any]:
    """STAGING layer (steps 3-4): verbatim, source-located extracted evidence.

    Applying the supplier contract changes evidence into an interpreted
    proposal, so normalized rows belong to Intermediate rather than Staging.
    """
    _load_run_or_404(db, run_uuid)
    run = str(run_uuid)
    extraction_attempts = (
        db.query(models.CatalogueExtractionAttempt)
        .filter_by(ingestion_run_uuid=run)
        .order_by(models.CatalogueExtractionAttempt.id)
        .all()
    )
    evidence = [
        persistence.extracted_evidence_to_contract(r).model_dump(mode="json")
        for r in db.query(models.CatalogueExtractedEvidence)
        .filter_by(ingestion_run_uuid=run)
        .order_by(models.CatalogueExtractedEvidence.id)
        .all()
    ]
    return {
        "ingestion_run_id": run,
        "layer": "staging",
        "extraction_attempts": [
            _extraction_attempt_summary(attempt)
            for attempt in extraction_attempts
        ],
        "evidence_count": len(evidence),
        "evidence": evidence,
    }


def _extraction_attempt_summary(
    attempt: models.CatalogueExtractionAttempt,
) -> dict[str, Any]:
    import json as _json

    return {
        "attempt_id": attempt.attempt_uuid,
        "status": attempt.status,
        "source_format": attempt.source_format,
        "provider": attempt.provider,
        "model": attempt.model_name,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "units_attempted": attempt.units_attempted,
        "units_completed": attempt.units_completed,
        "empty_units": attempt.empty_units,
        "observation_count": attempt.observation_count,
        "unit_outcomes": _json.loads(attempt.unit_outcomes_json or "[]"),
        "warnings": _json.loads(attempt.warnings_json or "[]"),
        "errors": _json.loads(attempt.errors_json or "[]"),
    }


@router.get("/ingestions/{run_uuid}/intermediate")
def get_intermediate_layer(
    run_uuid: UUID,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
) -> dict[str, Any]:
    """INTERMEDIATE layer (steps 5-9): contract-conformed normalized claims,
    validation issues and mastering candidates awaiting human review."""
    _load_run_or_404(db, run_uuid)
    run_filter = {"ingestion_run_uuid": str(run_uuid)}
    normalized_rows = [
        persistence.normalized_row_to_contract(r).model_dump(mode="json")
        for r in db.query(models.CatalogueNormalizedRow)
        .filter_by(**run_filter)
        .order_by(models.CatalogueNormalizedRow.id)
        .all()
    ]
    issues = [
        persistence.validation_issue_to_contract(r).model_dump(mode="json")
        for r in db.query(models.CatalogueValidationIssue).filter_by(**run_filter).order_by(models.CatalogueValidationIssue.id).all()
    ]
    candidates = [
        {
            **persistence.mastering_candidate_to_contract(r).model_dump(mode="json"),
            "superseded_by": r.superseded_by_uuid,
        }
        for r in db.query(models.CatalogueMasteringCandidate).filter_by(**run_filter).order_by(models.CatalogueMasteringCandidate.id).all()
    ]
    return {
        "ingestion_run_id": str(run_uuid),
        "layer": "intermediate",
        "normalized_rows": normalized_rows,
        "validation_issues": issues,
        "mastering_candidates": candidates,
    }


@router.get("/ingestions/{run_uuid}/serving")
def get_serving_layer(
    run_uuid: UUID,
    db: Session = Depends(database.get_db),
    _user: models.User = Depends(require_capability("catalogue_onboard")),
) -> dict[str, Any]:
    """SERVING layer: immutable approved publication snapshots for this run.

    History remains visible for audit; ``current_publications`` is the
    consumer-safe view and contains only non-superseded snapshots.
    """
    _load_run_or_404(db, run_uuid)
    candidate_ids = [
        value
        for (value,) in db.query(models.CatalogueMasteringCandidate.mastering_candidate_uuid)
        .filter_by(ingestion_run_uuid=str(run_uuid))
        .all()
    ]
    rows = []
    if candidate_ids:
        rows = (
            db.query(models.CatalogueServingPublication)
            .filter(models.CatalogueServingPublication.mastering_candidate_uuid.in_(candidate_ids))
            .order_by(models.CatalogueServingPublication.id)
            .all()
        )
    publications = [
        {
            "is_current": bool(row.is_current),
            "superseded_at": row.superseded_at,
            "snapshot": persistence.serving_item_to_contract(row).model_dump(mode="json"),
        }
        for row in rows
    ]
    return {
        "ingestion_run_id": str(run_uuid),
        "layer": "serving",
        "publication_count": len(publications),
        "current_publications": [item["snapshot"] for item in publications if item["is_current"]],
        "publication_history": publications,
    }


def _load_run_or_404(db: Session, run_uuid: UUID) -> models.IngestionRun:
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(run_uuid)).first()
    if run is None:
        raise HTTPException(status_code=404, detail=_detail("INGESTION_RUN_NOT_FOUND", f"Ingestion run {run_uuid} was not found"))
    return run


def _load_run_candidate_or_404(
    db: Session,
    run_uuid: UUID,
    mastering_candidate_id: UUID,
) -> models.CatalogueMasteringCandidate:
    candidate = db.query(models.CatalogueMasteringCandidate).filter_by(
        mastering_candidate_uuid=str(mastering_candidate_id),
        ingestion_run_uuid=str(run_uuid),
    ).first()
    if candidate is None:
        raise HTTPException(
            status_code=404,
            detail=_detail(
                "MASTERING_CANDIDATE_NOT_FOUND",
                f"Mastering candidate {mastering_candidate_id} was not found in ingestion run {run_uuid}",
            ),
        )
    return candidate


def _load_run_validation_issue_or_404(
    db: Session,
    run_uuid: UUID,
    validation_issue_id: UUID,
) -> models.CatalogueValidationIssue:
    issue = db.query(models.CatalogueValidationIssue).filter_by(
        validation_issue_uuid=str(validation_issue_id),
        ingestion_run_uuid=str(run_uuid),
    ).first()
    if issue is None:
        raise HTTPException(
            status_code=404,
            detail=_detail(
                "VALIDATION_ISSUE_NOT_FOUND",
                f"Validation issue {validation_issue_id} was not found in ingestion run {run_uuid}",
            ),
        )
    return issue


def _actor_id(user: models.User) -> str:
    return getattr(user, "username", None) or str(getattr(user, "id", ""))


def _audit_pipeline_action(
    db: Session,
    *,
    request: Request,
    user: models.User,
    action: str,
    entity_type: str,
    entity_id: UUID | str,
    result: stages.StageResult,
) -> None:
    audit_log.record(
        db,
        action=action,
        actor=user,
        entity_type=entity_type,
        entity_id=entity_id,
        details={
            "stage": result.stage,
            "status": result.status,
            "output_ids": [str(item) for item in result.output_ids],
            "metrics": vars(result.metrics),
        },
        request=request,
    )


def _action_response(result: stages.StageResult) -> PipelineActionResponse:
    return PipelineActionResponse(
        stage=result.stage,
        status=result.status,
        output_ids=[str(item) for item in result.output_ids],
        metrics=vars(result.metrics),
    )


def _source_summary(source: models.CatalogueSourceDocument) -> dict[str, Any]:
    import json as _json

    try:
        metadata = _json.loads(source.source_metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return {
        "supplier_catalogue_id": source.supplier_catalogue_uuid,
        "source_file_id": source.source_file_uuid,
        "original_filename": metadata.get("original_filename") or source.filename,
        "content_type": metadata.get("content_type"),
        "byte_size": source.byte_size,
        "page_count": source.page_count,
        "checksum_sha256": source.source_checksum,
        "source_format": source.source_format,
        "supplier_source_contract_id": source.supplier_source_contract_id,
        "supplier_source_contract_version": source.supplier_source_contract_version,
        "document_type": source.document_type,
        "raw_stage_status": source.raw_stage_status,
        "raw_stage_completed_at": source.raw_stage_completed_at,
        "received_at": source.received_at,
    }


def _attempt_summary(attempt: models.CatalogueRawStageAttempt) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_uuid,
        "status": attempt.status,
        "attempted_at": attempt.attempted_at,
        "completed_at": attempt.completed_at,
        "checksum_sha256": attempt.checksum_sha256,
        "byte_size": attempt.byte_size,
        "source_format": attempt.source_format,
        "page_count": attempt.page_count,
        "failure_code": attempt.failure_code,
        "failure_message": attempt.failure_message,
    }


def _submission_response(result: CatalogueSubmissionResult) -> CatalogueSubmissionResponse:
    return CatalogueSubmissionResponse(**result.__dict__)


def _status_response(result: CatalogueIngestionStatus) -> CatalogueIngestionStatusResponse:
    return CatalogueIngestionStatusResponse(**result.__dict__)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UnknownSupplierError):
        return HTTPException(status_code=404, detail=_detail("UNKNOWN_SUPPLIER", str(exc)))
    if isinstance(exc, ContractParameterError):
        return HTTPException(status_code=422, detail=_detail("INVALID_CONTRACT_PARAMETERS", str(exc)))
    if isinstance(exc, SupplierContractAmbiguousError):
        return HTTPException(status_code=409, detail=_detail("AMBIGUOUS_SUPPLIER_CONTRACT", str(exc)))
    if isinstance(exc, SupplierContractMismatchError):
        return HTTPException(status_code=409, detail=_detail("SUPPLIER_CONTRACT_MISMATCH", str(exc)))
    if isinstance(exc, SupplierContractSelectionError):
        return HTTPException(status_code=422, detail=_detail("UNSUPPORTED_SUPPLIER_CONTRACT", str(exc)))
    if isinstance(exc, SubmissionIdempotencyConflict):
        return HTTPException(status_code=409, detail=_detail("IDEMPOTENCY_CONFLICT", str(exc)))
    if isinstance(exc, EmptyUploadError):
        return HTTPException(status_code=400, detail=_detail("EMPTY_UPLOAD", str(exc)))
    if isinstance(exc, UnsupportedSourceTypeError):
        return HTTPException(status_code=415, detail=_detail("UNSUPPORTED_SOURCE_TYPE", str(exc)))
    if isinstance(exc, UploadTooLargeError):
        return HTTPException(status_code=413, detail=_detail("UPLOAD_TOO_LARGE", str(exc)))
    if isinstance(exc, MalformedFilenameError):
        return HTTPException(status_code=400, detail=_detail("MALFORMED_FILENAME", str(exc)))
    if isinstance(exc, StorageUnavailableError):
        return HTTPException(status_code=503, detail=_detail("STORAGE_UNAVAILABLE", str(exc)))
    if isinstance(exc, SubmissionPersistenceError):
        return HTTPException(status_code=503, detail=_detail("SUBMISSION_PERSISTENCE_UNAVAILABLE", str(exc)))
    if isinstance(exc, SubmissionNotFoundError):
        return HTTPException(status_code=404, detail=_detail("INGESTION_RUN_NOT_FOUND", str(exc)))
    if isinstance(exc, CatalogueSubmissionError):
        return HTTPException(status_code=400, detail=_detail("CATALOGUE_SUBMISSION_ERROR", str(exc)))
    return HTTPException(status_code=500, detail=_detail("INTERNAL_ERROR", "Catalogue submission failed"))


def _stage_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, stages.UpstreamRecordNotFound):
        return HTTPException(status_code=404, detail=_detail("PIPELINE_RECORD_NOT_FOUND", str(exc)))
    if isinstance(
        exc,
        (
            stages.BlockingValidationIssues,
            stages.ConcurrentModification,
            stages.IdempotencyConflict,
            stages.InvalidStageTransition,
            stages.MissingOrIncompatibleLineage,
            stages.PublicationIneligible,
            stages.StaleCandidateRevision,
            stages.SupplierContractMismatch,
            stages.AmbiguousProductVariant,
            stages.AmbiguousSupplierOffer,
        ),
    ):
        return HTTPException(status_code=409, detail=_detail("PIPELINE_TRANSITION_CONFLICT", str(exc)))
    if isinstance(exc, stages.CatalogueStageError):
        return HTTPException(status_code=422, detail=_detail("PIPELINE_ACTION_INVALID", str(exc)))
    return HTTPException(status_code=500, detail=_detail("INTERNAL_ERROR", "Catalogue pipeline action failed"))


def _detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}
