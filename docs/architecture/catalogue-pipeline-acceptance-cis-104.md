# CIS-104 End-to-End Acceptance Evidence

Status: source-backed acceptance proof added
Date: 2026-07-23

This note records the current end-to-end acceptance proof for EPIC CIS-03 and
story CIS-104. It is deliberately narrower than a product demo: it proves that
the implemented backend boundaries compose correctly and that served fields are
grounded in uploaded source content, without adding UI, automatic approval,
automatic publication, or new supplier semantics.

## Repository Audit

Initial inspected `main`: `fc868df503ae37a40f86ac221f894d9c205eb2cf`

Open GitHub pull requests at audit time: none.

Pre-edit verification on `main`:

```bash
cd apps/api
UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python -m pytest -q \
  tests/test_catalogue_submission_boundary.py \
  tests/test_api_versioning.py \
  tests/test_ingestion_run.py \
  tests/test_catalogue_pipeline_persistence.py \
  tests/test_catalogue_pipeline_stage_services.py \
  tests/test_catalogue_prefect_orchestration.py
# 53 passed

UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python -m pytest -q
# 265 passed
```

Current implementation evidence:

| Boundary | Evidence |
|---|---|
| Supplier and pipeline contracts | `apps/api/schemas/catalogue_pipeline/` |
| Supplier contract runtime selection | `apps/api/services/supplier_source_contract_runtime.py` |
| Durable source submission | `apps/api/services/catalogue_submission.py`, `apps/api/routers/v2/catalogues.py` |
| Pipeline persistence | `apps/api/v2/models/catalogue_pipeline.py`, `apps/api/services/catalogue_pipeline_persistence.py` |
| Stage services | `apps/api/services/catalogue_pipeline_stages.py` |
| Prefect orchestration | `apps/api/orchestration/catalogue_flows.py`, `apps/api/orchestration/catalogue_tasks.py` |
| Serving publication contract reconstruction | `serving_item_to_contract` in `apps/api/services/catalogue_pipeline_persistence.py` |

## Vertical Slice

`apps/api/tests/test_catalogue_pipeline_e2e_acceptance.py` drives the smallest
representative source-backed slice:

```text
v2 authenticated submission
-> durable source file and queued run
-> Prefect orchestration
-> Raw Observations
-> Staging Items
-> Validation Issue for invalid row
-> pending-review Mastering Candidate for valid row
-> explicit test-reviewer approval
-> approved Supplier Offer, packaging and supplier price
-> Serving publication
-> ServingItemV1 reconstruction
-> relational and evidential lineage assertions back to Raw, source document and run
```

The upload fixture is generated deterministically from:

```text
apps/api/tests/fixtures/catalogue_pipeline/e2e/hills_cis104_acceptance_page1.txt
```

That fixture text is written into a one-page PDF at test time so the source
submitted through the v2 API is an actual PDF that `pypdf` can read. The test
substitutes only the nondeterministic extraction provider output. The test
double must return evidence that is actually present on the uploaded PDF page;
source grounding fails if an extracted raw row or material field is changed
without changing the source fixture.

From the extraction adapter onward the test uses real contracts, persistence,
stage services, orchestration, review, application and publication services.

The fixture contains:

