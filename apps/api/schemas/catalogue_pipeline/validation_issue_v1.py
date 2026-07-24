"""Validation Issue Contract v1."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from .base import ContractModel, register_contract
from .enums import IssueResolutionStatus, IssueSeverity, ValidationStage


CONTRACT_ID = "catalogue.validation_issue.v1"


class ValidationIssueV1(ContractModel):
    """Uncertainty, invalid data, contradiction, incompleteness, or HITL decision request."""

    contract_id = CONTRACT_ID

    contract_version: Literal["catalogue.validation_issue.v1"] = Field(
        ...,
        description="Exact CIS-103 Validation Issue contract identifier.",
    )
    validation_issue_id: UUID = Field(..., description="Validation Issue identity.")
    ingestion_run_id: UUID = Field(..., description="Pipeline run identity.")
    catalogue_item_id: UUID | None = Field(None, description="Related interpreted claim when applicable.")
    raw_observation_id: UUID | None = Field(None, description="Related extracted evidence observation when applicable.")
    stage: ValidationStage = Field(..., description="Pipeline stage that created the issue.")
    issue_code: str = Field(..., min_length=1, description="Stable machine-readable issue code.")
    severity: IssueSeverity = Field(..., description="Issue severity.")
    message: str = Field(..., min_length=1, description="Human-readable issue message.")
    created_at: datetime = Field(..., description="Timezone-aware issue creation timestamp.")
    resolution_status: IssueResolutionStatus = Field(..., description="Issue resolution status.")
    field_path: str | None = Field(None, description="JSON-style path to the field involved.")
    raw_value: Any = Field(None, description="Raw value involved in the issue.")
    proposed_value: Any = Field(None, description="Proposed value involved in the issue.")
    expected_value: Any = Field(None, description="Expected value or rule summary.")
    review_guidance: str | None = Field(None, description="Business-readable instruction for the reviewer.")
    resolver_id: str | None = Field(None, description="Identity that resolved the issue.")
    resolved_at: datetime | None = Field(None, description="Timezone-aware resolution timestamp.")
    resolution_note: str | None = Field(None, description="Auditable note explaining the resolution.")

    @computed_field
    @property
    def publish_blocking(self) -> bool:
        """True when the issue prevents publication by definition."""

        return self.severity == IssueSeverity.BLOCKING and self.resolution_status == IssueResolutionStatus.OPEN

    @model_validator(mode="after")
    def _resolution_is_auditable(self):
        if self.resolution_status == IssueResolutionStatus.OPEN:
            if self.resolver_id or self.resolved_at or self.resolution_note:
                raise ValueError("unresolved issue cannot contain resolver, resolved_at, or resolution_note")
        else:
            if self.resolved_at is None:
                raise ValueError("resolved issue requires resolved_at")
            if not (self.resolver_id or self.resolution_note):
                raise ValueError("resolved issue requires resolver_id or resolution_note")
        if self.review_guidance:
            lower = self.review_guidance.lower()
            forbidden = ("traceback", "valueerror", "exception", "stack trace")
            if any(token in lower for token in forbidden):
                raise ValueError("review_guidance must be business-readable, not an exception message")
        return self


register_contract(ValidationIssueV1)

