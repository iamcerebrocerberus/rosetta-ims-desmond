"""Policy adapter from typed evidence extraction to orchestration outcomes.

Extraction here is deliberately contract-independent: it records what the
source contains and where. The resolved supplier-source contract is applied
after Raw persistence by ``services.catalogue_conformance``.
"""

from __future__ import annotations

from services.catalogue_evidence_extraction import (
    ExtractionResult,
    ExtractionStatus,
    extract_evidence,
)

from .catalogue_types import (
    EvidenceOutcome,
    ExtractionEvidenceError,
    TransientProviderError,
    VerifiedSourceAsset,
)


def extract_source_evidence(source: VerifiedSourceAsset) -> EvidenceOutcome:
    """Extract verbatim, source-located observations for one verified source."""

    return evidence_outcome_from_result(extract_source_result(source))


def extract_source_result(source: VerifiedSourceAsset) -> ExtractionResult:
    """Return the complete provider result so orchestration can audit attempts."""

    return extract_evidence(source.content, source.original_filename, "")


def evidence_outcome_from_result(result: ExtractionResult) -> EvidenceOutcome:
    """Allow downstream work only after every attempted source unit completed."""

    if result.status != ExtractionStatus.COMPLETE:
        raise _failure_from(result)
    if not result.observations:
        raise ExtractionEvidenceError(
            "Extraction completed but found no catalogue evidence in any source unit"
        )
    warnings = tuple(result.warnings) + tuple(
        f"{error.unit_key or 'source'}: {error.message}" for error in result.errors
    )
    return EvidenceOutcome(
        observations=result.observations,
        rejected_units=max(0, result.units_attempted - result.units_completed),
        warnings=warnings,
    )


def _failure_from(result: ExtractionResult) -> Exception:
    retryable = next((error for error in result.errors if error.retryable), None)
    if retryable is not None:
        return TransientProviderError(retryable.message)
    message = (
        "; ".join(error.message for error in result.errors)
        or "Extraction did not account for every source unit"
    )
    return ExtractionEvidenceError(message)
