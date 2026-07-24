# Catalogue Pipeline Contracts

## Status

CIS-103 adds standalone, versioned Pydantic v2 contracts for catalogue pipeline payloads. These models define data passed between stages and remain the authoritative boundary contracts. The catalogue logical persistence task adds SQLAlchemy tables and mapper services for these contracts. The v2 submission boundary records a source file and queued run. Prefect orchestration can now process that queued run through Raw Observation, Staging, Validation Issue and pending-review Mastering Candidate creation. HITL review, approved commercial application, serving publication and public serving API payloads remain outside the automatic path.

The Python models in `apps/api/schemas/catalogue_pipeline/` are the authoritative contracts. JSON fixtures are examples and regression inputs. JSON Schema files in `docs/contracts/catalogue-pipeline/v1/` are generated interoperability artifacts.

CIS-103B adds a separate supplier-source contract layer for incoming supplier catalogue formats. See `docs/architecture/catalogue-supplier-source-contracts.md`.

## Boundary Map

| Boundary | Contract ID | Question answered | Main rule |
|---|---|---|---|
| Source extraction -> Raw Observation | `catalogue.extracted_evidence.v1` | What did extraction observe, and where? | Requires raw evidence and a meaningful source locator; no review/mastering facts. |
| Raw Observation -> Staging Catalogue Item | `catalogue.interpreted_claim.v1` | What source fields were present, and what do we propose they mean? | Raw strings and proposed typed fields are separate. |
| Staging -> Mastering/HITL | `catalogue.mastering_candidate.v1` | How might this item resolve into canonical and supplier-commercial entities? | It is a candidate; confirmed/approved assertions require lineage. |
| Cross-cutting validation | `catalogue.validation_issue.v1` | What is uncertain, invalid, contradictory, or needs a business decision? | Blocking open issues prevent publication by definition. |
| Mastering -> Serving | `catalogue.serving_item.v1` | What approved information can consumers use? | Only `APPROVED` or `APPROVED_WITH_OVERRIDE` records validate. |
| Extraction profile config | `catalogue.extraction_profile.v1` | Which supplier-format rules guided extraction? | Versioned Pydantic config envelope; not a YAML contract system. |

## Evidence, Proposal, Mastering, Publication

Rosetta must preserve four different meanings:

- Source evidence: what the supplier catalogue contained.
- Observation: what OCR, spreadsheet parsing, or a model observed in that source.
- Proposal: what Rosetta thinks the observation means.
- Mastered decision: what BizOps/HITL approved or rejected.
- Publication: what downstream inventory views may consume as approved fact.

CIS-103 keeps these meanings in different contracts. A parser proposal is never represented as approved truth, and raw evidence is never mutated to agree with a proposal.

## Removed Legacy YAML Mappings

The repository no longer ships files under `apps/api/catalogue_contracts/*.yaml`. Those YAML files were legacy supplier extraction mappings, not CIS-103 Pipeline Contracts, and they were not the source of truth for these models.

Use these names consistently:

- Removed legacy extraction mapping: historical YAML-style parser guidance for the old loader; not shipped as contract files.
- Extraction Profile Contract: `catalogue.extraction_profile.v1`, the new typed/versioned Pydantic configuration contract.
- Pipeline Contract: a Pydantic payload model defining a stage boundary.

Current upload behavior uses `services/supplier_source_contract_runtime.py`, a Pydantic-backed adapter that selects supported supplier-source declarations from the registry. Supplier-only runtime selection is compatible only when exactly one `SUPPORTED` source format exists for the supplier; multiple supported formats require an explicit `contract_id` and `contract_version` or a later format-detection path. Future integration should continue validating extraction configuration through Pydantic models, not revive YAML files as contracts.

## Versioning Rules

Each public contract has an exact `Literal[...]` `contract_version`. Unknown IDs are rejected through `get_contract_model(contract_id)`.

Compatible change: adding an optional field without changing existing meaning.

Breaking changes: renaming/removing fields, changing meanings, making optional fields required, or removing accepted enum values. Breaking changes require a new major contract ID/module/class so historical v1 payloads remain readable.

## Validation

Validate a payload by resolving its model through the public registry:

```python
from schemas.catalogue_pipeline import get_contract_model

model = get_contract_model(payload["contract_version"])
contract = model.model_validate(payload)
```

All public models reject extra undeclared fields. Extensibility is explicit through fields such as `metadata`, `source_metadata`, or typed profile extension sections.

`SourceLocation` must contain at least one meaningful locator, such as a 1-based page number, sheet name, 1-based row number, cell range, bounding box, or source-object key. Blank locator strings are rejected, bounding boxes require positive dimensions, and raw/staging/mastering lineage lists reject duplicate raw-observation IDs. These cross-field rules are enforced by the Pydantic models even when JSON Schema cannot express every invariant.

## JSON Schema Export

Generate schemas:

```bash
cd apps/api
UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt \
  python scripts/export_catalogue_pipeline_schemas.py
```

Check committed schemas:

```bash
cd apps/api
UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python scripts/export_catalogue_pipeline_schemas.py --check
```

## Current Persistence Audit

The current SQLAlchemy models remain runtime state, not approved CIS-103 contracts. The audit below constrained the Pydantic design so the new contracts do not inherit table-shaped defects.

