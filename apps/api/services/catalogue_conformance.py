"""Deterministic conformance of persisted catalogue evidence (STAGING layer).

This module is the only place where verbatim source evidence becomes
contract-conformed normalized rows. It consumes Staging-persisted extracted
evidence observations (``raw_cells`` with column names) together with the
resolved supplier-source contract, and maps each named cell through the
contract's declared source columns to typed normalized fields. Every field
points back at the supporting extracted evidence observation.

Boundaries this module enforces:

- Deterministic only. The supplier contract maps source columns to business
  fields; nothing here calls a model, guesses, invents, or normalizes values
  the contract did not declare. No re-reading of the source file — only
  persisted evidence.
- Header rows (cells that repeat the contract's declared source columns) are
  evidence, not rows, and are skipped from Staging.
- Observations that carry no structured cells cannot be conformed
  deterministically; they are staged for manual review rather than dropped.
  (With cell-producing extraction — spreadsheets/CSV and vision — this is an
  edge case, e.g. a plain-text source with no columns.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from schemas.catalogue_pipeline.enums import UnitCode
from schemas.catalogue_pipeline.supplier_contracts.common import SourceFieldRequirement
from services.catalogue_evidence_extraction import ExtractedEvidence


@dataclass(frozen=True)
class ContractExecutionIssue:
    """Machine-readable contract condition that must not be silently ignored."""

    issue_code: str
    message: str
    severity: str = "WARNING"
    field_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "issue_code": self.issue_code,
            "severity": self.severity,
            "message": self.message,
            "field_key": self.field_key,
        }


@dataclass(frozen=True)
class ConformedRow:
    """One normalized row deterministically conformed from one evidence observation.

    ``provenance`` records HOW the row was produced ("contract_cells" for a
    deterministic contract-cell mapping, "unconformable" when the observation
    carried no structured cells to map). It is persisted onto the row's
    metadata.
    """

    observation_key: str
    raw_observation_id: UUID
    raw_fields: dict[str, Any]
    normalized_fields: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    issues: tuple[ContractExecutionIssue, ...] = ()


@dataclass(frozen=True)
class ConformanceOutcome:
    """Conformance results plus durable accounting.

    ``metadata`` carries machine-readable accounting (conformed / skipped
    header / unconformable counts) so the run record can distinguish "skipped
    as header row" from "could not be conformed".
    """

    items: tuple[ConformedRow, ...]
    warnings: tuple[str, ...] = ()
    skipped_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    issues: tuple[ContractExecutionIssue, ...] = ()


def conform_observations(
    observations: tuple[ExtractedEvidence, ...],
    raw_observation_ids: tuple[UUID, ...],
    runtime_contract,
) -> ConformanceOutcome:
    """Conform persisted observations into normalized rows using the contract."""

    if len(observations) != len(raw_observation_ids):
        raise ValueError("observations and evidence ids must align")

    document_issues = _document_issues(observations, runtime_contract)
    warnings: list[str] = [issue.message for issue in document_issues]
    items: list[ConformedRow] = []
    skipped = 0
    unconformable = 0
    for observation, raw_id in zip(observations, raw_observation_ids, strict=True):
        key = observation.observation_key
        if _has_cells(observation):
            fields = _fields_from_cells(observation, runtime_contract)
            if fields is None:
                # Header row — evidence, not a row.
                skipped += 1
                continue
            row_issues = (
                document_issues
                + _required_field_issues(fields, runtime_contract)
                + _validation_rule_issues(fields, runtime_contract)
                + _normalization_issues(fields, runtime_contract)
            )
            items.append(
                _item_from_fields(
                    observation,
                    raw_id,
                    fields,
                    runtime_contract,
                    provenance=_provenance("contract_cells"),
                    warnings=tuple(issue.message for issue in row_issues),
                    issues=row_issues,
                )
            )
            warnings.extend(f"{key}: {issue.message}" for issue in row_issues)
            continue

        # No structured cells to map through the contract. Never invent fields;
        # stage the row (with empty normalized fields) for manual review.
        unconformable += 1
        message = "no structured cells to conform; staged for manual review"
        warnings.append(f"{key}: {message}")
        items.append(
            _item_from_fields(
                observation,
                raw_id,
                {},
                runtime_contract,
                provenance=_provenance("unconformable"),
                warnings=(message,),
                issues=(
                    ContractExecutionIssue(
                        issue_code="CONTRACT_ROW_UNCONFORMABLE",
                        severity="BLOCKING",
                        message=message,
                    ),
                ),
            )
        )

    return ConformanceOutcome(
        items=tuple(items),
        warnings=tuple(warnings),
        skipped_count=skipped,
        metadata={
            "conformed_items": len(items),
            "skipped_header_rows": skipped,
            "unconformable_items": unconformable,
            "contract_issue_count": len(document_issues) + sum(len(item.issues) for item in items),
            "document_issues": [issue.as_dict() for issue in document_issues],
            "declared_row_eligibility_rules": list(runtime_contract.declaration.source_structure.row_eligibility_rules),
            "declared_skip_rules": list(runtime_contract.declaration.source_structure.skip_rules),
            "degraded": unconformable > 0 or bool(document_issues) or any(item.issues for item in items),
        },
        issues=document_issues,
    )


def _provenance(interpreter: str) -> dict[str, Any]:
    return {"interpreter": interpreter}


def _fields_from_cells(observation: ExtractedEvidence, runtime_contract) -> dict[str, Any] | None:
    """Deterministically map named cells through the contract's source columns.

    Handles direct source columns, ``composed_from`` multi-column joins, and
    contract constants — the full deterministic mapping the supplier contract
    declares. Returns None when the observation is a header row (its cell values
    repeat the contract's declared source columns) — headers are evidence, not
    rows.
    """

    # Index this row's cells by every match-key of their column heading.
    cell_by_key: dict[str, str] = {}
    row_values: list[str] = []
    for cell in observation.raw_cells:
        if cell.column_name and cell.raw_value is not None and str(cell.raw_value).strip():
            row_values.append(str(cell.raw_value))
            for key in _column_keys(cell.column_name):
                cell_by_key.setdefault(key, str(cell.raw_value))
    if not cell_by_key:
        return {}

    def _lookup(*column_names: str) -> str | None:
        for column_name in column_names:
            for key in _column_keys(column_name):
                if key in cell_by_key:
                    return cell_by_key[key]
        return None

    def _lookup_field(contract_field) -> str | None:
        names = tuple(
            filter(
                None,
                (
                    contract_field.source_column,
                    contract_field.source_path,
                    *contract_field.aliases,
                ),
            )
        )
        return _lookup(*names)

    def _lookup_composed(column_name: str) -> str | None:
        aliases: list[str] = []
        for candidate in runtime_contract.declaration.fields:
            if candidate.source_column == column_name or candidate.source_path == column_name:
                aliases.extend(candidate.aliases)
        for key in _column_keys(column_name):
            if key in cell_by_key:
                return cell_by_key[key]
        return _lookup(*aliases)

    # Header row: its cell VALUES repeat the contract's declared source columns.
    source_keys: set[str] = set()
    for contract_field in runtime_contract.declaration.fields:
        for source_name in filter(
            None, (contract_field.source_column, contract_field.source_path, *(contract_field.composed_from or ()))
        ):
            source_keys.update(_column_keys(source_name))
        for alias in contract_field.aliases:
            source_keys.update(_column_keys(alias))
    header_hits = sum(1 for value in row_values if any(key in source_keys for key in _column_keys(value)))
    if header_hits >= max(2, len(row_values) - 1):
        return None

    fields: dict[str, Any] = {}
    for contract_field in runtime_contract.declaration.fields:
        target = _role_target(contract_field.role)
        if target is None:
            target = f"additional:{contract_field.field_key}"
        value: Any = _lookup_field(contract_field)
        if value is None and contract_field.composed_from:
            parts = [part for part in (_lookup_composed(column) for column in contract_field.composed_from) if part]
            if parts:
                value = " ".join(parts)
        if value is None and contract_field.constant_value is not None:
            value = contract_field.constant_value
        if value is not None:
            # Preserve every declaration by its stable contract field key even
            # when multiple fields share one semantic role (for example pack
            # size and units per case are both PACKAGING).
            fields[f"source:{contract_field.field_key}"] = value
            fields.setdefault(target, value)
    if observation.confidence is not None:
        fields.setdefault("confidence", str(observation.confidence))
    return fields


def _item_from_fields(
    observation: ExtractedEvidence,
    raw_observation_id: UUID,
    fields: dict[str, Any],
    runtime_contract,
    *,
    provenance: dict[str, Any] | None = None,
    warnings: tuple[str, ...] = (),
    issues: tuple[ContractExecutionIssue, ...] = (),
) -> ConformedRow:
    additional_fields = {
        key.removeprefix("source:"): value
        for key, value in fields.items()
        if key.startswith("source:")
    }
    raw_fields = {
        "supplier_sku": _text(fields.get("supplier_sku")),
        "product_name": _text(fields.get("description")),
        "original_product_name": _text(fields.get("original_description")),
        "brand": _text(fields.get("brand")),
        "category": _text(fields.get("category")),
        "cost": _raw_money_text(fields.get("cost_price")),
        "rrp": _raw_money_text(fields.get("rrp")),
        "packaging": _text(fields.get("pack_size") or fields.get("uom")),
        "mbb_text": _text(fields.get("bulk_buy_tiers")),
        "barcode": _text(fields.get("barcode")),
        "variant": _text(fields.get("variant")),
        "species": _text(fields.get("species")),
        "segment": _text(fields.get("segment")),
        "effective_date": _text(fields.get("effective_date")),
        "content_measure": _text(fields.get("content_measure")),
        "row_eligibility": _text(fields.get("row_eligibility")),
        "additional_fields": additional_fields,
        "source_row_label": observation.observation_key,
    }
    evidence = {
        "raw_observation_id": str(raw_observation_id),
        "field_path": "/raw_cells" if _has_cells(observation) else "/raw_text",
        "confidence": _confidence_text(fields.get("confidence"), observation.confidence),
    }
    normalized: dict[str, Any] = {"mbb_terms": []}
    # normalized product_name = the contract's composed join, English only (bilingual
    # source cells keep their Latin portion). raw_fields keeps the verbatim join.
    # No recomposition beyond the contract: no brand/size, no reordering, no dedup.
    product_name = _english_text(fields.get("description")) or _text(fields.get("description"))
    if product_name is not None:
        normalized["product_name"] = {"value": product_name, "evidence": evidence}
    for source_key, normalized_key in (
        ("supplier_sku", "supplier_sku"),
        ("brand", "brand"),
        ("category", "category"),
        ("barcode", "barcode"),
        ("variant", "variant"),
        ("species", "species"),
        ("segment", "segment"),
    ):
        value = _text(fields.get(source_key))
        if value is not None:
            normalized[normalized_key] = {"value": value, "evidence": evidence}
    effective_date = _date_proposal(fields.get("effective_date"), evidence)
    if effective_date is not None:
        normalized["effective_date"] = effective_date

    cost = _cost_proposal(fields.get("cost_price"), runtime_contract, evidence)
    if cost is not None:
        normalized["cost"] = cost
    rrp = _money_proposal(fields.get("rrp"), runtime_contract, evidence)
    if rrp is not None:
        normalized["rrp"] = rrp
    packaging = _packaging_proposal(fields, runtime_contract, evidence)
    if packaging is not None:
        normalized["packaging"] = packaging

    return ConformedRow(
        observation_key=observation.observation_key,
        raw_observation_id=raw_observation_id,
        raw_fields=raw_fields,
        normalized_fields=normalized,
        provenance=dict(provenance or {}),
        warnings=warnings,
        issues=issues,
    )


def _money_proposal(value: Any, runtime_contract, evidence: dict[str, Any]) -> dict[str, Any] | None:
    amount = _decimal_or_none(value)
    if amount is None:
        return None
    return {
        "amount": str(amount),
        "currency": runtime_contract.declaration.pricing.currency,
        "evidence": evidence,
    }


def _date_proposal(value: Any, evidence: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _date_or_none(value)
    if parsed is None:
        return None
    return {"value": parsed.isoformat(), "evidence": evidence}


def _normalization_issues(
    fields: dict[str, Any],
    runtime_contract,
) -> tuple[ContractExecutionIssue, ...]:
    declaration = runtime_contract.declaration
    issues: list[ContractExecutionIssue] = []
    raw_cost = fields.get("cost_price")
    if _matches_null_marker(raw_cost, declaration.pricing.null_cost_markers):
        issues.append(
            ContractExecutionIssue(
                issue_code="CONTRACT_NULL_COST_REQUIRES_REVIEW",
                severity="WARNING",
                field_key=declaration.pricing.cost_source_field,
                message="The supplier marked the cost as unavailable; no numeric cost was proposed.",
            )
        )
    elif _decimal_or_none(raw_cost) is not None and declaration.pricing.price_basis is None:
        issues.append(
            ContractExecutionIssue(
                issue_code="CONTRACT_PRICE_BASIS_UNRESOLVED",
                severity="BLOCKING",
                field_key=declaration.pricing.cost_source_field,
                message="A supplier price was observed, but its price basis is unresolved.",
            )
        )
    elif _text(raw_cost) is not None and (
        _decimal_value(raw_cost) is None or _decimal_value(raw_cost) < 0
    ):
        issues.append(
            ContractExecutionIssue(
                issue_code="CONTRACT_COST_UNPARSEABLE",
                severity="BLOCKING",
                field_key=declaration.pricing.cost_source_field,
                message=f"Supplier cost '{raw_cost}' could not be normalized as a monetary amount.",
            )
        )

    raw_date = _text(fields.get("effective_date"))
    if raw_date and _date_or_none(raw_date) is None:
        issues.append(
            ContractExecutionIssue(
                issue_code="CONTRACT_EFFECTIVE_DATE_UNPARSEABLE",
                severity="WARNING",
                field_key="effective_date",
                message=f"Effective date '{raw_date}' could not be normalized without guessing.",
            )
        )

    raw_mbb = _text(fields.get("bulk_buy_tiers"))
    if raw_mbb:
        guidance = declaration.mbb.requires_validation_issue_when
        issues.append(
            ContractExecutionIssue(
                issue_code="CONTRACT_MBB_REQUIRES_REVIEW",
                severity="WARNING",
                field_key="mbb_text",
                message=(
                    "Promotion or MBB text was preserved but not normalized because its scope, "
                    "condition, or benefit is not fully proven."
                    + (f" Review condition: {' '.join(guidance)}" if guidance else "")
                ),
            )
        )
    return tuple(issues)


def _validation_rule_issues(
    fields: dict[str, Any],
    runtime_contract,
) -> tuple[ContractExecutionIssue, ...]:
    """Execute the small, declared v1 rule vocabulary without evaluating code."""

    issues: list[ContractExecutionIssue] = []
    for rule in runtime_contract.declaration.validation_rules:
        failed = False
        cost = _decimal_value(fields.get("cost_price"))
        rrp = _decimal_value(fields.get("rrp"))
        increment = _decimal_value(fields.get("order_increment_qty"))
        if rule.source_expression == "cost_price < rrp":
            failed = cost is not None and rrp is not None and cost >= rrp
        elif rule.source_expression == "cost_price > 0":
            failed = cost is not None and cost <= 0
        elif rule.source_expression == "order_increment_qty >= 1":
            failed = increment is not None and increment < 1
        elif rule.source_expression:
            issues.append(
                ContractExecutionIssue(
                    issue_code="CONTRACT_VALIDATION_RULE_UNSUPPORTED",
                    severity="WARNING",
                    field_key=rule.rule_id,
                    message=f"Contract validation rule '{rule.rule_id}' has no deterministic executor.",
                )
            )
            continue
        if failed:
            issues.append(
                ContractExecutionIssue(
                    issue_code=rule.issue_code,
                    severity=rule.severity.value,
                    field_key=rule.rule_id,
                    message=rule.review_guidance,
                )
            )
    return tuple(issues)


def _required_field_issues(
    fields: dict[str, Any],
    runtime_contract,
) -> tuple[ContractExecutionIssue, ...]:
    issues: list[ContractExecutionIssue] = []
    for contract_field in runtime_contract.declaration.fields:
        if contract_field.requirement != SourceFieldRequirement.REQUIRED:
            continue
        if _text(fields.get(f"source:{contract_field.field_key}")) is None:
            issues.append(
                ContractExecutionIssue(
                    issue_code="CONTRACT_REQUIRED_FIELD_MISSING",
                    severity="BLOCKING",
                    field_key=contract_field.field_key,
                    message=f"Required contract field '{contract_field.field_key}' is missing from the source row.",
                )
            )
    return tuple(issues)


def _document_issues(
    observations: tuple[ExtractedEvidence, ...],
    runtime_contract,
) -> tuple[ContractExecutionIssue, ...]:
    structure = runtime_contract.declaration.source_structure
    observed_columns = {
        key
        for observation in observations
        for cell in observation.raw_cells
        if cell.column_name
        for key in _column_keys(cell.column_name)
    }
    issues: list[ContractExecutionIssue] = []
    for required_header in structure.required_headers:
        accepted_names = [required_header]
        for contract_field in runtime_contract.declaration.fields:
            declared_names = [contract_field.source_column, contract_field.source_path, *contract_field.composed_from]
            if any(declared and _names_overlap(declared, required_header) for declared in declared_names):
                accepted_names.extend(contract_field.aliases)
        if not any(key in observed_columns for name in accepted_names for key in _column_keys(name)):
            issues.append(
                ContractExecutionIssue(
                    issue_code="CONTRACT_REQUIRED_HEADER_MISSING",
                    severity="BLOCKING",
                    field_key=required_header,
                    message=f"Required source header '{required_header}' was not observed.",
                )
            )

    observed_sheets = {
        observation.source_location.sheet_name
        for observation in observations
        if observation.source_location.sheet_name
    }
    if structure.expected_sheet_names and observed_sheets:
        expected_keys = {key for name in structure.expected_sheet_names for key in _column_keys(name)}
        for sheet in sorted(observed_sheets):
            if not any(key in expected_keys for key in _column_keys(sheet)):
                issues.append(
                    ContractExecutionIssue(
                        issue_code="CONTRACT_UNEXPECTED_SHEET",
                        severity="WARNING",
                        field_key="sheet_name",
                        message=f"Source sheet '{sheet}' is not declared by the supplier contract.",
                    )
                )
    return tuple(issues)


def _names_overlap(left: str, right: str) -> bool:
    """Match a required header to a more descriptive declared bilingual header."""

    for left_key in _column_keys(left):
        for right_key in _column_keys(right):
            if left_key == right_key or left_key in right_key or right_key in left_key:
                return True
    return False


def _cost_proposal(value: Any, runtime_contract, evidence: dict[str, Any]) -> dict[str, Any] | None:
    if _matches_null_marker(value, runtime_contract.declaration.pricing.null_cost_markers):
        return None
    amount = _decimal_or_none(value)
    pricing = runtime_contract.declaration.pricing
    basis = pricing.price_basis
    if amount is None or basis is None or basis.code is None:
        return None
    return {
        "amount": str(amount),
        # Currency is a CONTRACT declaration, never a conformance default.
        "currency": pricing.currency,
        "price_basis": basis.model_dump(mode="json"),
        "evidence": evidence,
    }


def _matches_null_marker(value: Any, markers: list[str]) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.strip().lower()
    return any(marker.strip().lower() in lowered for marker in markers if marker.strip())


def _packaging_proposal(fields: dict[str, Any], runtime_contract, evidence: dict[str, Any]) -> dict[str, Any] | None:
    semantics = runtime_contract.declaration.packaging
    source_text = _source_field_value(fields, semantics.packaging_source_field) or _text(
        fields.get("pack_size") or fields.get("uom")
    )
    if not source_text and not any(
        (
            semantics.purchase_uom,
            semantics.price_basis,
            semantics.sellable_unit_uom,
            semantics.break_pack_allowed is not None,
        )
    ):
        return None
    proposal: dict[str, Any] = {"source_text": source_text, "evidence": evidence}
    for attribute in ("purchase_uom", "price_basis", "sellable_unit_uom"):
        uom = getattr(semantics, attribute)
        if uom is not None:
            proposal[attribute] = uom.model_dump(mode="json")
    if semantics.break_pack_allowed is not None:
        proposal["break_pack_allowed"] = semantics.break_pack_allowed
    content_source = _source_field_value(fields, semantics.content_measure_source_field)
    content = _content_measure(content_source) if semantics.content_measure_source_field else None
    if content:
        amount, uom = content
        proposal["content_amount"] = str(amount)
        proposal["content_uom"] = (
            semantics.content_measure_uom.model_dump(mode="json")
            if semantics.content_measure_uom is not None
            else {"code": uom}
        )
    sellable_count = _leading_decimal(
        _source_field_value(fields, semantics.sellable_units_per_purchase_unit_source_field)
    )
    if sellable_count is not None:
        proposal["sellable_units_per_purchase_unit"] = str(sellable_count)
    quantity_uom = semantics.sellable_unit_uom or semantics.price_basis
    order_increment = _leading_decimal(_source_field_value(fields, semantics.order_increment_source_field))
    if order_increment is not None and quantity_uom is not None and quantity_uom.code is not None:
        proposal["order_increment"] = {
            "amount": str(order_increment),
            "uom": quantity_uom.model_dump(mode="json"),
        }
    minimum_order = _leading_decimal(_source_field_value(fields, semantics.minimum_order_source_field))
    if minimum_order is not None and quantity_uom is not None and quantity_uom.code is not None:
        proposal["minimum_order_quantity"] = {
            "amount": str(minimum_order),
            "uom": quantity_uom.model_dump(mode="json"),
        }
    return proposal


def _content_measure(text: str | None) -> tuple[Decimal, str] | None:
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(ml|g|kg|l|oz|lb|lbs)\b", text, re.IGNORECASE)
    if not match:
        return None
    amount = Decimal(match.group(1))
    raw_uom = match.group(2).upper()
    uom = {
        "ML": UnitCode.ML.value,
        "G": UnitCode.G.value,
        "KG": UnitCode.KG.value,
        "L": UnitCode.L.value,
        "OZ": UnitCode.OZ.value,
        "LB": UnitCode.LB.value,
        "LBS": UnitCode.LB.value,
    }[raw_uom]
    return amount, uom


def _source_field_value(fields: dict[str, Any], field_key: str | None) -> str | None:
    if not field_key:
        return None
    return _text(fields.get(f"source:{field_key}"))


def _leading_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    parsed = Decimal(match.group(1))
    return parsed if parsed > 0 else None


def _date_or_none(value: Any) -> date | None:
    text = _text(value)
    if text is None:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _confidence_text(field_value: Any, observation_confidence: Decimal | None) -> str | None:
    for candidate in (field_value, observation_confidence):
        if candidate is None or candidate == "":
            continue
        try:
            confidence = Decimal(str(candidate))
        except (InvalidOperation, ValueError):
            continue
        if Decimal("0") <= confidence <= Decimal("1"):
            return str(confidence)
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    decimal = _decimal_value(value)
    return decimal if decimal is not None and decimal >= 0 else None


def _decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() in {"by quote", "quote", "n/a", "na"}:
        return None
    try:
        decimal = Decimal(str(value).replace(",", "").replace("$", "").replace("HKD", "").replace("HK$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return decimal


def _raw_money_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fold(value: str) -> str:
    # Separator-insensitive: treat "/", "|" and whitespace runs as one space so a
    # contract column "Product Range / 產品系列" folds the same as an OCR rendering
    # "Product Range 產品系列".
    return re.sub(r"[\s/|]+", " ", value).strip().lower()


def _english_fold(value: str) -> str:
    # The Latin/ASCII portion only. Bilingual headers keep a stable English name
    # while their CJK rendering varies across OCR passes (the vision provider vs
    # the contract text), so the English part is the reliable match key.
    ascii_only = re.sub(r"[^\x00-\x7f]+", " ", value)
    ascii_only = re.sub(r"[/|*()]+", " ", ascii_only)
    return re.sub(r"\s+", " ", ascii_only).strip().lower()


def _column_keys(value: str) -> list[str]:
    keys = [_fold(value)]
    english = _english_fold(value)
    if english and english not in keys:
        keys.append(english)
    return keys


def _english_text(value: Any) -> str | None:
    """The Latin/ASCII portion of a (possibly bilingual) value.

    Bilingual source cells render as "中文 English"; keep only the English and
    tidy whitespace. Returns None when there is no Latin text.
    """
    if value is None:
        return None
    ascii_only = re.sub(r"[^\x00-\x7f]+", " ", str(value))
    return re.sub(r"\s+", " ", ascii_only).strip(" -|/*·,") or None


def _has_cells(observation: ExtractedEvidence) -> bool:
    return any(cell.raw_value is not None and str(cell.raw_value).strip() for cell in observation.raw_cells)


def _role_target(role) -> str | None:
    return _ROLE_TARGETS.get(getattr(role, "value", role))


_ROLE_TARGETS = {
    "SUPPLIER_SKU": "supplier_sku",
    "PRODUCT_NAME": "description",
    "BRAND": "brand",
    "CATEGORY": "category",
    "SOURCE_PRICE": "cost_price",
    "RRP": "rrp",
    "PACKAGING": "pack_size",
    "BARCODE": "barcode",
    "VARIANT": "variant",
    "SPECIES": "species",
    "SEGMENT": "segment",
    "MBB_TEXT": "bulk_buy_tiers",
    "EFFECTIVE_DATE": "effective_date",
    "ORDER_INCREMENT": "order_increment_qty",
    "CONTENT_MEASURE": "content_measure",
    "ROW_ELIGIBILITY": "row_eligibility",
}


__all__ = [
    "ConformanceOutcome",
    "ConformedRow",
    "ContractExecutionIssue",
    "conform_observations",
]
