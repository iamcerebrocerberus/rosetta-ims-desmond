"""Framework-neutral catalogue pipeline stage services.

These services execute one persisted catalogue pipeline transition at a time.
They validate the Pydantic boundary contract, persist through the approved
mapper/model layer, and return typed results. They intentionally do not import
FastAPI, Prefect, routers, or request objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from schemas.catalogue_pipeline import (
    MasteringCandidateV1,
    ExtractedEvidenceV1,
    ServingItemV1,
    NormalizedRowV1,
    ValidationIssueV1,
)
from schemas.catalogue_pipeline.common import (
    Cost,
    ExtractionProfileReference,
    FieldEvidence,
    LineageReference,
    MbbTerm,
    Money,
    PackagingConfiguration,
    PipelineTrace,
    UnitOfMeasure,
)
from schemas.catalogue_pipeline.enums import (
    ExtractionMethod,
    IssueResolutionStatus,
    IssueSeverity,
    ResolutionState,
    ReviewRequirement,
    ReviewStatus,
    UnitCode,
    ValidationStage,
)
from schemas.catalogue_pipeline.mastering_candidate_v1 import (
    MbbResolution,
    OptionalTextResolution,
    PackagingConfigurationResolution,
    ProductVariantResolution,
    SupplierPriceResolution,
    SupplierProductResolution,
)
from schemas.catalogue_pipeline.extracted_evidence_v1 import RawCell, SourceLocation
from schemas.catalogue_pipeline.serving_item_v1 import PublicationLineage, SupplierOffering
from schemas.catalogue_pipeline.normalized_row_v1 import NormalizedCatalogueFields, ClaimRawFields
from services import catalogue_pipeline_persistence as persistence
from services import supplier_source_contract_runtime


class CatalogueStageError(ValueError):
    """Base error for catalogue stage service failures."""


class UpstreamRecordNotFound(CatalogueStageError):
    """Raised when a required upstream row is missing."""


class SupplierContractMismatch(CatalogueStageError):
    """Raised when run/source/supplier contract identity is inconsistent."""


class UnsupportedSupplierContract(CatalogueStageError):
    """Raised when a supplier-source contract is not runtime supported."""


class InvalidStageTransition(CatalogueStageError):
    """Raised when a stage transition is not allowed from current state."""


class MissingOrIncompatibleLineage(CatalogueStageError):
    """Raised when a stage command crosses incompatible lineage boundaries."""


class BlockingValidationIssues(CatalogueStageError):
    """Raised when open blocking validation issues prevent progression."""


class StaleCandidateRevision(CatalogueStageError):
    """Raised when a review command targets an older candidate revision."""


class AmbiguousProductVariant(CatalogueStageError):
    """Raised when an approved action lacks a resolvable Product Variant."""


class AmbiguousSupplierOffer(CatalogueStageError):
    """Raised when an approved action lacks a resolvable Supplier Offer."""


class IdempotencyConflict(CatalogueStageError):
    """Raised when an idempotency identity is reused with different material input."""


class ConcurrentModification(CatalogueStageError):
    """Raised when a database uniqueness guard catches a conflicting write."""


class PublicationIneligible(CatalogueStageError):
    """Raised when a serving publication precondition is not met."""


@dataclass(frozen=True)
class StageMetrics:
    """Small machine-readable outcome counters for a stage operation."""

    input_count: int = 0
    created_count: int = 0
    reused_count: int = 0
    warning_count: int = 0
    blocking_issue_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class StageResult:
    """Typed result returned by public stage services."""

    stage: str
    output_ids: tuple[UUID | str, ...] = ()
    status: str = "completed"
    metrics: StageMetrics = field(default_factory=StageMetrics)
    issue_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ExtractedEvidenceInput:
    """One supplier-extraction row/cell/text evidence payload."""

    idempotency_key: str
    source_location: SourceLocation | dict[str, Any]
    raw_text: str | None = None
    raw_cells: tuple[RawCell | dict[str, Any], ...] = ()
    extraction_method: ExtractionMethod = ExtractionMethod.MODEL_TEXT
    captured_at: datetime | None = None
    extraction_model: str | None = None
    extraction_model_version: str | None = None
    extraction_confidence: Decimal | str | int | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureExtractedEvidenceCommand:
    """Capture extracted evidence observation contracts for one ingestion run."""

    ingestion_run_id: UUID
    supplier_catalogue_id: UUID
    source_file_id: UUID
    supplier_id: int
    observations: tuple[ExtractedEvidenceInput, ...]
    contract_id: str | None = None
    contract_version: str | None = None


@dataclass(frozen=True)
class BuildNormalizedRowCommand:
    """Build one interpreted claim from persisted extracted evidence observations."""

    raw_observation_ids: tuple[UUID, ...]
    raw_fields: ClaimRawFields | dict[str, Any]
    normalized_fields: NormalizedCatalogueFields | dict[str, Any]
    idempotency_key: str
    review_requirement: ReviewRequirement | None = None
    validation_issue_ids: tuple[UUID, ...] = ()
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluateNormalizedRowCommand:
    """Evaluate domain validation rules for one staged item."""

    catalogue_item_id: UUID
    evaluated_at: datetime | None = None


@dataclass(frozen=True)
class ResolveValidationIssueCommand:
    """Resolve a durable validation issue with an auditable decision."""

    validation_issue_id: UUID
    resolver_id: str
    resolution_status: IssueResolutionStatus
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class PrepareMasteringCandidateCommand:
    """Prepare one reviewable Mastering Candidate from a interpreted claim."""

    catalogue_item_id: UUID
    idempotency_key: str
    supplier_product_resolution: SupplierProductResolution | dict[str, Any] | None = None
    product_variant_resolution: ProductVariantResolution | dict[str, Any] | None = None
    packaging_resolution: PackagingConfigurationResolution | dict[str, Any] | None = None
    supplier_price_resolution: SupplierPriceResolution | dict[str, Any] | None = None
    mbb_resolution: MbbResolution | dict[str, Any] | None = None
    product_family_resolution: OptionalTextResolution | dict[str, Any] | None = None
    brand_resolution: OptionalTextResolution | dict[str, Any] | None = None
    category_resolution: OptionalTextResolution | dict[str, Any] | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordReviewDecisionCommand:
    """Record an explicit review decision for a Mastering Candidate."""

    mastering_candidate_id: UUID
    actor_id: str
    review_status: ReviewStatus
    decided_at: datetime | None = None
    reason: str | None = None
    override_reason: str | None = None
    expected_candidate_created_at: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ApplyApprovedCandidateCommand:
    """Apply an approved Mastering Candidate to supplier commercial state."""

    mastering_candidate_id: UUID
    applied_at: datetime | None = None


@dataclass(frozen=True)
class PublishServingItemCommand:
    """Publish an approved, applied Mastering Candidate as a serving snapshot."""

    mastering_candidate_id: UUID
    publication_version: str
    published_at: datetime | None = None
    idempotency_key: str | None = None


class _TransactionalService:
    """Base class with explicit transaction ownership."""

    def __init__(self, db: Session, *, commit: bool = True):
        self.db = db
        self.commit = commit

    def _finish(self) -> None:
        try:
            if self.commit:
                self.db.commit()
            else:
                self.db.flush()
        except IntegrityError as exc:
            if self.commit:
                self.db.rollback()
            raise ConcurrentModification(str(exc.orig)) from exc


class ExtractedEvidenceService(_TransactionalService):
    """Capture immutable raw evidence from supported supplier extraction output."""

    def capture(self, command: CaptureExtractedEvidenceCommand) -> StageResult:
        if not command.observations:
            return StageResult(stage="extracted_evidence_capture", metrics=StageMetrics(input_count=0))

        run, source_document, runtime_contract = _resolve_run_source_contract(self.db, command)
        trace_profile = ExtractionProfileReference(
            profile_id=runtime_contract.slug,
            profile_version=runtime_contract.version,
        )

        created = reused = 0
        output_ids: list[UUID] = []
        for item in command.observations:
            contract = self._contract_for_input(command, item, trace_profile)
            existing = persistence._extracted_evidence(self.db, contract.raw_observation_id)  # noqa: SLF001
            if existing is not None:
                _assert_same_material(
                    _raw_material(persistence.extracted_evidence_to_contract(existing)),
                    _raw_material(contract),
                    f"extracted evidence observation {contract.raw_observation_id}",
                )
                reused += 1
            else:
                persistence.persist_extracted_evidence(self.db, contract)
                created += 1
            output_ids.append(contract.raw_observation_id)

        if run.status in {"queued", "running"}:
            run.status = "running"
        if source_document.status == "active":
            source_document.updated_at = _iso(_now())
        self._finish()
        return StageResult(
            stage="extracted_evidence_capture",
            output_ids=tuple(output_ids),
            metrics=StageMetrics(input_count=len(command.observations), created_count=created, reused_count=reused),
        )

    def _contract_for_input(
        self,
        command: CaptureExtractedEvidenceCommand,
        item: ExtractedEvidenceInput,
        extraction_profile: ExtractionProfileReference,
    ) -> ExtractedEvidenceV1:
        source_location = SourceLocation.model_validate(item.source_location)
        raw_cells = [RawCell.model_validate(cell) for cell in item.raw_cells]
        material = {
            "run": str(command.ingestion_run_id),
            "source": str(command.supplier_catalogue_id),
            "file": str(command.source_file_id),
            "idempotency_key": item.idempotency_key,
        }
        observation_id = _stable_uuid("raw-observation", material)
        return ExtractedEvidenceV1.model_validate(
            {
                "contract_version": "catalogue.extracted_evidence.v1",
                "raw_observation_id": str(observation_id),
                "ingestion_run_id": str(command.ingestion_run_id),
                "supplier_catalogue_id": str(command.supplier_catalogue_id),
                "source_file_id": str(command.source_file_id),
                "extraction_profile": extraction_profile.model_dump(mode="json"),
                "source_location": source_location.model_dump(mode="json"),
                "raw_text": item.raw_text,
                "raw_cells": [cell.model_dump(mode="json") for cell in raw_cells],
                "extraction_method": _enum_value(item.extraction_method),
                "captured_at": _iso(item.captured_at or _now()),
                "extraction_model": item.extraction_model,
                "extraction_model_version": item.extraction_model_version,
                "extraction_confidence": item.extraction_confidence,
                "source_metadata": item.source_metadata,
            }
        )


class CatalogueValidationService(_TransactionalService):
    """Persist durable validation issues for cross-record and domain rules."""

    def evaluate_claim(self, command: EvaluateNormalizedRowCommand) -> StageResult:
        staging_row = _normalized_row_row(self.db, command.catalogue_item_id)
        staging = persistence.normalized_row_to_contract(staging_row)
        issue_specs = _claim_issue_specs(staging)

        created = reused = warning_count = blocking_count = 0
        issue_ids: list[UUID] = []
        for spec in issue_specs:
            issue = self._issue_for_spec(staging, spec, command.evaluated_at or _now())
            existing = persistence._validation_issue(self.db, issue.validation_issue_id)  # noqa: SLF001
            if existing is None:
                persistence.persist_validation_issue(self.db, issue)
                created += 1
                current_issue = issue
            else:
                existing_contract = persistence.validation_issue_to_contract(existing)
                _assert_same_material(
                    _issue_material(existing_contract),
                    _issue_material(issue),
                    f"Validation Issue {issue.validation_issue_id}",
                )
                reused += 1
                current_issue = existing_contract
            warning_count += 1 if current_issue.severity == IssueSeverity.WARNING else 0
            blocking_count += 1 if current_issue.publish_blocking else 0
            issue_ids.append(issue.validation_issue_id)

        if issue_specs:
            staging_row.stage_status = "NEEDS_REVIEW"
            existing_issue_ids = set(json.loads(staging_row.validation_issue_ids_json or "[]"))
            existing_issue_ids.update(str(issue_id) for issue_id in issue_ids)
            staging_row.validation_issue_ids_json = _json_dumps(sorted(existing_issue_ids))
        elif staging_row.stage_status == "NEEDS_REVIEW" and staging.review_requirement == ReviewRequirement.NOT_REQUIRED:
            staging_row.stage_status = "PROPOSED"
        self._finish()
        return StageResult(
            stage="validation",
            output_ids=tuple(issue_ids),
            issue_ids=tuple(issue_ids),
            metrics=StageMetrics(
                input_count=1,
                created_count=created,
                reused_count=reused,
                warning_count=warning_count,
                blocking_issue_count=blocking_count,
            ),
        )

    def resolve_issue(self, command: ResolveValidationIssueCommand) -> StageResult:
        if command.resolution_status == IssueResolutionStatus.OPEN:
            raise InvalidStageTransition("Validation issue resolution command cannot set OPEN status")
        issue = _validation_issue_row(self.db, command.validation_issue_id)
        if issue.resolution_status != IssueResolutionStatus.OPEN:
            if (
                issue.resolution_status == command.resolution_status.value
                and issue.resolver_id == command.resolver_id
                and (not command.resolution_note or issue.resolution_note == command.resolution_note)
            ):
                return StageResult(
                    stage="validation_resolution",
                    output_ids=(command.validation_issue_id,),
                    metrics=StageMetrics(input_count=1, reused_count=1),
                )
            raise IdempotencyConflict(f"Validation Issue {command.validation_issue_id} is already resolved differently")

        resolved_at = command.resolved_at or _now()
        issue.resolution_status = command.resolution_status.value
        issue.resolver_id = command.resolver_id
        issue.resolved_at = _iso(resolved_at)
        issue.resolution_note = command.resolution_note
        issue.publish_blocking = 0

        decision_id = _stable_uuid(
            "validation-review-decision",
            {
                "issue": str(command.validation_issue_id),
                "idempotency_key": command.idempotency_key
                or f"{command.resolution_status.value}:{command.resolver_id}:{_iso(resolved_at)}",
            },
        )
        if self.db.query(models.CatalogueReviewDecision).filter_by(review_decision_uuid=str(decision_id)).first() is None:
            self.db.add(
                models.CatalogueReviewDecision(
                    review_decision_uuid=str(decision_id),
                    validation_issue_uuid=str(command.validation_issue_id),
                    decision_type="validation_issue_resolution",
                    actor_id=command.resolver_id,
                    actor_display_name=command.resolver_id,
                    decided_at=_iso(resolved_at),
                    reason=command.resolution_note,
                    details_json=_json_dumps(
                        {
                            "resolution_status": command.resolution_status.value,
                            "issue_code": issue.issue_code,
                        }
                    ),
                    created_at=_iso(resolved_at),
                )
            )
        self._finish()
        return StageResult(
            stage="validation_resolution",
            output_ids=(command.validation_issue_id,),
            metrics=StageMetrics(input_count=1, created_count=1),
        )

    def _issue_for_spec(self, staging: NormalizedRowV1, spec: dict[str, Any], created_at: datetime) -> ValidationIssueV1:
        issue_id = _stable_uuid(
            "validation-issue",
            {
                "run": str(staging.trace.ingestion_run_id),
                "catalogue_item_id": str(staging.catalogue_item_id),
                "stage": spec["stage"].value,
                "code": spec["issue_code"],
                "field_path": spec.get("field_path"),
                "raw_value": spec.get("raw_value"),
                "proposed_value": spec.get("proposed_value"),
            },
        )
        return ValidationIssueV1.model_validate(
            {
                "contract_version": "catalogue.validation_issue.v1",
                "validation_issue_id": str(issue_id),
                "ingestion_run_id": str(staging.trace.ingestion_run_id),
                "catalogue_item_id": str(staging.catalogue_item_id),
                "raw_observation_id": str(staging.raw_observation_ids[0]) if staging.raw_observation_ids else None,
                "stage": spec["stage"].value,
                "issue_code": spec["issue_code"],
                "severity": spec["severity"].value,
                "message": spec["message"],
                "created_at": _iso(created_at),
                "resolution_status": IssueResolutionStatus.OPEN.value,
                "field_path": spec.get("field_path"),
                "raw_value": spec.get("raw_value"),
                "proposed_value": spec.get("proposed_value"),
                "expected_value": spec.get("expected_value"),
                "review_guidance": spec.get("review_guidance"),
            }
        )


class NormalizedRowService(_TransactionalService):
    """Persist interpreted claims (timeline step 6, INTERMEDIATE layer).

    Terminology: despite the historical "staging" name, these records are the
    Intermediate layer's interpreted claims — the supplier contract has
    already been applied by interpretation (step 5). The Staging layer's
    records are the extracted evidence observations (step 4).
    """

    def build_item(self, command: BuildNormalizedRowCommand) -> StageResult:
        if not command.raw_observation_ids:
            raise MissingOrIncompatibleLineage("An interpreted claim requires at least one extracted evidence observation")
        if len(command.raw_observation_ids) != len(set(command.raw_observation_ids)):
            raise MissingOrIncompatibleLineage("Interpreted-claim evidence lineage cannot contain duplicates")

        observations = [_extracted_evidence_row(self.db, raw_id) for raw_id in command.raw_observation_ids]
        _assert_same_raw_context(observations)
        first = observations[0]
        trace = PipelineTrace(
            ingestion_run_id=UUID(first.ingestion_run_uuid),
            supplier_catalogue_id=UUID(first.supplier_catalogue_uuid),
            source_file_id=UUID(first.source_file_uuid),
            extraction_profile=ExtractionProfileReference(
                profile_id=first.extraction_profile_id,
                profile_version=first.extraction_profile_version,
            ),
        )
        raw_fields = ClaimRawFields.model_validate(command.raw_fields)
        normalized_fields = NormalizedCatalogueFields.model_validate(command.normalized_fields)
        review_requirement = command.review_requirement or _review_requirement(raw_fields, normalized_fields, command.validation_issue_ids)
        catalogue_item_id = _stable_uuid(
            "staging-item",
            {
                "run": first.ingestion_run_uuid,
                "idempotency_key": command.idempotency_key,
            },
        )
        contract = NormalizedRowV1.model_validate(
            {
                "contract_version": "catalogue.normalized_row.v1",
                "trace": trace.model_dump(mode="json"),
                "catalogue_item_id": str(catalogue_item_id),
                "raw_observation_ids": [str(item) for item in command.raw_observation_ids],
                "raw_fields": raw_fields.model_dump(mode="json"),
                "normalized_fields": normalized_fields.model_dump(mode="json"),
                "review_requirement": review_requirement.value,
                "validation_issue_ids": [str(item) for item in command.validation_issue_ids],
                "created_at": _iso(command.created_at or _now()),
                "metadata": command.metadata,
            }
        )

        existing = persistence._normalized_row(self.db, catalogue_item_id)  # noqa: SLF001
        if existing is not None:
            _assert_same_material(
                _claim_material(persistence.normalized_row_to_contract(existing)),
                _claim_material(contract),
                f"interpreted claim {catalogue_item_id}",
            )
            reused = 1
            created = 0
        else:
            persistence.persist_normalized_row(self.db, contract)
            reused = 0
            created = 1
        self._finish()
        return StageResult(
            stage="intermediate_normalization",
            output_ids=(catalogue_item_id,),
            metrics=StageMetrics(input_count=len(command.raw_observation_ids), created_count=created, reused_count=reused),
        )


class MasteringService(_TransactionalService):
    """Prepare reviewable mastering candidates from eligible interpreted claims."""

    def prepare_candidate(self, command: PrepareMasteringCandidateCommand) -> StageResult:
        staging_row = _normalized_row_row(self.db, command.catalogue_item_id)
        _raise_if_open_blocking(self.db, catalogue_item_uuid=str(command.catalogue_item_id))
        staging = persistence.normalized_row_to_contract(staging_row)
        candidate_id = _stable_uuid(
            "mastering-candidate",
            {
                "catalogue_item_id": str(command.catalogue_item_id),
                "idempotency_key": command.idempotency_key,
            },
        )
        lineage = LineageReference(
            catalogue_item_id=staging.catalogue_item_id,
            raw_observation_ids=staging.raw_observation_ids,
        )
        contract = MasteringCandidateV1.model_validate(
            {
                "contract_version": "catalogue.mastering_candidate.v1",
                "mastering_candidate_id": str(candidate_id),
                "trace": staging.trace.model_dump(mode="json"),
                "catalogue_item_id": str(staging.catalogue_item_id),
                "raw_observation_ids": [str(item) for item in staging.raw_observation_ids],
                "lineage": lineage.model_dump(mode="json"),
                "supplier_product_resolution": _model_or_default(
                    command.supplier_product_resolution,
                    _default_supplier_product_resolution(self.db, staging),
                    SupplierProductResolution,
                ),
                "product_variant_resolution": _model_or_default(
                    command.product_variant_resolution,
                    _default_product_variant_resolution(self.db, staging),
                    ProductVariantResolution,
                ),
                "packaging_resolution": _model_or_default(
                    command.packaging_resolution,
                    _default_packaging_resolution(staging),
                    PackagingConfigurationResolution,
                ),
                "supplier_price_resolution": _model_or_default(
                    command.supplier_price_resolution,
                    _default_supplier_price_resolution(staging),
                    SupplierPriceResolution,
                ),
                "mbb_resolution": _model_or_default(command.mbb_resolution, _default_mbb_resolution(staging), MbbResolution),
                "review_status": ReviewStatus.PENDING_REVIEW.value,
                "product_family_resolution": _optional_model(command.product_family_resolution, OptionalTextResolution),
                "brand_resolution": _optional_model(command.brand_resolution, OptionalTextResolution),
                "category_resolution": _optional_model(command.category_resolution, OptionalTextResolution),
                "created_at": _iso(command.created_at or _now()),
                "metadata": command.metadata,
            }
        )

        existing = persistence._mastering_candidate(self.db, candidate_id)  # noqa: SLF001
        if existing is not None:
            _assert_same_material(
                _candidate_material(persistence.mastering_candidate_to_contract(existing), include_review=False),
                _candidate_material(contract, include_review=False),
                f"Mastering Candidate {candidate_id}",
            )
            created = 0
            reused = 1
        else:
            persistence.persist_mastering_candidate(self.db, contract)
            created = 1
            reused = 0
        self._finish()
        return StageResult(
            stage="mastering",
            output_ids=(candidate_id,),
            metrics=StageMetrics(input_count=1, created_count=created, reused_count=reused),
        )


class ReviewDecisionService(_TransactionalService):
    """Record explicit append-only review decisions for mastering candidates."""

    def record_decision(self, command: RecordReviewDecisionCommand) -> StageResult:
        if command.review_status == ReviewStatus.PENDING_REVIEW:
            raise InvalidStageTransition("A review decision cannot set PENDING_REVIEW")
        if command.review_status == ReviewStatus.APPROVED_WITH_OVERRIDE and not command.override_reason:
            raise InvalidStageTransition("APPROVED_WITH_OVERRIDE requires override_reason")

        candidate = _candidate_row(self.db, command.mastering_candidate_id)
        if command.expected_candidate_created_at and candidate.created_at != command.expected_candidate_created_at:
            raise StaleCandidateRevision(
                f"Mastering Candidate {command.mastering_candidate_id} current revision is {candidate.created_at}; "
                f"requested {command.expected_candidate_created_at}"
            )
        if command.review_status in {ReviewStatus.APPROVED, ReviewStatus.APPROVED_WITH_OVERRIDE}:
            _raise_if_open_blocking(self.db, catalogue_item_uuid=candidate.catalogue_item_uuid)

        decided_at = command.decided_at or _now()
        decision_id = _stable_uuid(
            "mastering-review-decision",
            {
                "candidate": str(command.mastering_candidate_id),
                "idempotency_key": command.idempotency_key
                or f"{command.review_status.value}:{command.actor_id}:{_iso(decided_at)}",
            },
        )
        existing_decision = self.db.query(models.CatalogueReviewDecision).filter_by(review_decision_uuid=str(decision_id)).first()
        if existing_decision is not None:
            if candidate.review_decision_uuid == str(decision_id) and candidate.review_status == command.review_status.value:
                return StageResult(
                    stage="review_decision",
                    output_ids=(decision_id,),
                    metrics=StageMetrics(input_count=1, reused_count=1),
                )
            raise IdempotencyConflict(f"Review decision {decision_id} is already associated with different candidate state")

        if candidate.review_status != ReviewStatus.PENDING_REVIEW.value:
            if candidate.review_status == command.review_status.value and candidate.reviewed_by == command.actor_id:
                return StageResult(
                    stage="review_decision",
                    output_ids=(UUID(candidate.review_decision_uuid),) if candidate.review_decision_uuid else (),
                    metrics=StageMetrics(input_count=1, reused_count=1),
                )
            raise InvalidStageTransition(
                f"Mastering Candidate {command.mastering_candidate_id} is {candidate.review_status}; "
                f"cannot transition to {command.review_status.value}"
            )

        snapshot = persistence.mastering_candidate_to_contract(candidate).model_dump(mode="json")
        self.db.add(
            models.CatalogueReviewDecision(
                review_decision_uuid=str(decision_id),
                mastering_candidate_uuid=str(command.mastering_candidate_id),
                decision_type="mastering_review",
                review_status=command.review_status.value,
                actor_id=command.actor_id,
                actor_display_name=command.actor_id,
                decided_at=_iso(decided_at),
                reason=command.reason,
                override_reason=command.override_reason,
                details_json=_json_dumps({"candidate_snapshot": snapshot}),
                created_at=_iso(decided_at),
            )
        )
        candidate.review_status = command.review_status.value
        candidate.reviewed_by = command.actor_id
        candidate.reviewed_at = _iso(decided_at)
        candidate.override_reason = command.override_reason
        candidate.review_decision_uuid = str(decision_id)
        if command.review_status in {ReviewStatus.REJECTED, ReviewStatus.NEEDS_CLARIFICATION}:
            staging = _normalized_row_row(self.db, UUID(candidate.catalogue_item_uuid))
            staging.stage_status = "NEEDS_REVIEW"
        self._finish()
        return StageResult(
            stage="review_decision",
            output_ids=(decision_id,),
            metrics=StageMetrics(input_count=1, created_count=1),
        )


class ApprovedCommercialStateService(_TransactionalService):
    """Apply an approved candidate to supplier-scoped commercial state."""

    def apply_approved_candidate(self, command: ApplyApprovedCandidateCommand) -> StageResult:
        candidate_row = _candidate_row(self.db, command.mastering_candidate_id)
        if candidate_row.review_status not in {ReviewStatus.APPROVED.value, ReviewStatus.APPROVED_WITH_OVERRIDE.value}:
            raise InvalidStageTransition(
                f"Mastering Candidate {command.mastering_candidate_id} is {candidate_row.review_status}; "
                "approved commercial state requires APPROVED or APPROVED_WITH_OVERRIDE"
            )
        _raise_if_open_blocking(self.db, catalogue_item_uuid=candidate_row.catalogue_item_uuid)
        candidate = persistence.mastering_candidate_to_contract(candidate_row)
        applied_at = command.applied_at or _now()

        supplier_product_key = _candidate_supplier_product_key(candidate)
        existing_supplier_product = self.db.query(models.CatalogueSupplierProduct).filter_by(
            supplier_product_key=supplier_product_key
        ).first()
        current_price = None
        if existing_supplier_product is not None:
            current_price = (
                self.db.query(models.CatalogueSupplierPrice)
                .filter_by(
                    supplier_product_id=existing_supplier_product.id,
                    mastering_candidate_uuid=str(candidate.mastering_candidate_id),
                    is_current=1,
                )
                .first()
            )
        if existing_supplier_product is not None and current_price is not None:
            return StageResult(
                stage="commercial_application",
                output_ids=(supplier_product_key,),
                metrics=StageMetrics(input_count=1, reused_count=1),
            )

        supplier_product = existing_supplier_product or self._create_supplier_product(candidate, supplier_product_key, applied_at)
        if existing_supplier_product is not None:
            self._update_supplier_product(existing_supplier_product, candidate, applied_at)
        self._persist_packaging(candidate, supplier_product, applied_at)
        price = self._persist_price(candidate, supplier_product, applied_at)
        mbb_count = self._persist_mbb(candidate, supplier_product, applied_at)
        self._finish()
        return StageResult(
            stage="commercial_application",
            output_ids=(supplier_product_key,),
            metrics=StageMetrics(input_count=1, created_count=1, warning_count=mbb_count),
        )

    def _create_supplier_product(
        self,
        candidate: MasteringCandidateV1,
        supplier_product_key: str,
        applied_at: datetime,
    ) -> models.CatalogueSupplierProduct:
        supplier_resolution = candidate.supplier_product_resolution
        product_resolution = candidate.product_variant_resolution
        supplier_id = supplier_resolution.supplier_id or _supplier_id_from_source(self.db, candidate.trace.supplier_catalogue_id)
        if supplier_id is None:
            raise AmbiguousSupplierOffer("Supplier Product application requires supplier_id")
        product_id = _product_id_for_sku(self.db, product_resolution.canonical_sku)
        row = models.CatalogueSupplierProduct(
            supplier_product_key=supplier_product_key,
            supplier_id=supplier_id,
            product_variant_id=product_id,
            supplier_sku=supplier_resolution.supplier_sku,
            barcode=supplier_resolution.barcode,
            status="active",
            approved_review_decision_uuid=str(candidate.review_decision_id) if candidate.review_decision_id else None,
            created_at=_iso(applied_at),
            updated_at=_iso(applied_at),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _update_supplier_product(
        self,
        supplier_product: models.CatalogueSupplierProduct,
        candidate: MasteringCandidateV1,
        applied_at: datetime,
    ) -> None:
        supplier_product.supplier_sku = candidate.supplier_product_resolution.supplier_sku
        supplier_product.barcode = candidate.supplier_product_resolution.barcode
        supplier_product.product_variant_id = _product_id_for_sku(self.db, candidate.product_variant_resolution.canonical_sku)
        supplier_product.approved_review_decision_uuid = str(candidate.review_decision_id) if candidate.review_decision_id else None
        supplier_product.updated_at = _iso(applied_at)

    def _persist_packaging(
        self,
        candidate: MasteringCandidateV1,
        supplier_product: models.CatalogueSupplierProduct,
        applied_at: datetime,
    ) -> None:
        packaging = candidate.packaging_resolution.packaging
        if packaging is None:
            raise InvalidStageTransition("Approved commercial application requires resolved packaging")
        for current in (
            self.db.query(models.CataloguePackagingConfiguration)
            .filter_by(supplier_product_id=supplier_product.id, superseded_at=None)
            .all()
        ):
            current.superseded_at = _iso(applied_at)
            current.effective_to = current.effective_to or _iso(applied_at)
        self.db.add(
            models.CataloguePackagingConfiguration(
                supplier_product_id=supplier_product.id,
                purchase_uom_code=_uom_code(packaging.purchase_uom),
                purchase_uom_label=_uom_label(packaging.purchase_uom),
                price_basis_uom_code=_uom_code(packaging.price_basis),
                price_basis_uom_label=_uom_label(packaging.price_basis),
                sellable_unit_uom_code=_uom_code(packaging.sellable_unit_uom),
                sellable_unit_uom_label=_uom_label(packaging.sellable_unit_uom),
                sellable_units_per_purchase_unit=packaging.sellable_units_per_purchase_unit,
                content_amount=packaging.content_amount,
                content_uom_code=_uom_code(packaging.content_uom),
                content_uom_label=_uom_label(packaging.content_uom),
                order_increment_amount=packaging.order_increment.amount if packaging.order_increment else None,
                order_increment_uom_code=_uom_code(packaging.order_increment.uom) if packaging.order_increment else None,
                order_increment_uom_label=_uom_label(packaging.order_increment.uom) if packaging.order_increment else None,
                minimum_order_amount=packaging.minimum_order_quantity.amount if packaging.minimum_order_quantity else None,
                minimum_order_uom_code=_uom_code(packaging.minimum_order_quantity.uom) if packaging.minimum_order_quantity else None,
                minimum_order_uom_label=_uom_label(packaging.minimum_order_quantity.uom) if packaging.minimum_order_quantity else None,
                break_pack_allowed=None if packaging.break_pack_allowed is None else int(packaging.break_pack_allowed),
                source_text=packaging.source_text,
                effective_from=_iso(applied_at),
                review_decision_uuid=str(candidate.review_decision_id) if candidate.review_decision_id else None,
                raw_observation_ids_json=_json_dumps([str(item) for item in candidate.raw_observation_ids]),
                created_at=_iso(applied_at),
            )
        )
        self.db.flush()

    def _persist_price(
        self,
        candidate: MasteringCandidateV1,
        supplier_product: models.CatalogueSupplierProduct,
        applied_at: datetime,
    ) -> models.CatalogueSupplierPrice:
        cost = candidate.supplier_price_resolution.current_cost
        if cost is None:
            raise InvalidStageTransition("Approved commercial application requires resolved supplier cost")
        for current in self.db.query(models.CatalogueSupplierPrice).filter_by(
            supplier_product_id=supplier_product.id,
            is_current=1,
        ):
            current.is_current = 0
            current.effective_to = current.effective_to or _iso(applied_at)
            current.superseded_at = _iso(applied_at)
        row = models.CatalogueSupplierPrice(
            supplier_product_id=supplier_product.id,
            amount=cost.amount,
            currency=cost.currency,
            price_basis_uom_code=cost.price_basis.code.value,
            price_basis_uom_label=cost.price_basis.label,
            effective_from=_iso(candidate.supplier_price_resolution.effective_from or applied_at),
            effective_to=_iso(candidate.supplier_price_resolution.effective_to) if candidate.supplier_price_resolution.effective_to else None,
            source_document_id=_source_document_id(self.db, candidate.trace.supplier_catalogue_id),
            ingestion_run_uuid=str(candidate.trace.ingestion_run_id),
            mastering_candidate_uuid=str(candidate.mastering_candidate_id),
            review_decision_uuid=str(candidate.review_decision_id) if candidate.review_decision_id else None,
            is_current=1,
            created_at=_iso(applied_at),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _persist_mbb(
        self,
        candidate: MasteringCandidateV1,
        supplier_product: models.CatalogueSupplierProduct,
        applied_at: datetime,
    ) -> int:
        terms = candidate.mbb_resolution.terms
        if not terms:
            return 0
        for current in self.db.query(models.CatalogueSupplierMbbTerm).filter_by(
            supplier_product_id=supplier_product.id,
            is_active=1,
        ):
            current.is_active = 0
            current.superseded_at = _iso(applied_at)
        for term in terms:
            self.db.add(_mbb_row(term, candidate, supplier_product.id, applied_at, self.db))
        self.db.flush()
        return len(terms)


class ServingPublicationService(_TransactionalService):
    """Publish approved and applied commercial state as an immutable serving snapshot."""

    def publish(self, command: PublishServingItemCommand) -> StageResult:
        candidate_row = _candidate_row(self.db, command.mastering_candidate_id)
        if candidate_row.review_status not in {ReviewStatus.APPROVED.value, ReviewStatus.APPROVED_WITH_OVERRIDE.value}:
            raise PublicationIneligible("Serving publication requires an approved Mastering Candidate")
        _raise_if_open_blocking(self.db, catalogue_item_uuid=candidate_row.catalogue_item_uuid)
        candidate = persistence.mastering_candidate_to_contract(candidate_row)
        supplier_product_key = _candidate_supplier_product_key(candidate)
        supplier_product = self.db.query(models.CatalogueSupplierProduct).filter_by(
            supplier_product_key=supplier_product_key
        ).first()
        if supplier_product is None:
            raise PublicationIneligible("Serving publication requires applied Supplier Offer state")
        price = (
            self.db.query(models.CatalogueSupplierPrice)
            .filter_by(
                supplier_product_id=supplier_product.id,
                mastering_candidate_uuid=str(candidate.mastering_candidate_id),
                is_current=1,
            )
            .first()
        )
        if price is None:
            raise PublicationIneligible("Serving publication requires applied current supplier price")
        packaging = (
            self.db.query(models.CataloguePackagingConfiguration)
            .filter_by(supplier_product_id=supplier_product.id, superseded_at=None)
            .order_by(models.CataloguePackagingConfiguration.id.desc())
            .first()
        )
        if packaging is None:
            raise PublicationIneligible("Serving publication requires applied packaging configuration")

        published_at = command.published_at or _now()
        serving_item_id = _stable_uuid(
            "serving-publication",
            {
                "candidate": str(command.mastering_candidate_id),
                "idempotency_key": command.idempotency_key or command.publication_version,
            },
        )
        contract = _serving_contract_from_state(
            self.db,
            serving_item_id=serving_item_id,
            candidate=candidate,
            supplier_product=supplier_product,
            price=price,
            packaging_row=packaging,
            publication_version=command.publication_version,
            published_at=published_at,
        )
        existing = persistence._serving_publication(self.db, serving_item_id)  # noqa: SLF001
        if existing is not None:
            _assert_same_material(
                _serving_material(persistence.serving_item_to_contract(existing)),
                _serving_material(contract),
                f"Serving Item {serving_item_id}",
            )
            return StageResult(
                stage="serving_publication",
                output_ids=(serving_item_id,),
                metrics=StageMetrics(input_count=1, reused_count=1),
            )

        publication_key = _publication_key(contract)
        for current in self.db.query(models.CatalogueServingPublication).filter_by(publication_key=publication_key, is_current=1).all():
            current.is_current = 0
            current.superseded_at = _iso(published_at)
        self.db.add(
            models.CatalogueServingPublication(
                serving_item_uuid=str(contract.serving_item_id),
                contract_version=contract.contract_version,
                publication_key=publication_key,
                publication_version=contract.lineage.publication_version,
                canonical_sku=contract.canonical_sku,
                product_variant_key=contract.product_variant_id,
                product_variant_name=contract.product_variant_name,
                product_id=_product_id_for_sku(self.db, contract.canonical_sku),
                supplier_id=contract.supplier_offering.supplier_id,
                supplier_product_id=supplier_product.id,
                supplier_product_key=supplier_product.supplier_product_key,
                supplier_sku=contract.supplier_offering.supplier_sku,
                barcode=contract.supplier_offering.barcode,
                current_approved_cost_amount=contract.current_approved_cost.amount,
                current_approved_cost_currency=contract.current_approved_cost.currency,
                current_approved_cost_basis_uom_code=contract.current_approved_cost.price_basis.code.value,
                current_approved_cost_basis_uom_label=contract.current_approved_cost.price_basis.label,
                cost_per_sellable_unit_amount=contract.cost_per_sellable_unit.amount if contract.cost_per_sellable_unit else None,
                cost_per_sellable_unit_currency=contract.cost_per_sellable_unit.currency if contract.cost_per_sellable_unit else None,
                review_status=contract.review_status.value,
                published_at=_iso(published_at),
                mastering_candidate_uuid=str(contract.lineage.mastering_candidate_id),
                catalogue_item_uuid=str(contract.lineage.catalogue_item_id),
                raw_observation_ids_json=_json_dumps([str(item) for item in contract.lineage.raw_observation_ids]),
                lineage_json=_json_dumps(contract.lineage.model_dump(mode="json")),
                snapshot_json=_json_dumps(contract.model_dump(mode="json")),
                is_current=1,
                created_at=_iso(published_at),
            )
        )
        self._finish()
        return StageResult(
            stage="serving_publication",
            output_ids=(serving_item_id,),
            metrics=StageMetrics(input_count=1, created_count=1),
        )


def _resolve_run_source_contract(
    db: Session,
    command: CaptureExtractedEvidenceCommand,
) -> tuple[models.IngestionRun, models.CatalogueSourceDocument, supplier_source_contract_runtime.SupplierSourceRuntimeContract]:
    run = db.query(models.IngestionRun).filter_by(run_uuid=str(command.ingestion_run_id)).first()
    if run is None:
        raise UpstreamRecordNotFound(f"Ingestion Run {command.ingestion_run_id} does not exist")
    source_document = db.query(models.CatalogueSourceDocument).filter_by(
        supplier_catalogue_uuid=str(command.supplier_catalogue_id)
    ).first()
    if source_document is None:
        raise UpstreamRecordNotFound(f"Catalogue Source Document {command.supplier_catalogue_id} does not exist")
    if source_document.source_file_uuid != str(command.source_file_id):
        raise MissingOrIncompatibleLineage("Raw capture source_file_id does not match source document")
    if run.catalogue_source_document_id and run.catalogue_source_document_id != source_document.id:
        raise MissingOrIncompatibleLineage("Ingestion Run does not belong to the supplied source document")
    if run.supplier_id and run.supplier_id != command.supplier_id:
        raise SupplierContractMismatch(f"Ingestion Run supplier_id={run.supplier_id} does not match command supplier_id={command.supplier_id}")
    if source_document.supplier_id and source_document.supplier_id != command.supplier_id:
        raise SupplierContractMismatch(
            f"Source Document supplier_id={source_document.supplier_id} does not match command supplier_id={command.supplier_id}"
        )

    contract_id = command.contract_id or run.supplier_source_contract_id or source_document.supplier_source_contract_id
    contract_version = command.contract_version or run.supplier_source_contract_version or source_document.supplier_source_contract_version
    try:
        runtime_contract = supplier_source_contract_runtime.resolve_supplier_contract(
            supplier_id=command.supplier_id,
            contract_id=contract_id,
            contract_version=contract_version,
        )
    except supplier_source_contract_runtime.SupplierContractResolutionError as exc:
        raise UnsupportedSupplierContract(str(exc)) from exc

    _assert_recorded_contract("Ingestion Run", run.supplier_source_contract_id, run.supplier_source_contract_version, runtime_contract)
    _assert_recorded_contract(
        "Source Document",
        source_document.supplier_source_contract_id,
        source_document.supplier_source_contract_version,
        runtime_contract,
    )
    return run, source_document, runtime_contract


def _assert_recorded_contract(label: str, contract_id: str | None, version: str | None, runtime_contract) -> None:
    if contract_id and contract_id != runtime_contract.slug:
        raise SupplierContractMismatch(f"{label} contract_id={contract_id} does not match resolved {runtime_contract.slug}")
    if version and version != runtime_contract.version:
        raise SupplierContractMismatch(f"{label} contract_version={version} does not match resolved {runtime_contract.version}")


def _claim_issue_specs(staging: NormalizedRowV1) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = _contract_execution_issue_specs(staging)
    if staging.raw_fields.cost and staging.normalized_fields.cost is None:
        specs.append(
            {
                "stage": ValidationStage.STAGING,
                "issue_code": "STAGING_COST_BASIS_UNRESOLVED",
                "severity": IssueSeverity.BLOCKING,
                "message": "Rosetta observed a source cost but does not yet know the amount, currency, or price basis.",
                "field_path": "/normalized_fields/cost",
                "raw_value": staging.raw_fields.cost,
                "expected_value": "Review the source row and decide the HKD amount and price basis.",
                "review_guidance": "Confirm the supplier cost amount and what one quoted price buys before approval.",
            }
        )
    if staging.raw_fields.packaging and staging.normalized_fields.packaging is None:
        specs.append(
            {
                "stage": ValidationStage.STAGING,
                "issue_code": "STAGING_PACKAGING_UNRESOLVED",
                "severity": IssueSeverity.WARNING,
                "message": "Rosetta observed packaging text but has not proposed purchase, sellable-unit, and content semantics.",
                "field_path": "/normalized_fields/packaging",
                "raw_value": staging.raw_fields.packaging,
                "expected_value": "Structured packaging or explicit decision to leave unknown.",
                "review_guidance": "Decide whether the text is purchase packaging, sellable units, content measure, or order multiple.",
            }
        )
    packaging = staging.normalized_fields.packaging
    if packaging and packaging.content_amount is not None and packaging.sellable_units_per_purchase_unit == packaging.content_amount:
        content_uom = packaging.content_uom.code.value if packaging.content_uom and packaging.content_uom.code else None
        if content_uom in {UnitCode.ML.value, UnitCode.G.value, UnitCode.KG.value, UnitCode.L.value}:
            specs.append(
                {
                    "stage": ValidationStage.STAGING,
                    "issue_code": "STAGING_CONTENT_MEASURE_AS_SELLABLE_COUNT",
                    "severity": IssueSeverity.BLOCKING,
                    "message": "Content measurement appears to have been reused as a sellable-unit count.",
                    "field_path": "/normalized_fields/packaging/sellable_units_per_purchase_unit",
                    "raw_value": staging.raw_fields.packaging,
                    "proposed_value": str(packaging.sellable_units_per_purchase_unit),
                    "expected_value": "Sellable-unit count must be separate from content amount.",
                    "review_guidance": "Confirm the number of sellable units separately; do not use mL or grams as the count.",
                }
            )
    return specs


def _contract_execution_issue_specs(staging: NormalizedRowV1) -> list[dict[str, Any]]:
    """Promote Phase 2/3 contract issues from claim metadata into durable issues."""

    payload = staging.metadata.get("contract_execution_issues", [])
    if not isinstance(payload, list):
        return [_invalid_contract_issue_spec("contract_execution_issues must be a list")]

    specs: list[dict[str, Any]] = []
    for index, raw_issue in enumerate(payload):
        if not isinstance(raw_issue, dict):
            specs.append(_invalid_contract_issue_spec(f"entry {index} must be an object"))
            continue
        issue_code = raw_issue.get("issue_code")
        message = raw_issue.get("message")
        try:
            severity = IssueSeverity(raw_issue.get("severity", IssueSeverity.WARNING.value))
        except ValueError:
            specs.append(_invalid_contract_issue_spec(f"entry {index} has an unsupported severity"))
            continue
        if not isinstance(issue_code, str) or not issue_code.strip():
            specs.append(_invalid_contract_issue_spec(f"entry {index} has no issue_code"))
            continue
        if not isinstance(message, str) or not message.strip():
            specs.append(_invalid_contract_issue_spec(f"entry {index} has no business-readable message"))
            continue

        field_key = raw_issue.get("field_key")
        field_path, raw_value = _contract_issue_field_context(staging, field_key)
        specs.append(
            {
                "stage": ValidationStage.STAGING,
                "issue_code": issue_code.strip(),
                "severity": severity,
                "message": message.strip(),
                "field_path": field_path,
                "raw_value": raw_value,
                "expected_value": "The supplier-contract requirement must be satisfied or explicitly reviewed.",
                "review_guidance": "Compare the interpreted claim with its linked source evidence and record a decision.",
            }
        )
    return specs


def _invalid_contract_issue_spec(detail: str) -> dict[str, Any]:
    return {
        "stage": ValidationStage.STAGING,
        "issue_code": "CONTRACT_ISSUE_METADATA_INVALID",
        "severity": IssueSeverity.BLOCKING,
        "message": f"A contract execution issue could not be persisted safely: {detail}.",
        "field_path": "/metadata/contract_execution_issues",
        "expected_value": "A list of typed, business-readable contract execution issues.",
        "review_guidance": "Escalate this ingestion for technical review before approving the interpreted claim.",
    }


def _contract_issue_field_context(staging: NormalizedRowV1, field_key: Any) -> tuple[str, Any]:
    if not isinstance(field_key, str) or not field_key.strip():
        return "/metadata/contract_execution_issues", None
    key = field_key.strip()
    normalized_paths = {
        "cost": "/normalized_fields/cost",
        "rrp": "/normalized_fields/rrp",
        "pack_size": "/normalized_fields/packaging",
        "packaging": "/normalized_fields/packaging",
        "effective_date": "/normalized_fields/effective_date",
        "mbb_text": "/normalized_fields/mbb_terms",
    }
    raw_payload = staging.raw_fields.model_dump(mode="json")
    if key in raw_payload:
        return normalized_paths.get(key, f"/raw_fields/{key}"), raw_payload.get(key)
    additional = raw_payload.get("additional_fields") or {}
    if key in additional:
        return normalized_paths.get(key, f"/raw_fields/additional_fields/{key}"), additional.get(key)
    return f"/metadata/contract_execution_issues/{key}", None


def _review_requirement(
    raw_fields: ClaimRawFields,
    normalized_fields: NormalizedCatalogueFields,
    validation_issue_ids: tuple[UUID, ...],
) -> ReviewRequirement:
    if validation_issue_ids:
        return ReviewRequirement.REQUIRED
    if raw_fields.cost and normalized_fields.cost is None:
        return ReviewRequirement.BLOCKING
    if raw_fields.packaging and normalized_fields.packaging is None:
        return ReviewRequirement.RECOMMENDED
    return ReviewRequirement.NOT_REQUIRED


def _default_supplier_product_resolution(db: Session, staging: NormalizedRowV1) -> dict[str, Any]:
    supplier_id = _supplier_id_from_source(db, staging.trace.supplier_catalogue_id)
    supplier_sku = _proposal_text(staging.normalized_fields.supplier_sku) or staging.raw_fields.supplier_sku
    barcode = _proposal_text(staging.normalized_fields.barcode) or staging.raw_fields.barcode
    matches = _supplier_product_matches(db, supplier_id=supplier_id, supplier_sku=supplier_sku, barcode=barcode)
    if len(matches) > 1:
        raise AmbiguousSupplierOffer("Supplier SKU/barcode resolves to multiple supplier offerings")
    if matches:
        match = matches[0]
        return {
            "state": ResolutionState.PROPOSED_MATCH.value,
            "confidence": "1",
            "supplier_id": supplier_id,
            "supplier_product_id": match["supplier_product_id"],
            "supplier_sku": supplier_sku,
            "barcode": barcode,
        }
    return {
        "state": ResolutionState.PROPOSED_CREATE.value if supplier_sku else ResolutionState.UNRESOLVED.value,
        "supplier_id": supplier_id,
        "supplier_product_id": f"supplier:{supplier_id}:offer:{supplier_sku}" if supplier_id and supplier_sku else None,
        "supplier_sku": supplier_sku,
        "barcode": barcode,
    }


def _default_product_variant_resolution(db: Session, staging: NormalizedRowV1) -> dict[str, Any]:
    proposed_name = _proposal_text(staging.normalized_fields.product_name) or staging.raw_fields.product_name
    supplier_sku = _proposal_text(staging.normalized_fields.supplier_sku) or staging.raw_fields.supplier_sku
    barcode = _proposal_text(staging.normalized_fields.barcode) or staging.raw_fields.barcode
    supplier_id = _supplier_id_from_source(db, staging.trace.supplier_catalogue_id)
    product = _exact_product_match(
        db,
        supplier_id=supplier_id,
        supplier_sku=supplier_sku,
        barcode=barcode,
    )
    if product is not None:
        return {
            "state": ResolutionState.PROPOSED_MATCH.value,
            "confidence": "1",
            "canonical_sku": product.sku_code,
            "product_variant_id": product.sku_code,
            "product_variant_name": product.name,
            "proposed_name": proposed_name,
            "product_family_id": None,
        }
    return {
        "state": ResolutionState.PROPOSED_CREATE.value if proposed_name else ResolutionState.UNRESOLVED.value,
        "canonical_sku": None,
        "product_variant_id": None,
        "product_variant_name": proposed_name,
        "proposed_name": proposed_name,
        "product_family_id": None,
    }


def _supplier_product_matches(
    db: Session,
    *,
    supplier_id: int | None,
    supplier_sku: str | None,
    barcode: str | None,
) -> list[dict[str, Any]]:
    if supplier_id is None:
        return []
    matches: dict[str, dict[str, Any]] = {}
    current_rows = db.query(models.CatalogueSupplierProduct).filter_by(supplier_id=supplier_id).all()
    for row in current_rows:
        if (supplier_sku and row.supplier_sku == supplier_sku) or (barcode and row.barcode == barcode):
            matches[row.supplier_product_key] = {
                "supplier_product_id": row.supplier_product_key,
                "product_id": row.product_variant_id,
            }
    if matches:
        return list(matches.values())
    legacy_rows = db.query(models.ProductSupplier).filter_by(supplier_id=supplier_id).all()
    for row in legacy_rows:
        if (supplier_sku and row.supplier_sku == supplier_sku) or (barcode and row.barcode == barcode):
            key = f"legacy-product-supplier:{row.id}"
            matches[key] = {"supplier_product_id": key, "product_id": row.product_id}
    return list(matches.values())


def _exact_product_match(
    db: Session,
    *,
    supplier_id: int | None,
    supplier_sku: str | None,
    barcode: str | None,
):
    supplier_matches = _supplier_product_matches(
        db,
        supplier_id=supplier_id,
        supplier_sku=supplier_sku,
        barcode=barcode,
    )
    product_ids = {match["product_id"] for match in supplier_matches if match.get("product_id")}
    if len(product_ids) > 1:
        raise AmbiguousProductVariant("Supplier identity resolves to multiple canonical products")
    if product_ids:
        return db.get(models.Product, next(iter(product_ids)))
    if supplier_sku:
        return db.query(models.Product).filter_by(sku_code=supplier_sku).first()
    return None


def _default_packaging_resolution(staging: NormalizedRowV1) -> dict[str, Any]:
    packaging = staging.normalized_fields.packaging
    return {
        "state": ResolutionState.PROPOSED_CREATE.value if packaging else ResolutionState.UNRESOLVED.value,
        "packaging": packaging.model_dump(mode="json", exclude={"evidence"}) if packaging else None,
    }


def _default_supplier_price_resolution(staging: NormalizedRowV1) -> dict[str, Any]:
    cost = staging.normalized_fields.cost
    return {
        "state": ResolutionState.PROPOSED_CREATE.value if cost else ResolutionState.UNRESOLVED.value,
        "current_cost": cost.model_dump(mode="json", exclude={"evidence"}) if cost else None,
    }


def _default_mbb_resolution(staging: NormalizedRowV1) -> dict[str, Any]:
    terms = staging.normalized_fields.mbb_terms
    return {
        "state": ResolutionState.PROPOSED_CREATE.value if terms else ResolutionState.UNRESOLVED.value,
        "terms": [term.model_dump(mode="json") for term in terms],
    }


def _model_or_default(value, default: dict[str, Any], model_cls):
    if value is None:
        return default
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return model_cls.model_validate(value).model_dump(mode="json")


def _optional_model(value, model_cls):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return model_cls.model_validate(value).model_dump(mode="json")


def _proposal_text(value) -> str | None:
    return value.value if value is not None else None


def _candidate_supplier_product_key(candidate: MasteringCandidateV1) -> str:
    resolution = candidate.supplier_product_resolution
    supplier_id = resolution.supplier_id
    identity = resolution.supplier_product_id or resolution.supplier_sku or resolution.barcode
    if not supplier_id:
        raise AmbiguousSupplierOffer("Supplier Product resolution requires supplier_id")
    if not identity:
        raise AmbiguousSupplierOffer("Supplier Product resolution requires supplier_product_id, supplier_sku, or barcode")
    if str(identity).startswith("supplier:"):
        return str(identity)
    return f"supplier:{supplier_id}:offer:{identity}"


def _serving_contract_from_state(
    db: Session,
    *,
    serving_item_id: UUID,
    candidate: MasteringCandidateV1,
    supplier_product: models.CatalogueSupplierProduct,
    price: models.CatalogueSupplierPrice,
    packaging_row: models.CataloguePackagingConfiguration,
    publication_version: str,
    published_at: datetime,
) -> ServingItemV1:
    product_resolution = candidate.product_variant_resolution
    canonical_sku = product_resolution.canonical_sku or product_resolution.product_variant_id
    product_variant_name = product_resolution.product_variant_name or product_resolution.proposed_name
    if not canonical_sku or not product_variant_name:
        raise AmbiguousProductVariant("Serving publication requires canonical_sku and product_variant_name")
    supplier = db.get(models.Supplier, supplier_product.supplier_id)
    if supplier is None:
        raise UpstreamRecordNotFound(f"Supplier {supplier_product.supplier_id} does not exist")
    packaging = _packaging_from_row(packaging_row)
    current_cost = Cost(
        amount=price.amount,
        currency=price.currency,
        price_basis=UnitOfMeasure(code=price.price_basis_uom_code, label=price.price_basis_uom_label),
    )
    cost_per_sellable = _cost_per_sellable_unit(current_cost, packaging)
    if packaging.sellable_units_per_purchase_unit is not None and cost_per_sellable is None:
        raise PublicationIneligible("Cost per sellable unit is not derivable from approved cost and packaging basis")
    lineage = PublicationLineage(
        catalogue_item_id=candidate.catalogue_item_id,
        raw_observation_ids=candidate.raw_observation_ids,
        review_decision_id=candidate.review_decision_id,
        mastering_candidate_id=candidate.mastering_candidate_id,
        publication_version=publication_version,
    )
    return ServingItemV1.model_validate(
        {
            "contract_version": "catalogue.serving_item.v1",
            "serving_item_id": str(serving_item_id),
            "canonical_sku": canonical_sku,
            "product_variant_id": product_resolution.product_variant_id or canonical_sku,
            "product_variant_name": product_variant_name,
            "supplier_offering": {
                "supplier_id": supplier_product.supplier_id,
                "supplier_name": supplier.name,
                "supplier_product_id": supplier_product.supplier_product_key,
                "supplier_sku": supplier_product.supplier_sku,
                "barcode": supplier_product.barcode,
            },
            "purchasing_packaging": packaging.model_dump(mode="json"),
            "current_approved_cost": current_cost.model_dump(mode="json"),
            "cost_per_sellable_unit": cost_per_sellable.model_dump(mode="json") if cost_per_sellable else None,
            "review_status": candidate.review_status.value,
            "published_at": _iso(published_at),
            "lineage": lineage.model_dump(mode="json"),
            "product_family_id": product_resolution.product_family_id,
            "brand": candidate.brand_resolution.value if candidate.brand_resolution else None,
            "categories": [candidate.category_resolution.value] if candidate.category_resolution and candidate.category_resolution.value else [],
            "active_mbb_terms": [term.model_dump(mode="json") for term in candidate.mbb_resolution.terms],
        }
    )


def _packaging_from_row(row: models.CataloguePackagingConfiguration) -> PackagingConfiguration:
    return PackagingConfiguration.model_validate(
        {
            "purchase_uom": _uom_from_row(row.purchase_uom_code, row.purchase_uom_label),
            "price_basis": _uom_from_row(row.price_basis_uom_code, row.price_basis_uom_label),
            "sellable_unit_uom": _uom_from_row(row.sellable_unit_uom_code, row.sellable_unit_uom_label),
            "sellable_units_per_purchase_unit": _decimal_json(row.sellable_units_per_purchase_unit),
            "content_amount": _decimal_json(row.content_amount),
            "content_uom": _uom_from_row(row.content_uom_code, row.content_uom_label),
            "order_increment": _quantity_from_row(row.order_increment_amount, row.order_increment_uom_code, row.order_increment_uom_label),
            "minimum_order_quantity": _quantity_from_row(row.minimum_order_amount, row.minimum_order_uom_code, row.minimum_order_uom_label),
            "break_pack_allowed": None if row.break_pack_allowed is None else bool(row.break_pack_allowed),
            "source_text": row.source_text,
        }
    )


def _cost_per_sellable_unit(cost: Cost, packaging: PackagingConfiguration) -> Money | None:
    price_basis = cost.price_basis.code
    sellable = packaging.sellable_unit_uom.code if packaging.sellable_unit_uom else None
    purchase = packaging.purchase_uom.code if packaging.purchase_uom else None
    if price_basis is not None and sellable is not None and price_basis == sellable:
        return Money(amount=cost.amount, currency=cost.currency)
    if (
        packaging.sellable_units_per_purchase_unit is not None
        and price_basis is not None
        and purchase is not None
        and price_basis == purchase
    ):
        return Money(amount=cost.amount / packaging.sellable_units_per_purchase_unit, currency=cost.currency)
    return None


def _mbb_row(term: MbbTerm, candidate: MasteringCandidateV1, supplier_product_id: int, applied_at: datetime, db: Session):
    condition = term.condition
    benefit = term.benefit
    kwargs: dict[str, Any] = {
        "supplier_product_id": supplier_product_id,
        "contract_mbb_term_uuid": str(term.mbb_term_id),
        "scope": term.scope.value,
        "condition_type": condition.condition_type,
        "benefit_type": benefit.benefit_type,
        "description": term.description,
        "effective_from": term.effective_from.isoformat() if term.effective_from else None,
        "effective_to": term.effective_to.isoformat() if term.effective_to else None,
        "source_document_id": _source_document_id(db, candidate.trace.supplier_catalogue_id),
        "ingestion_run_uuid": str(candidate.trace.ingestion_run_id),
        "mastering_candidate_uuid": str(candidate.mastering_candidate_id),
        "review_decision_uuid": str(candidate.review_decision_id) if candidate.review_decision_id else None,
        "is_active": 1,
        "created_at": _iso(applied_at),
    }
    if condition.condition_type == "minimum_quantity":
        kwargs.update(
            {
                "condition_quantity_amount": condition.quantity.amount,
                "condition_quantity_uom_code": condition.quantity.uom.code.value,
                "condition_quantity_uom_label": condition.quantity.uom.label,
            }
        )
    else:
        kwargs.update({"condition_spend_amount": condition.spend.amount, "condition_spend_currency": condition.spend.currency})

    if benefit.benefit_type == "discounted_unit_price":
        kwargs.update(
            {
                "discounted_price_amount": benefit.discounted_price.amount,
                "discounted_price_currency": benefit.discounted_price.currency,
                "discounted_price_basis_uom_code": benefit.discounted_price.price_basis.code.value,
                "discounted_price_basis_uom_label": benefit.discounted_price.price_basis.label,
            }
        )
    elif benefit.benefit_type == "percentage_discount":
        kwargs["percentage_discount"] = benefit.percentage
    elif benefit.benefit_type == "fixed_discount":
        kwargs.update(
            {
                "fixed_discount_amount": benefit.amount.amount,
                "fixed_discount_currency": benefit.amount.currency,
                "fixed_discount_reduction_basis": benefit.reduction_basis.value,
            }
        )
    else:
        kwargs.update(
            {
                "free_quantity_amount": benefit.quantity.amount,
                "free_quantity_uom_code": benefit.quantity.uom.code.value,
                "free_quantity_uom_label": benefit.quantity.uom.label,
            }
        )
    return models.CatalogueSupplierMbbTerm(**kwargs)


def _extracted_evidence_row(db: Session, raw_id: UUID) -> models.CatalogueExtractedEvidence:
    row = persistence._extracted_evidence(db, raw_id)  # noqa: SLF001
    if row is None:
        raise UpstreamRecordNotFound(f"extracted evidence observation {raw_id} does not exist")
    return row


def _normalized_row_row(db: Session, catalogue_item_id: UUID) -> models.CatalogueNormalizedRow:
    row = persistence._normalized_row(db, catalogue_item_id)  # noqa: SLF001
    if row is None:
        raise UpstreamRecordNotFound(f"interpreted claim {catalogue_item_id} does not exist")
    return row


def _validation_issue_row(db: Session, issue_id: UUID) -> models.CatalogueValidationIssue:
    row = persistence._validation_issue(db, issue_id)  # noqa: SLF001
    if row is None:
        raise UpstreamRecordNotFound(f"Validation Issue {issue_id} does not exist")
    return row


def _candidate_row(db: Session, candidate_id: UUID) -> models.CatalogueMasteringCandidate:
    row = persistence._mastering_candidate(db, candidate_id)  # noqa: SLF001
    if row is None:
        raise UpstreamRecordNotFound(f"Mastering Candidate {candidate_id} does not exist")
    return row


def _assert_same_raw_context(observations: list[models.CatalogueExtractedEvidence]) -> None:
    first = observations[0]
    for row in observations:
        if row.ingestion_run_uuid != first.ingestion_run_uuid:
            raise MissingOrIncompatibleLineage("Staging cannot group extracted evidence observations from different ingestion runs")
        if row.supplier_catalogue_uuid != first.supplier_catalogue_uuid:
            raise MissingOrIncompatibleLineage("Staging cannot group extracted evidence observations from different source documents")
        if row.source_file_uuid != first.source_file_uuid:
            raise MissingOrIncompatibleLineage("Staging cannot group extracted evidence observations from different source files")
        if (row.extraction_profile_id, row.extraction_profile_version) != (
            first.extraction_profile_id,
            first.extraction_profile_version,
        ):
            raise SupplierContractMismatch("Staging cannot group extracted evidence observations from different supplier-source contracts")


def _raise_if_open_blocking(db: Session, *, catalogue_item_uuid: str) -> None:
    issue = (
        db.query(models.CatalogueValidationIssue)
        .filter_by(
            catalogue_item_uuid=catalogue_item_uuid,
            severity=IssueSeverity.BLOCKING.value,
            resolution_status=IssueResolutionStatus.OPEN.value,
        )
        .first()
    )
    if issue is not None:
        raise BlockingValidationIssues(f"Open blocking validation issue prevents transition: {issue.issue_code}")


def _source_document_id(db: Session, supplier_catalogue_id: UUID) -> int | None:
    source = db.query(models.CatalogueSourceDocument).filter_by(supplier_catalogue_uuid=str(supplier_catalogue_id)).first()
    return source.id if source else None


def _supplier_id_from_source(db: Session, supplier_catalogue_id: UUID) -> int | None:
    source = db.query(models.CatalogueSourceDocument).filter_by(supplier_catalogue_uuid=str(supplier_catalogue_id)).first()
    return source.supplier_id if source else None


def _product_id_for_sku(db: Session, canonical_sku: str | None) -> int | None:
    if not canonical_sku:
        return None
    product = db.query(models.Product).filter_by(sku_code=canonical_sku).first()
    return product.id if product else None


def _uom_code(value: UnitOfMeasure | None) -> str | None:
    return value.code.value if value and value.code else None


def _uom_label(value: UnitOfMeasure | None) -> str | None:
    return value.label if value else None


def _uom_from_row(code: str | None, label: str | None) -> dict[str, str | None] | None:
    if code is None and label is None:
        return None
    return {"code": code, "label": label}


def _quantity_from_row(amount, code: str | None, label: str | None):
    if amount is None:
        return None
    return {"amount": _decimal_json(amount), "uom": _uom_from_row(code, label)}


def _publication_key(contract: ServingItemV1) -> str:
    return f"sku:{contract.canonical_sku}:supplier:{contract.supplier_offering.supplier_id}:{contract.supplier_offering.supplier_product_id}"


# Attempt metadata describes one extraction ATTEMPT, never the observed
# evidence itself. Explicit allowlist (Option B of the replay-safety design):
# identical evidence must reuse the immutable first observation when only
# these per-call operational keys differ. Everything else in source_metadata
# stays material. Extraction-attempt history is tracked as focused debt in
# docs/technical-debt/extraction-attempt-history.md.
_VOLATILE_ATTEMPT_METADATA_KEYS = (
    "provider_request_id",
    "provider_trace_id",
    "attempt_number",
    "request_timestamp",
    "latency_ms",
    "extraction_warnings",
)


def _raw_material(contract: ExtractedEvidenceV1) -> dict[str, Any]:
    """Material equality for extracted-evidence replay.

    Excluded from equality (replay-safe): ``captured_at``, the volatile
    attempt-metadata keys above, and ``extraction_confidence`` (a replay with
    a different confidence reuses the immutable first observation — it is
    never overwritten). Material (conflict or distinct identity on change):
    raw text, raw cells, source location + bounding box, extraction method,
    provider/model identity, and all stable source metadata.
    """

    payload = contract.model_dump(mode="json")
    payload.pop("captured_at", None)
    payload.pop("extraction_confidence", None)
    metadata = payload.get("source_metadata")
    if isinstance(metadata, dict):
        payload["source_metadata"] = {
            key: value for key, value in metadata.items() if key not in _VOLATILE_ATTEMPT_METADATA_KEYS
        }
    return payload


def _claim_material(contract: NormalizedRowV1) -> dict[str, Any]:
    """Material equality for interpreted-claim (step 6) replay.

    Mirrors the step-4 policy: per-attempt volatile values — ``created_at``
    and the model confidence recorded inside per-field evidence — are
    excluded, so re-interpreting identical evidence with only confidence
    drift reuses the immutable first claim. Proposal VALUES, evidence links,
    raw fields, lineage and interpreter identity remain material: genuine
    interpretation drift under the same claim identity stays a controlled
    IdempotencyConflict rather than silent mutation.
    """

    payload = contract.model_dump(mode="json")
    payload.pop("created_at", None)
    proposed = payload.get("normalized_fields")
    if isinstance(proposed, dict):
        payload["normalized_fields"] = _scrub_evidence_confidence(proposed)
    return payload


def _scrub_evidence_confidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_evidence_confidence(item)
            for key, item in value.items()
            if key != "confidence"
        }
    if isinstance(value, list):
        return [_scrub_evidence_confidence(item) for item in value]
    return value


def _issue_material(contract: ValidationIssueV1) -> dict[str, Any]:
    payload = contract.model_dump(mode="json")
    payload.pop("created_at", None)
    for key in (
        "resolution_status",
        "resolver_id",
        "resolved_at",
        "resolution_note",
        "publish_blocking",
    ):
        payload.pop(key, None)
    return payload


def _candidate_material(contract: MasteringCandidateV1, *, include_review: bool = True) -> dict[str, Any]:
    payload = contract.model_dump(mode="json")
    payload.pop("created_at", None)
    if not include_review:
        for key in ("review_status", "reviewed_by", "reviewed_at", "override_reason", "review_decision_id"):
            payload.pop(key, None)
    return payload


def _serving_material(contract: ServingItemV1) -> dict[str, Any]:
    payload = contract.model_dump(mode="json")
    payload.pop("published_at", None)
    return payload


def _assert_same_material(existing: dict[str, Any], incoming: dict[str, Any], label: str) -> None:
    if existing != incoming:
        raise IdempotencyConflict(f"{label} already exists with different material input")


def _stable_uuid(namespace: str, material: dict[str, Any]) -> UUID:
    return uuid5(NAMESPACE_URL, f"rosetta:{namespace}:{_json_dumps(material)}")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decimal_json(value) -> str | None:
    return str(value) if value is not None else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)
