"""Raw stage: preserve, validate, identify and audit the original supplier file.

The raw stage answers exactly one question: what did the supplier send us,
and is the stored original intact? It verifies the durably stored original
(existence, readability, size limit, path safety, signature, checksum),
performs lightweight structural inspection only (PDF encryption flag and page
count — never text extraction), records the outcome, and returns a typed
result that carries identifiers and file facts but no file content.

State model (documented and tested):

- ``CatalogueRawStageAttempt`` is the append-only history — one immutable row
  per raw-stage execution, completed or failed. Re-running raw appends a new
  attempt; earlier attempts are never overwritten, so a later integrity
  failure can never erase the record of an earlier successful verification.
- The mutable fields on ``CatalogueSourceDocument`` (``raw_stage_status``,
  ``raw_stage_completed_at``, ``byte_size``, ``page_count``) mirror the MOST
  RECENT attempt only, for cheap status queries — ALL of them, so state can
  never read ambiguously: a failed latest attempt clears the completion
  timestamp and carries only what that attempt actually observed. The
  earlier successful verification stays in the attempt history.

This module must never import or reach an AI provider, OCR, document parsing,
extraction, interpretation, normalization or business validation. Anything
that tries to understand what the file MEANS belongs to the extraction stage
and later stages, which consume the stored original through its durable
``source_ref`` after this stage has completed. Terminology note: the
``CatalogueExtractedEvidence`` model is NOT this stage's output — it holds
extracted evidence observations produced by the extraction stage.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pypdf
from sqlalchemy.orm import Session

import models

from .catalogue_source_loader import load_and_verify_source_asset
from .catalogue_types import RawStageResult, SourceVerificationError, VerifiedSourceAsset

logger = logging.getLogger(__name__)


def complete_raw_stage(
    db: Session,
    *,
    ingestion_run_id: UUID,
    upload_root: str | Path | None = None,
    max_source_bytes: int | None = None,
) -> RawStageResult:
    """Verify and audit the stored original file; never look inside its meaning.

    Business-record idempotent: re-running for the same run re-verifies the
    same stored bytes and refreshes the same current-state fields without
    creating duplicate source documents — while each execution appends one
    immutable attempt row. Raises ``SourceVerificationError`` (persisting a
    ``failed`` attempt and current state) when the file is missing, empty,
    oversized, unreadable, corrupted, checksum-mismatched or password
    protected.
    """

    attempted_at = _now_iso()
    asset: VerifiedSourceAsset | None = None
    try:
        asset = load_and_verify_source_asset(
            db,
            ingestion_run_id=ingestion_run_id,
            upload_root=upload_root,
            max_source_bytes=max_source_bytes,
        )
        page_count = _structural_page_count(asset.content, asset.source_format)
    except SourceVerificationError as exc:
        _record_failed_attempt(
            db,
            ingestion_run_id=ingestion_run_id,
            attempted_at=attempted_at,
            error=exc,
            asset=asset,
        )
        raise

    source = _source_row(db, ingestion_run_id=ingestion_run_id)
    now = _now_iso()
    source.byte_size = asset.size_bytes
    source.page_count = page_count
    source.raw_stage_status = "completed"
    source.raw_stage_completed_at = now
    source.updated_at = now
    db.add(
        models.CatalogueRawStageAttempt(
            ingestion_run_uuid=str(ingestion_run_id),
            catalogue_source_document_id=source.id,
            status="completed",
            attempted_at=attempted_at,
            completed_at=now,
            checksum_sha256=asset.sha256,
            byte_size=asset.size_bytes,
            source_format=asset.source_format,
            page_count=page_count,
            created_at=now,
        )
    )
    db.commit()

    metadata = _source_metadata(source)
    return RawStageResult(
        run_identity=asset.run_identity,
        catalogue_import_id=source.legacy_import_id,
        original_filename=asset.original_filename,
        content_type=metadata.get("content_type"),
        byte_size=asset.size_bytes,
        checksum_sha256=asset.sha256,
        source_ref=asset.source_ref,
        page_count=page_count,
        received_at=source.received_at,
    )


def _structural_page_count(content: bytes, source_format: str) -> int | None:
    """Lightweight structural inspection for PDFs only.

    Reads the document structure to detect password protection and count
    pages. Never extracts text, images, tables or layout.
    """

    if source_format not in {"PDF", "PDF_TABLE"}:
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            # Supplier PDFs are frequently owner-locked (copy/print restrictions)
            # with an EMPTY user password — any viewer opens them. Those are
            # readable sources; only a real user password is a rejection.
            try:
                decrypted = reader.decrypt("")
            except Exception:
                decrypted = 0
            if not decrypted:
                raise SourceVerificationError("Source PDF is password protected")
        return len(reader.pages)
    except SourceVerificationError:
        raise
    except Exception as exc:
        raise SourceVerificationError("Source PDF structure cannot be read") from exc


def _record_failed_attempt(
    db: Session,
    *,
    ingestion_run_id: UUID,
    attempted_at: str,
    error: SourceVerificationError,
    asset: VerifiedSourceAsset | None,
) -> None:
    """Append a sanitized failed attempt and refresh current state.

    Best-effort: recording the attempt must never mask the original
    verification error. When even the run/source identity is unavailable
    (e.g. unknown run), nothing can be recorded.
    """

    try:
        db.rollback()
        source = _source_row(db, ingestion_run_id=ingestion_run_id)
        now = _now_iso()
        # Current state mirrors this (most recent, failed) attempt completely:
        # no stale completion timestamp or structural metrics from an earlier
        # success may coexist with a failed status.
        source.raw_stage_status = "failed"
        source.raw_stage_completed_at = None
        source.byte_size = asset.size_bytes if asset else None
        source.page_count = None
        source.updated_at = now
        db.add(
            models.CatalogueRawStageAttempt(
                ingestion_run_uuid=str(ingestion_run_id),
                catalogue_source_document_id=source.id,
                status="failed",
                attempted_at=attempted_at,
                checksum_sha256=asset.sha256 if asset else None,
                byte_size=asset.size_bytes if asset else None,
                source_format=asset.source_format if asset else (source.source_format or None),
                failure_code=getattr(error, "error_code", "SOURCE_VERIFICATION_ERROR"),
                failure_message=error.public_message(),
                created_at=now,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "raw stage failed for run %s and the failure attempt could not be recorded",
            ingestion_run_id,
        )


def _source_row(db: Session, *, ingestion_run_id: UUID) -> models.CatalogueSourceDocument:
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(ingestion_run_id)).first()
    if run is None:
        raise SourceVerificationError("Queued run has no canonical source document")
    source = run.pipeline_source_document
    if source is None and run.catalogue_source_document_id:
        source = db.get(models.CatalogueSourceDocument, run.catalogue_source_document_id)
    if source is None:
        raise SourceVerificationError("Queued run has no canonical source document")
    return source


def _source_metadata(source: models.CatalogueSourceDocument) -> dict:
    import json

    try:
        metadata = json.loads(source.source_metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["complete_raw_stage"]
