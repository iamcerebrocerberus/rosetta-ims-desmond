# Supplier-Source Catalogue Contracts

CIS-103B adds a separate supplier-source contract layer for incoming supplier catalogue formats. These contracts describe the shape and semantics of source documents before Rosetta creates shared `RawObservationV1` and `StagingCatalogueItemV1` payloads.

The Python models in `apps/api/schemas/catalogue_pipeline/supplier_contracts/` are authoritative. JSON fixtures and JSON Schemas are generated/review artifacts. The repository no longer ships the old files under `apps/api/catalogue_contracts/*.yaml`; those files were legacy extraction mappings, not contracts.

## Contract Architecture

```
Supplier source contract
  supplier + document format + version
  e.g. hills.price_list.v1
        |
        v
Shared Raw Observation contract
        |
        v
Shared Staging Catalogue Item contract
        |
        v
Shared Mastering Candidate / Validation Issue / Serving contracts
```

| Layer | Contract source of truth | Purpose |
|---|---|---|
| Supplier-specific source contract | `SupplierSourceContractV1` declarations registered by supplier-format ID | Defines expected document structure, source fields, price/packaging/MBB semantics, validation rules, and known ambiguities. |
| Raw Observation | `catalogue.extracted_evidence.v1` | Preserves extracted source evidence and exact location. |
| Staging Item | `catalogue.interpreted_claim.v1` | Separates raw source strings from proposed typed interpretations. |
| Mastering/HITL | `catalogue.mastering_candidate.v1` plus `catalogue.validation_issue.v1` | Proposes canonical/supplier-commercial resolution and records decisions needed from BizOps. |
| Serving | `catalogue.serving_item.v1` | Exposes only approved information to consumer-facing views or APIs. |

A supplier-source contract identity is per supplier plus document format plus major version, not merely per supplier. One supplier can have `supplier.price_list.v1`, `supplier.promotion_sheet.v1`, and later `supplier.price_list.v2`.

## Runtime Boundary

Runtime ingestion now uses `services/supplier_source_contract_runtime.py` as a small adapter over the Pydantic supplier-source registry. The authoritative resolver supports exact selection by `supplier_id`, `contract_id`, and `contract_version`; it verifies that the contract belongs to the supplied supplier and that the declaration is `SUPPORTED`. It never falls back from an unknown ID or unsupported version to another format.

Supplier-only selection is allowed only when exactly one `SUPPORTED` declaration exists for the supplier. If no supported contract exists, runtime falls back to generic extraction only through the current compatibility wrapper. If multiple supported formats exist for a supplier, the resolver raises an ambiguity error and requires an explicit contract identity or a later document-format detection path.

The v1 compatibility upload and reparse callers still pass only the numeric supplier ID. Hill's and Alfamedic therefore remain compatible there because each has exactly one supported registered format. The v2 queued submission boundary accepts optional explicit `contract_id` and `contract_version` and stores the exact selected identity on the source document and ingestion run. Prefect orchestration later resolves that recorded identity exactly and does not use supplier-only fallback.

Vetapet and KPN/Kangaroo declarations are not production-selected because they still need row fixtures, supplier-id reconciliation, or per-section parser rules.

Runtime integration should continue to:

1. Select a supplier-source contract by explicit `contract_id` and `contract_version`.
2. Validate the declaration through `SupplierSourceContractV1`.
3. Use its source-field and validation rules to produce `RawObservationV1`.
4. Interpret proposed business fields into `StagingCatalogueItemV1`.
5. Create `ValidationIssueV1` records instead of guessing unresolved price, packaging, or MBB semantics.

## Evidence Rules

Set `support_status` conservatively:

| Status | Meaning |
|---|---|
| `SUPPORTED` | Real source samples and business rules are sufficient for production interpretation once runtime integration exists. |
| `PARTIALLY_VERIFIED` | Parser behavior, tests, and/or docs support important semantics, but source samples or business confirmation are missing. |
| `UNVERIFIED` | Contract identity is known, but evidence is too thin to apply semantics automatically. |
| `DEPRECATED` | Historical format retained for validation or audit only. |

Historical YAML-style mappings are insufficient for `SUPPORTED`. A format needs raw source samples, representative extracted rows, confirmed column/header semantics, price basis, packaging/order semantics, and MBB/promotion rules where applicable.

## Row Evidence Fixtures

Every registered supplier-source contract must have a row-evidence fixture under:

```text
apps/api/tests/fixtures/catalogue_pipeline/supplier_source/v1/row_examples/
```

The fixture filename is:

```text
{contract_id}.rows.json
```

`SUPPORTED` contracts require `row_evidence_status = CONFIRMED` and at least one source-located example row that can run through the runtime adapter in tests. Each confirmed example records the source sample, page number, extraction method, raw row values, runtime input, expected runtime output, and business semantics such as price basis and packaging interpretation.

