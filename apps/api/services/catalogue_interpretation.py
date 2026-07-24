"""Post-Raw interpretation of persisted catalogue evidence.

This module is the only place where verbatim source evidence becomes proposed
interpreted-claim fields. It consumes Raw-persisted observations (``raw_text`` or
``raw_cells``) together with the resolved supplier-source contract, and it
proposes typed fields with per-field evidence pointing back at the supporting
extracted evidence observation.

Boundaries this module enforces:

- Extraction records; interpretation proposes. Nothing here re-reads the
  source file — only persisted evidence.
- Non-catalogue lines (titles, section banners, column headers) are skipped
  from Staging while their extracted evidence observations remain persisted.
- Interpretation failures degrade: observations become claims with empty proposals and
  route to human review. They never invent values and never fail the run.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from schemas.catalogue_pipeline.enums import UnitCode
from services.catalogue_evidence_extraction import ExtractedEvidence

INTERPRETATION_MODEL = "claude-haiku-4-5-20251001"
MAX_INTERPRETATION_TOKENS = 8192
_ROWS_PER_MODEL_CALL = 40

# Semantic field vocabulary shared with the supplier-source contract prompt
# sections (SourceFieldRole -> output key). Interpretation output is expressed
# in these keys; interpreted claim building consumes them.
_SEMANTIC_KEYS = (
    "supplier_sku",
    "description",
    "original_description",
    "brand",
    "category",
    "cost_price",
    "rrp",
    "pack_size",
    "uom",
    "barcode",
    "variant",
    "bulk_buy_tiers",
    "order_increment_qty",
    "confidence",
)

INTERPRETATION_PROMPT = """Interpret catalogue rows from verbatim source evidence.

You receive one JSON object: {"rows": {"<observation_key>": "<verbatim row text>", ...}}.
A supplier source contract describing this document follows the JSON.

Rules:
- Use ONLY the verbatim text of each row. Never invent values, never borrow
  values from other rows, never normalize units, currencies, or SKUs.
- A row that is not one product/offer line (document titles, section banners,
  column headers, footers, page numbers) maps to null.
- Copy values exactly as printed. Ambiguous discount/bulk text goes verbatim
  into bulk_buy_tiers without interpretation.