| Row | Path | Result |
|---|---|---|
| Hill's `10447`, `Hill's Healthy Cuisine Chicken 82g`, HKD `13.10`, `82g` | Valid | Reaches pending-review candidate, explicit approval, supplier commercial state and Serving publication. |
| Hill's `Q-1`, `Quoted Special Order Item`, `By Quote` cost | Invalid | Creates blocking `STAGING_COST_BASIS_UNRESOLVED`; no Mastering Candidate, approval, commercial state or Serving publication. |

## Lineage Proof

The acceptance test proves two forms of lineage:

| Lineage type | What is asserted |
|---|---|
| Relational lineage | `ServingItemV1.lineage` points to the Mastering Candidate, Staging Item and Raw Observation IDs; those records point back to the Source Document and Ingestion Run. |
| Evidential lineage | The uploaded file bytes match the persisted source file bytes; the cited page exists; each extracted `_raw_text` row and material extracted value occurs on that page after whitespace folding; served SKU, supplier SKU, product name, HKD currency, approved cost and packaging content are grounded in the Raw Observation text. |

The source object key used in the Raw Observation is a deterministic
page-scoped row key from the extraction adapter (`page:1:row:N`). It is not a
claimed bounding box, table cell, or coordinate. No bounding-box or cell
precision is fabricated for the PDF fixture.

## Requirement Matrix

| Requirement | Status | Implementation evidence | Test/runtime evidence | Gap/action |
|---|---|---|---|---|
| Authenticated catalogue upload creates an Ingestion Run. | PASS | `CatalogueSubmissionService.submit`, `/v2/catalogues/ingestions` | Acceptance test asserts `202`, one `CatalogueImport`, one `CatalogueSourceDocument`, one queued `IngestionRun`. | None. |
| Original source document is referenced. | PASS | `CatalogueSourceDocument`, source checksum/source ref, `IngestionRun.catalogue_source_document_id` | Acceptance test asserts persisted source, checksum, stored file and run/source contract agreement. | None. |
| Pipeline execution does not occur inline in submission. | PASS | Submission service has no Prefect/stage calls. | Acceptance test patches extraction/tagging to fail during POST and asserts no Raw/Staging/Candidate rows after submission. | None. |
| Submission idempotency and conflict behavior are verified. | PASS | `CatalogueSubmissionIdempotency` | Acceptance test replays same key/material and receives same run; changed material gets `409 IDEMPOTENCY_CONFLICT`. | None. |
| Raw extraction output is persisted. | PASS | `CatalogueRawObservation`, `RawObservationService.capture` | Acceptance test asserts two source-backed Raw Observations and reconstructs `RawObservationV1`. | None. |
| Raw data is not mutated during normalization/review/publication. | PASS | Raw and staging models are separate; raw rows have no review fields. | Acceptance test stores source-grounded raw text before review and asserts unchanged after approval/publication. | None. |
| Staging records are generated separately from Raw. | PASS | `CatalogueStagingItem`, `CatalogueStagingRawObservation` | Acceptance test asserts two Staging Items and raw-observation links. | None. |
| Staging records validate against the staging contract. | PASS | `staging_item_to_contract` reconstructs `StagingCatalogueItemV1`. | Acceptance test reconstructs staging contracts for valid and invalid rows. | None. |
| Validation occurs before mastering and cannot be bypassed. | PASS | `CatalogueValidationService.evaluate_staging`, `MasteringService.prepare_candidate` blocker checks. | Acceptance test asserts validation issue is created before invalid candidate preparation is rejected. | None. |
| Invalid records do not silently enter canonical/load persistence. | PASS | Blocking issue guard in mastering, approval/application, publication services. | Acceptance test asserts invalid row has no candidate and is absent from Serving. | None. |
| Invalid/ambiguous records produce Validation Issues or DLQ-equivalent rows. | PASS | `CatalogueValidationIssue` durable model. | Acceptance test asserts source-backed `STAGING_COST_BASIS_UNRESOLVED`, raw value, guidance, source/run lineage and publish-blocking flag. | None. |
| Valid records reach Mastering Candidates. | PASS | `MasteringService.prepare_candidate` | Acceptance test asserts one candidate in `PENDING_REVIEW`. | None. |
| Orchestration stops truthfully at human review. | PASS | Prefect flow stops after candidate preparation. | Acceptance test asserts no ReviewDecision, SupplierProduct or Serving row before explicit approval/application/publication. | None. |
| Approval is explicit, attributable and auditable. | PASS | `ReviewDecisionService.record_decision`, `CatalogueReviewDecision` | Acceptance test supplies `acceptance-reviewer@example.com`, reason, expected candidate revision and checks idempotent replay. | None. |
| Stale/concurrent review is rejected. | PASS | Stale candidate timestamp guard. | Acceptance test asserts `StaleCandidateRevision` for an old candidate timestamp. | None. |
| Valid records load into canonical or approved supplier-commercial representation. | PASS | `ApprovedCommercialStateService.apply_approved_candidate`, `CatalogueSupplierProduct`, `CatalogueSupplierPrice`, `CataloguePackagingConfiguration` | Acceptance test asserts Supplier Offer, canonical product link, HKD price, price basis and content measure. | Existing canonical `Product` must already exist; automatic Product Variant creation remains a later matching/mastering task. |
| Loaded records retain ingestion lineage. | PASS | Price, packaging, candidate and serving lineage columns. | Acceptance test asserts supplier price has `ingestion_run_uuid` and publication lineage points to candidate/staging/raw. | None. |
| Canonical records are retrievable through the serving layer. | PASS | `CatalogueServingPublication`, `serving_item_to_contract` | Acceptance test reconstructs `ServingItemV1` from current publication. | No public v2 serving HTTP endpoint is required by this acceptance proof. |
| Serving contains only approved data. | PASS | `ServingPublicationService.publish`, `ServingItemV1` review-status guard. | Acceptance test asserts publish before approval fails and invalid/pending rows are absent from serving lineage. | None. |
| Serving publication is explicit and idempotent. | PASS | `PublishServingItemCommand` stable publication key. | Acceptance test publishes after approval/application and replay reuses the same publication. | None. |
| Served fields are traceable to ingestion run/source. | PASS | `ServingItemV1.lineage`, raw/staging/candidate/source/run tables. | Acceptance test machine-asserts SKU, supplier SKU, name, cost, currency and packaging content lineage to staging, raw, page location, source document, run, and actual text in the uploaded PDF. | None. |
| Pipeline stages are independently identifiable. | PASS | Separate Raw, Staging, Validation Issue, Mastering Candidate, Review Decision, Supplier Offer/Price/Packaging and Serving tables; run metrics. | Acceptance test asserts row counts and status/metrics through status endpoint. | None. |
| Retry/replay/illegal-transition behavior is safe. | PASS | Submission idempotency, stage idempotency, terminal replay handling, run claim CAS. | Acceptance test asserts flow replay does not duplicate, duplicate approval/publication reuse, terminal run claim raises `TerminalRunReplay`. | None. |
| v1 compatibility remains intact. | PASS | v1 router and legacy import path unchanged. | Covered by existing regression suite; acceptance test does not alter v1. | Continue running full suite. |

## CIS-104 Child Task Evidence

| Child | Current acceptance evidence |
|---|---|
| CIS-104.1 Ingestion Run model | `IngestionRun` creates queued/running/terminal lifecycle and is exercised by submission, Prefect and acceptance tests. |
| CIS-104.2 Raw persistence | `CatalogueRawObservation` is populated only from source-located evidence present in the uploaded PDF and reconstructed as `RawObservationV1`. |
| CIS-104.3 Staging representation | `CatalogueStagingItem` stores raw/proposed values separately with raw-observation lineage. |
| CIS-104.4 Contract validation | Raw, Staging, Mastering Candidate, Validation Issue and Serving contracts are reconstructed and validated by mappers/tests. |
| CIS-104.5 Validation Issue / DLQ path | Invalid `By Quote` cost creates durable blocking `STAGING_COST_BASIS_UNRESOLVED` with guidance and lineage. |
| CIS-104.6 Canonical load with lineage | Explicitly approved candidate applies to supplier offer, price and packaging with review/source/run lineage. |
| CIS-104.7 Serving representation | Approved data publishes to `CatalogueServingPublication` and reconstructs `ServingItemV1`; invalid rows are absent. |

## Lifecycle Mapping

The run lifecycle is machine-ingestion oriented:

```text
queued -> running -> completed | completed_with_warnings | failed | cancelled
```

Business review is represented on `CatalogueMasteringCandidate.review_status`.
For CIS-104 acceptance, "pending review" means the Prefect machine run is
terminal and a candidate remains `PENDING_REVIEW`; it is not a separate run
status.

The acceptance test continues from that explicit review boundary through the
same review/application/publication services used by the run-scoped HITL API.

## Deferred Work

- HITL UI integration for the implemented run-scoped command API.
- Automatic Product Variant creation or matching beyond the current approved
  service boundary. The acceptance fixture uses an existing canonical SKU.
- Consumer cutover from legacy inventory reads to the run-scoped Serving
  retrieval endpoint.
- Wider supplier examples beyond the currently supported evidence-backed
  Hill's and Alfamedic runtime contracts.

## Remediation Verification

Source-backed remediation branch checks run:

```bash
cd apps/api
UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python -m pytest -q tests/test_catalogue_pipeline_e2e_acceptance.py
# 2 passed, 3 warnings

UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python -m pytest -q \
  tests/test_catalogue_pipeline_e2e_acceptance.py \
  tests/test_catalogue_submission_boundary.py \
  tests/test_catalogue_prefect_orchestration.py \
  tests/test_catalogue_pipeline_stage_services.py \
  tests/test_catalogue_pipeline_persistence.py \
  tests/test_catalogue_pipeline_contracts.py \
  tests/test_supplier_source_contracts.py \
  tests/test_supplier_source_contract_runtime.py \
  tests/test_api_versioning.py
# 121 passed, 3 warnings

UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python scripts/export_catalogue_pipeline_schemas.py --check
# passed

UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python scripts/export_supplier_source_contract_schemas.py --check
# passed

UV_CACHE_DIR=/tmp/uv-cache uv run --with-requirements requirements.txt --with pytest \
  python -m pytest -q
# 267 passed, 219 warnings

git diff --check
# passed
```

The committed negative source-grounding test mutates the extracted `cost_price`
to `99.99` while leaving the generated PDF unchanged and asserts the grounding
check fails with the expected `cost_price` failure.

A temporary local mutation of the deterministic extraction row from `13.10` to
`99.99` produced the expected failure:

```text
AssertionError: row 1 cost_price not found on cited source page: '99.99'
```

No repository Markdown validation command is configured; the touched Markdown
file was included in `git diff --check` and a trailing-whitespace scan.