Non-supported contracts use `NEEDS_CONFIRMATION` or `TECHNICAL_DEBT` with an explicit debt list. These fixtures are still required so missing evidence is visible and testable rather than hidden in prose.

## Coverage Audit

My prompt referenced documentation for about 24 suppliers, but this clean checkout does not contain that full inventory. The local seed file lists nine starter suppliers, `supplier_import.py` can import larger external supplier sheets, and the domain dictionary records historical YAML-style extraction mappings that have now been removed from the repository. The table below reflects repository evidence plus the source samples I supplied locally for CIS-103B follow-up.

| Supplier | Contract ID | Version | Document format | Status | Runtime selectable | Evidence | Known gaps |
|---|---|---:|---|---|---|---|---|
| Alfamedic | `alfamedic.price_list.v1` | `v1` | PDF price list | `SUPPORTED` | Yes, supplier ID `1` | Real source catalogue sample; parser behavior; existing test extraction fixture; business/domain documentation | MBB tier semantics still need later runtime parsing evidence. |
| Hill's | `hills.price_list.v1` | `v1` | PDF price list | `SUPPORTED` | Yes, supplier ID `14` | Real source catalogue sample; parser behavior; existing test extraction fixture; business/domain documentation | Supplier code remains unasserted. |
| C. Vetapet & Company / Vetapet Vet | `vetapet.vet_price_list.v1` | `v1` | Mixed PDF catalogue/price list | `PARTIALLY_VERIFIED` | No | Real source catalogue sample; parser behavior; existing test extraction fixture | Several table layouts (`UNIT PRICE`, `WHOLESALE/RETAIL/TERMS`, Chinese wholesale/retail); split or per-section parser rules needed. |
| C. Vetapet & Company / Vetapet Non-Vet | `vetapet.non_vet_price_list.v1` | `v1` | PDF price list section | `PARTIALLY_VERIFIED` | No | Real source catalogue sample; parser behavior | Price basis and representative non-vet row fixtures remain missing. |
| Kangaroo Pet Nutrition Ltd / KPN | `kangaroo.mixed_price_catalogue.v1` | `v1` | Mixed PDF catalogue | `PARTIALLY_VERIFIED` | No | Real source catalogue sample; supplier label I supplied | Deferred technical debt: confirm whether supplier ID `15` (`K.P.N. Trading`) or `81` (`Kangaroo Pet Nutrition`) owns this document, then add representative row fixtures. |
| Kangaroo Pet Nutrition Ltd / KPN | `kangaroo.purina_proplan_veterinary_diets.v1` | `v1` | Purina Pro Plan Veterinary Diets product list | `PARTIALLY_VERIFIED` | No | Real source catalogue sample; supplier label I supplied | Deferred technical debt: confirm supplier ID `15` or `81`, then add dry/supplement/wet-can row fixtures. |
| Kangaroo Pet Nutrition Ltd / KPN | `kangaroo.earthz_pet_price_sheet.v1` | `v1` | Earthz Pet image-only price sheet | `UNVERIFIED` | No | Real source catalogue sample; visual inspection | Deferred technical debt: confirm supplier ID `15` or `81`, then add OCR/vision row fixtures with bounding boxes and price-basis confirmation. |

Additional local supplier names without enough source-format evidence remain unimplemented:

| Supplier | Document format | Evidence available | Contract identity | Gap |
|---|---|---|---|---|
| Arrowana Int'l Ltd | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |
| Asia Vet Medical Limited | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |
| Blue Pet Co | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |
| BuggyBix | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |
| Caesars | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |
| Etta International | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |
| Happypaws Int'l Ltd | Unknown | Missing | TBD | Need document type/version, source samples, parser fixtures, and business rules. |

## Adding A Supplier Format

1. Add or confirm the supplier identity without inventing a supplier code.
2. Gather evidence: raw catalogue sample, extracted row fixture, business/domain rule, and any relevant parser behavior.
3. Add a declaration under `apps/api/schemas/catalogue_pipeline/supplier_contracts/suppliers/`.
4. Use `SupplierSourceContractV1` components for fields, source structure, pricing, packaging, MBB, validation, and ambiguity rules.
5. Register the declaration by stable identity such as `supplier_slug.price_list.v1`.
6. Add valid/invalid contract fixtures and a row-evidence fixture under `apps/api/tests/fixtures/catalogue_pipeline/supplier_source/v1/`.
7. Export schemas:

```bash
cd apps/api
UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt \
  python scripts/export_supplier_source_contract_schemas.py
```

8. Check schemas:

```bash
cd apps/api
UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt \
  python scripts/export_supplier_source_contract_schemas.py --check
```

Do not add a permissive generic fallback. If the supplier or document version is unknown, the registry must fail and route the item to explicit review or a later integration path.