Return ONE JSON object: {"rows": {"<observation_key>": null | {<fields>}}}.
Fields (all optional, omit when absent): supplier_sku, description,
original_description, brand, category, cost_price, rrp, pack_size, uom,
barcode, variant, bulk_buy_tiers, order_increment_qty,
confidence (decimal string in [0, 1]).
Return only the JSON object, without Markdown fences.
"""


class InterpretationUnavailable(RuntimeError):
    """No interpretation provider is configured."""


class InterpretationProviderError(RuntimeError):
    """The interpretation provider failed in a non-retryable way."""


class InterpretationTransientError(RuntimeError):
    """The interpretation provider failed in a retryable way."""


@dataclass(frozen=True)
class InterpretedItem:
    """One interpreted claim derived from exactly one persisted evidence observation.

    ``provenance`` records WHO produced the proposals (model identity,
    deterministic contract-cell mapping, or none when degraded), whether the
    provider returned no verdict, and which model values were dropped by the
    grounding check. It is persisted onto the claim's metadata.
    """

    observation_key: str
    raw_observation_id: UUID
    raw_fields: dict[str, Any]
    proposed_fields: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterpretationOutcome:
    """Interpretation results plus durable verdict accounting.

    ``metadata`` carries machine-readable accounting (interpreted /
    skipped-non-catalogue / no-verdict / grounding-dropped counts and the
    degraded flag) so the run record can distinguish "interpreted as non-row"
    from "never interpreted".
    """

    items: tuple[InterpretedItem, ...]
    warnings: tuple[str, ...] = ()
    skipped_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def interpret_observations(
    observations: tuple[ExtractedEvidence, ...],
    raw_observation_ids: tuple[UUID, ...],
    runtime_contract,
) -> InterpretationOutcome:
    """Propose interpreted-claim fields for persisted observations using the contract."""

    if len(observations) != len(raw_observation_ids):
        raise ValueError("observations and evidence ids must align")

    warnings: list[str] = []
    pairs = tuple(zip(observations, raw_observation_ids, strict=True))
    text_rows = {
        observation.observation_key: observation.raw_text
        for observation, _ in pairs
        if not _has_cells(observation) and observation.raw_text
    }

    model_fields: dict[str, dict[str, Any] | None] = {}
    model_degraded = False
    if text_rows:
        try:
            model_fields = _model_interpret_rows(text_rows, runtime_contract)
        except InterpretationTransientError:
            raise
        except InterpretationUnavailable:
            model_degraded = True
            warnings.append(
                f"interpretation provider not configured; {len(text_rows)} "
                "text observation(s) staged for manual review"
            )
        except InterpretationProviderError as exc:
            model_degraded = True
            warnings.append(
                f"interpretation provider failed ({exc}); {len(text_rows)} "
                "text observation(s) staged for manual review"
            )

    items: list[InterpretedItem] = []
    skipped = 0
    no_verdict = 0
    grounding_dropped_total = 0
    for observation, raw_id in pairs:
        key = observation.observation_key
        if _has_cells(observation):
            fields = _fields_from_cells(observation, runtime_contract)
            if fields is None:
                skipped += 1
                continue
            provenance = _provenance("contract_cells")
            items.append(_item_from_fields(observation, raw_id, fields, runtime_contract, provenance=provenance))
            continue

        if key in model_fields:
            fields = model_fields[key]
            if fields is None:
                skipped += 1
                continue
            grounded, dropped = _ground_model_fields(_sanitize_fields(fields), observation)
            grounding_dropped_total += len(dropped)
            item_warnings: tuple[str, ...] = ()
            if dropped:
                item_warnings = tuple(f"ungrounded {name} value dropped by grounding check" for name in dropped)
                warnings.append(f"{key}: ungrounded value(s) dropped by grounding check: {', '.join(dropped)}")
            provenance = _provenance("model", grounding_dropped=dropped)
            items.append(
                _item_from_fields(
                    observation, raw_id, grounded, runtime_contract,
                    provenance=provenance, warnings=item_warnings,
                )
            )
            continue

        item_warnings = ()
        if text_rows and not model_degraded:
            no_verdict += 1
            item_warnings = ("interpretation returned no verdict for this observation",)
            warnings.append(f"{key}: no interpretation verdict; staged for manual review")
            provenance = _provenance("model", no_verdict=True)
        else:
            provenance = _provenance("none", degraded=True)
        items.append(
            _item_from_fields(observation, raw_id, {}, runtime_contract, provenance=provenance, warnings=item_warnings)
        )

    return InterpretationOutcome(
        items=tuple(items),
        warnings=tuple(warnings),
        skipped_count=skipped,
        metadata={
            "interpreted_items": len(items),
            "skipped_non_catalogue": skipped,
            "no_verdict_items": no_verdict,
            "grounding_dropped_total": grounding_dropped_total,
            "degraded": model_degraded,
        },
    )


def _provenance(
    interpreter: str,
    *,
    degraded: bool = False,
    no_verdict: bool = False,
    grounding_dropped: tuple[str, ...] = (),
) -> dict[str, Any]:
    provenance: dict[str, Any] = {"interpreter": interpreter}
    if interpreter == "model":
        provenance["model"] = INTERPRETATION_MODEL
        provenance["model_version"] = INTERPRETATION_MODEL
    if degraded:
        provenance["degraded"] = True
    if no_verdict:
        provenance["no_verdict"] = True
    if grounding_dropped:
        provenance["grounding_dropped"] = list(grounding_dropped)
    return provenance


def _ground_model_fields(
    fields: dict[str, Any],
    observation: ExtractedEvidence,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Mechanical groundedness check for model-produced values.

    Every proposed value must appear verbatim (casefolded, whitespace-free
    containment; currency tokens stripped for numeric fields) in the
    observation's raw text/cells. Ungrounded values are DROPPED — never
    silently kept — and reported so the claim routes to human review. This
    enforces the no-invention contract mechanically and blunts prompt
    injection via malicious catalogue rows. Deterministic cell-mapped fields
    and contract constants are grounded by construction and are not checked
    here.
    """

    corpus_parts: list[str] = []
    if observation.raw_text:
        corpus_parts.append(observation.raw_text)
    for cell in observation.raw_cells:
        if cell.raw_value is not None:
            corpus_parts.append(str(cell.raw_value))
        if cell.column_name:
            corpus_parts.append(cell.column_name)
    corpus = _fold_for_grounding(" ".join(corpus_parts))

    grounded: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in fields.items():
        if key == "confidence" or value is None or str(value).strip() == "":
            grounded[key] = value
            continue
        candidate = str(value)
        if key in {"cost_price", "rrp", "order_increment_qty"}:
            candidate = candidate.replace(",", "").replace("$", "").replace("HKD", "").replace("HK$", "")
        if _fold_for_grounding(candidate) in corpus:
            grounded[key] = value
        else:
            dropped.append(key)
    return grounded, tuple(dropped)