| Current structure | Classification | Notes for CIS-103 |
|---|---|---|
| `Supplier` | `REUSABLE_WITH_LATER_MIGRATION` | Useful supplier identity, aliases, and segment fields exist, but supplier terms are mostly free text and not a typed commercial model. |
| `Product` | `REUSABLE_WITH_LATER_MIGRATION` | Represents Product Variant / Canonical SKU today. Product Family is missing and grouping must remain optional enrichment. Money fields use `Float`; category/brand are strings. |
| `ProductSupplier` | `REUSABLE_WITH_LATER_MIGRATION` | Closest current Supplier Product table. It stores supplier SKU, barcode, cost, pack, ordering terms, and cost provenance, but cost is `Float`, HKD is implicit, price basis is not explicit, and history/effective dating are weak. |
| `MbbTerm` | `REUSABLE_WITH_LATER_MIGRATION` | Relational 0..N terms are a good direction, but current `kind` plus nullable columns is still flatter than the required condition+benefit discriminated model and uses `Float` for spend/discount/cost. |
| `CatalogueImport` | `REUSABLE_WITH_LATER_MIGRATION` | Useful source-file/import envelope with supplier detection and `source_ref`; lacks UUID pipeline IDs, extraction profile reference, typed source metadata, and effective catalogue dates. |
| `CatalogueItem` | `SEMANTICALLY_INADEQUATE` | Useful as current review queue evidence, but raw, proposed, reviewed, and committed meanings share columns. Cost/packaging are flattened, monetary values are `Float`, raw/proposed lineage is incomplete, and review state is too coarse. |
| `CatalogueCostStaging` | `SEMANTICALLY_INADEQUATE` | Stages only cost and match confidence. It lacks basis, currency, raw observation lineage, validation issue linkage, and durable review semantics. |
| `CatalogueAuditEvent` | `REUSABLE_WITH_LATER_MIGRATION` | Append-only decision evidence is useful, but action/details are free text/JSON and not a typed Review Decision contract. |
| `ReparseBatch` | `REUSABLE_WITH_LATER_MIGRATION` | Good staged-diff boundary for reparse runs, but scoped to retained text recapture and not a general Mastering Candidate or validation issue model. |
| `ReparseChange` | `REUSABLE_WITH_LATER_MIGRATION` | Preserves old/new field diffs before confirmed apply, but values are strings/floats and field semantics are not typed. |
| Packaging fields (`uom`, `pack_unit`, `units_per_pack`, `pack_size`, order terms) | `SEMANTICALLY_INADEQUATE` | Current fields partially separate sell UOM, buy UOM, pack size, order increment, and MOQ, but content measure and price basis can still be ambiguous. |
| Validation Issue / durable issue table | `IMPLEMENTED_FOUNDATION` | `catalogue_validation_issues` now stores typed severity/status, review guidance, resolution metadata and publish-blocking query fields. Runtime ingestion does not emit it yet. |
| Raw Observation table/contract | `IMPLEMENTED_FOUNDATION` | `catalogue_extracted_evidence` now stores source text/cells, source location, extractor metadata and UUID lineage. Runtime ingestion does not emit it yet. |
| Mastering Candidate contract | `MISSING` | Current HITL flow writes to runtime tables; there is no standalone candidate payload with per-section resolution states and lineage. |
| Serving Item contract | `IMPLEMENTED_FOUNDATION` | `catalogue_serving_publications` now stores approved serving snapshots and lineage. Existing API responses do not read it yet. |
| Extraction Profile Contract | `MISSING` | Legacy YAML exists, but no typed/versioned Pydantic configuration contract validates supplier-format extraction profiles. |
| Inventory, stock, sales, channels, competitor price, Client SSOT | `OUT_OF_SCOPE` | These consume catalogue identity/cost or are adjacent operational domains; CIS-103 does not model them. |

Key risks found and carried into the new contract design:

- Catalogue source evidence can be conflated with canonical product data in current tables.
- Raw, proposed, reviewed, and published values share fields in `CatalogueItem` and downstream writes.
- Product Variant and Product Family are conflated because `Product` is the SKU row and no family table exists.
- Product grouping is absent, so the contracts keep `product_family_id` optional.
- Cost is `Float` without explicit currency, basis, history, or effective dates in several current models.
- Price basis, purchase unit, sellable unit, content measure, order increment, and MOQ need separate fields.
- `30 ML` and `410 G` must remain content measures, not sellable-unit counts.
- Case configuration does not prove whole-case-only purchasing, so break-pack semantics remain nullable.
- MBB must be modelled as condition + benefit, not nullable scalar columns.
- Durable validation issues and business-readable review guidance now have a persistence foundation, but the current ingestion runtime does not write those rows yet.
- Lineage from mastered/served values back to staging/raw evidence is incomplete.

## Persistence Foundation

The catalogue logical persistence task maps contracts to logical entities without making the contracts table-shaped. The design is documented in `docs/architecture/catalogue-logical-persistence-model.md`; the implementation lives in `apps/api/v2/models/catalogue_pipeline.py` and `apps/api/services/catalogue_pipeline_persistence.py`.

- Raw Observation is an immutable evidence table keyed by UUID and linked to source file/catalogue/import UUIDs.
- Staging Catalogue Item preserves raw fields, proposed typed fields, issue references, review requirement and raw-observation lineage.
- Mastering Candidate maps to reviewable resolution-section snapshots and typed review decisions.
- Validation Issue is durable and independently resolvable.
- Serving Item is a publication/read-model snapshot derived only from approved mastering decisions.
- Supplier Product, Packaging Configuration, Supplier Price and MBB Term now have normalized foundation tables; legacy runtime tables remain compatibility projections until integration work replaces the current reads/writes.

## Non-Wiring Statement

CIS-103 and the logical persistence foundation do not wire the shared pipeline payload contracts into `/v1/catalogues`, `/v1/catalogues/reparse`, the current UI, human review, approved commercial application or serving publication. The v2 API exposes a queued submission boundary, and Prefect orchestration can claim queued runs to create Raw Observation, Staging, Validation Issue and pending-review Mastering Candidate records. It still does not approve, apply or publish Serving payload records.
