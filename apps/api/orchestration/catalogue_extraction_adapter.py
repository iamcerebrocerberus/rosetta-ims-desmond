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

    result = extract_evidence(source.content, source.original_filename, "")
    if result.status == ExtractionStatus.FAILED:
        raise _failure_from(result)
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
    message = "; ".join(error.message for error in result.errors) or "Extraction produced no source evidence"
    return ExtractionEvidenceError(message)