def _fold_for_grounding(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _model_interpret_rows(rows: dict[str, str], runtime_contract) -> dict[str, dict[str, Any] | None]:
    """Interpret verbatim text rows with the configured model provider.

    Module-level seam: tests replace this function. Raises
    InterpretationUnavailable when no provider is configured,
    InterpretationTransientError on retryable provider failures, and
    InterpretationProviderError otherwise.
    """

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise InterpretationUnavailable("ANTHROPIC_API_KEY is not configured")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    interpreted: dict[str, dict[str, Any] | None] = {}
    keys = list(rows)
    for start in range(0, len(keys), _ROWS_PER_MODEL_CALL):
        chunk = {key: rows[key] for key in keys[start : start + _ROWS_PER_MODEL_CALL]}
        prompt = (
            INTERPRETATION_PROMPT
            + "\n"
            + runtime_contract.prompt_section()
            + "\n\nRows:\n"
            + json.dumps({"rows": chunk}, ensure_ascii=False)
        )
        try:
            message = client.messages.create(
                model=INTERPRETATION_MODEL,
                max_tokens=MAX_INTERPRETATION_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - provider surface is broad
            raise _classify_interpretation_failure(exc) from exc
        text_blocks = [block.text for block in message.content if getattr(block, "type", None) == "text"]
        try:
            payload = _strict_json_object("\n".join(text_blocks))
            chunk_rows = payload["rows"]
            if not isinstance(chunk_rows, dict):
                raise ValueError("rows must be an object")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise InterpretationProviderError("interpretation provider returned an invalid envelope") from exc
        for key, value in chunk_rows.items():
            if key not in chunk:
                continue
            if value is None or isinstance(value, dict):
                interpreted[key] = value
    return interpreted


def _fields_from_cells(observation: ExtractedEvidence, runtime_contract) -> dict[str, Any] | None:
    """Deterministically map named cells through the contract's source columns.

    Returns None when the observation is a header row (its cells repeat the
    contract's declared source columns) — headers are evidence, not items.
    """

    column_targets: dict[str, str] = {}
    for contract_field in runtime_contract.declaration.fields:
        target = _role_target(contract_field.role)
        if target is None:
            continue
        for source_name in filter(None, (contract_field.source_column, contract_field.source_path)):
            column_targets[_fold(source_name)] = target

    named_cells = [
        cell
        for cell in observation.raw_cells
        if cell.column_name and cell.raw_value is not None and str(cell.raw_value).strip()
    ]
    header_hits = sum(1 for cell in named_cells if _fold(str(cell.raw_value)) in column_targets)
    if named_cells and header_hits >= max(2, len(named_cells) - 1):
        return None

    fields: dict[str, Any] = {}
    for cell in named_cells:
        target = column_targets.get(_fold(cell.column_name))
        if target and target not in fields:
            fields[target] = str(cell.raw_value)
    for contract_field in runtime_contract.declaration.fields:
        target = _role_target(contract_field.role)
        if target and contract_field.constant_value is not None and target not in fields:
            fields[target] = contract_field.constant_value
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
) -> InterpretedItem:
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
    proposed: dict[str, Any] = {"mbb_terms": []}
    for source_key, proposed_key in (
        ("supplier_sku", "supplier_sku"),
        ("description", "product_name"),
        ("brand", "brand"),
        ("category", "category"),
        ("barcode", "barcode"),
        ("variant", "variant"),
    ):
        value = _text(fields.get(source_key))
        if value is not None:
            proposed[proposed_key] = {"value": value, "evidence": evidence}

    cost = _cost_proposal(fields.get("cost_price"), runtime_contract, evidence)
    if cost is not None:
        proposed["cost"] = cost
    packaging = _packaging_proposal(fields, runtime_contract, evidence)
    if packaging is not None:
        proposed["packaging"] = packaging

    return InterpretedItem(
        observation_key=observation.observation_key,
        raw_observation_id=raw_observation_id,
        raw_fields=raw_fields,
        proposed_fields=proposed,
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
        # Currency is a CONTRACT declaration, never an interpretation default.
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


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: fields[key] for key in _SEMANTIC_KEYS if key in fields}


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


def _strict_json_object(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("provider response must be one JSON object")
    return value


def _classify_interpretation_failure(exc: Exception) -> Exception:
    """Typed retry classification for the interpretation provider seam.

    Mirrors the Stage 3 vision seam: timeouts/connection/rate-limit/5xx are
    transient (task retries); authentication/permission failures degrade like
    an unconfigured provider (claims stage unproposed for review — the run
    does not fail); bad requests and schema violations are non-retryable
    provider errors (also degrade). Falls back to a conservative string
    heuristic only for non-SDK exceptions.
    """

    try:
        import anthropic

        if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError)):
            return InterpretationTransientError("interpretation provider failed temporarily")
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return InterpretationUnavailable("interpretation provider rejected the configured credentials")
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code in {408, 409, 429} or exc.status_code >= 500:
                return InterpretationTransientError("interpretation provider failed temporarily")
            return InterpretationProviderError("interpretation provider request failed")
    except ImportError:
        pass
    if _looks_transient(exc):
        return InterpretationTransientError("interpretation provider failed temporarily")
    return InterpretationProviderError("interpretation provider request failed")


def _looks_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in ("timeout", "timed out", "rate limit", "overloaded", "temporar", "connection", "503", "529")
    )


__all__ = [
    "InterpretationOutcome",
    "InterpretationProviderError",
    "InterpretationTransientError",
    "InterpretationUnavailable",
    "InterpretedItem",
    "interpret_observations",
]
