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
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from schemas.catalogue_pipeline.enums import UnitCode
from services.catalogue_evidence_extraction import ExtractedEvidence


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


def conform_observations(
    observations: tuple[ExtractedEvidence, ...],
    raw_observation_ids: tuple[UUID, ...],
    runtime_contract,
) -> ConformanceOutcome:
    """Conform persisted observations into normalized rows using the contract."""

    if len(observations) != len(raw_observation_ids):
        raise ValueError("observations and evidence ids must align")

    warnings: list[str] = []
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
            items.append(
                _item_from_fields(observation, raw_id, fields, runtime_contract, provenance=_provenance("contract_cells"))
            )
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
            "degraded": unconformable > 0,
        },
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

    # This row's values, keyed by its folded column heading.
    cell_by_column: dict[str, str] = {}
    for cell in observation.raw_cells:
        if cell.column_name and cell.raw_value is not None and str(cell.raw_value).strip():
            cell_by_column.setdefault(_fold(cell.column_name), str(cell.raw_value))
    if not cell_by_column:
        return {}

    direct_targets: dict[str, str] = {}
    composed: list[tuple[str, list[str]]] = []
    constants: list[tuple[str, Any]] = []
    source_columns: set[str] = set()
    for contract_field in runtime_contract.declaration.fields:
        target = _role_target(contract_field.role)
        if target is None:
            continue
        for source_name in filter(None, (contract_field.source_column, contract_field.source_path)):
            direct_targets[_fold(source_name)] = target
            source_columns.add(_fold(source_name))
        if contract_field.composed_from:
            folded = [_fold(column) for column in contract_field.composed_from]
            composed.append((target, folded))
            source_columns.update(folded)
        if contract_field.constant_value is not None:
            constants.append((target, contract_field.constant_value))

    # Header row: its cell VALUES repeat the contract's declared source columns.
    header_hits = sum(1 for value in cell_by_column.values() if _fold(value) in source_columns)
    if header_hits >= max(2, len(cell_by_column) - 1):
        return None

    fields: dict[str, Any] = {}
    for folded_column, value in cell_by_column.items():
        target = direct_targets.get(folded_column)
        if target and target not in fields:
            fields[target] = value
    for target, columns in composed:
        if target in fields:
            continue
        parts = [cell_by_column[column] for column in columns if column in cell_by_column]
        if parts:
            fields[target] = " ".join(parts)
    for target, constant_value in constants:
        fields.setdefault(target, constant_value)
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
) -> ConformedRow:
    raw_fields = {
        "supplier_sku": _text(fields.get("supplier_sku")),
        "product_name": _text(fields.get("description")),
        "original_product_name": _text(fields.get("original_description")),
        "brand": _text(fields.get("brand")),
        "category": _text(fields.get("category")),
        "cost": _raw_money_text(fields.get("cost_price")),
        "packaging": _text(fields.get("pack_size") or fields.get("uom")),
        "mbb_text": _text(fields.get("bulk_buy_tiers")),
        "barcode": _text(fields.get("barcode")),
        "variant": _text(fields.get("variant")),
        "source_row_label": observation.observation_key,
    }
    evidence = {
        "raw_observation_id": str(raw_observation_id),
        "field_path": "/raw_cells" if _has_cells(observation) else "/raw_text",
        "confidence": _confidence_text(fields.get("confidence"), observation.confidence),
    }
    normalized: dict[str, Any] = {"mbb_terms": []}
    for source_key, normalized_key in (
        ("supplier_sku", "supplier_sku"),
        ("description", "product_name"),
        ("brand", "brand"),
        ("category", "category"),
        ("barcode", "barcode"),
        ("variant", "variant"),
    ):
        value = _text(fields.get(source_key))
        if value is not None:
            normalized[normalized_key] = {"value": value, "evidence": evidence}

    cost = _cost_proposal(fields.get("cost_price"), runtime_contract, evidence)
    if cost is not None:
        normalized["cost"] = cost
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
    )


def _cost_proposal(value: Any, runtime_contract, evidence: dict[str, Any]) -> dict[str, Any] | None:
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


def _packaging_proposal(fields: dict[str, Any], runtime_contract, evidence: dict[str, Any]) -> dict[str, Any] | None:
    source_text = _text(fields.get("pack_size") or fields.get("uom"))
    semantics = runtime_contract.declaration.packaging
    if not source_text and semantics.price_basis is None:
        return None
    proposal: dict[str, Any] = {"source_text": source_text, "evidence": evidence}
    if semantics.price_basis is not None:
        proposal["price_basis"] = semantics.price_basis.model_dump(mode="json")
    content = _content_measure(source_text)
    if content:
        amount, uom = content
        proposal["content_amount"] = str(amount)
        proposal["content_uom"] = {"code": uom}
    order_increment = _decimal_or_none(fields.get("order_increment_qty"))
    if order_increment is not None and semantics.price_basis is not None and semantics.price_basis.code is not None:
        proposal["order_increment"] = {
            "amount": str(order_increment),
            "uom": semantics.price_basis.model_dump(mode="json"),
        }
    return proposal


def _content_measure(text: str | None) -> tuple[Decimal, str] | None:
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(ml|mL|ML|g|G|kg|KG|l|L)\b", text)
    if not match:
        return None
    amount = Decimal(match.group(1))
    raw_uom = match.group(2).upper()
    uom = {
        "ML": UnitCode.ML.value,
        "G": UnitCode.G.value,
        "KG": UnitCode.KG.value,
        "L": UnitCode.L.value,
    }[raw_uom]
    return amount, uom


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
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() in {"by quote", "quote", "n/a", "na"}:
        return None
    try:
        decimal = Decimal(str(value).replace(",", "").replace("$", "").replace("HKD", "").replace("HK$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return decimal if decimal >= 0 else None


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
    return re.sub(r"\s+", " ", value).strip().lower()


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
    "ORDER_INCREMENT": "order_increment_qty",
}


__all__ = [
    "ConformanceOutcome",
    "ConformedRow",
    "conform_observations",
]
