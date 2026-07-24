"""Serializable types shared by catalogue orchestration tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


class CatalogueOrchestrationError(ValueError):
    """Base class for sanitized orchestration failures."""

    error_code = "CATALOGUE_ORCHESTRATION_ERROR"
    retryable = False

    def public_message(self) -> str:
        return str(self) or self.error_code


class RunNotFound(CatalogueOrchestrationError):
    error_code = "RUN_NOT_FOUND"


class InvalidRunTransition(CatalogueOrchestrationError):
    error_code = "INVALID_RUN_TRANSITION"


class DuplicateRunClaim(CatalogueOrchestrationError):
    error_code = "DUPLICATE_RUN_CLAIM"


class TerminalRunReplay(CatalogueOrchestrationError):
    error_code = "TERMINAL_RUN_REPLAY"


class SourceVerificationError(CatalogueOrchestrationError):
    error_code = "SOURCE_VERIFICATION_ERROR"


class RecordedContractError(CatalogueOrchestrationError):
    error_code = "RECORDED_CONTRACT_ERROR"


class ExtractionEvidenceError(CatalogueOrchestrationError):
    error_code = "EXTRACTION_EVIDENCE_ERROR"


class TransientProviderError(CatalogueOrchestrationError):
    """Retryable extraction/interpretation provider failure."""

    error_code = "TRANSIENT_PROVIDER_ERROR"
    retryable = True


@dataclass(frozen=True)
class RunIdentity:
    """Persisted run/source/contract identity reloaded inside orchestration."""

    run_uuid: UUID
    supplier_catalogue_id: UUID
    source_file_id: UUID
    supplier_id: int
    contract_id: str
    contract_version: str
    document_type: str
    source_format: str
    filename: str


@dataclass(frozen=True)
class VerifiedSourceAsset:
    """A durably verified source file loaded for extraction."""

    run_identity: RunIdentity
    original_filename: str
    source_ref: str
    source_format: str
    sha256: str
    size_bytes: int
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class RawStageResult:
    """File-only outcome of the raw stage: identity plus integrity metadata.

    Deliberately carries no file content and nothing interpreted from it —
    no rows, text, tables, products, model output or confidence values.
    """

    run_identity: RunIdentity
    catalogue_import_id: int | None
    original_filename: str
    content_type: str | None
    byte_size: int
    checksum_sha256: str
    source_ref: str
    page_count: int | None
    received_at: str | None
    status: str = "completed"


@dataclass(frozen=True)
class RecordedSupplierContract:
    """Exact supplier-source contract identity resolved from persistence."""

    supplier_id: int
    contract_id: str
    contract_version: str
    document_type: str
    source_format: str


@dataclass(frozen=True)
class EvidenceOutcome:
    """Typed observations plus completeness accounting from evidence extraction.

    ``observations`` holds ``services.catalogue_evidence_extraction.ExtractedEvidence``
    instances; kept untyped here so this module stays import-light for Prefect.
    """

    observations: tuple[Any, ...]
    rejected_units: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogueFlowResult:
    """Operational result for one machine ingestion attempt."""

    ingestion_run_id: UUID
    terminal_status: str
    rows_extracted: int = 0
    raw_observations_created: int = 0
    raw_observations_reused: int = 0
    staging_items_created: int = 0
    staging_items_reused: int = 0
    validation_issues_created: int = 0
    validation_issues_reused: int = 0
    mastering_candidates_created: int = 0
    mastering_candidates_reused: int = 0
    rows_rejected: int = 0
    rows_interpreted: int = 0
    rows_skipped_non_catalogue: int = 0
    interpretation_degraded: bool = False
    warnings: tuple[str, ...] = ()
    human_review_required: bool = False
    error_code: str | None = None
