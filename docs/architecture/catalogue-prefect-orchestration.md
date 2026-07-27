# Catalogue Prefect Orchestration

Status: implementation foundation  
Date: 2026-07-23

This document defines the Prefect orchestration boundary for v2 catalogue
ingestion. The v2 submission endpoint durably creates a queued `IngestionRun`;
this orchestration layer claims that queued run and coordinates the
machine-executable stages through Extracted Evidence, Interpreted Claim, Validation Issue and
pending-review Mastering Candidate creation.

The flow does not approve, apply supplier commercial state, publish Serving
Items, replace the v1 synchronous import path, or make Prefect responsible for
domain semantics.

## Prerequisites Rechecked

The implementation builds on the current persistence and service foundations:

| Foundation | Current implementation | Use in orchestration |
|---|---|---|
| Submission boundary | `CatalogueSubmissionService` and `/catalogues/ingestions` create durable source documents and queued runs. | The flow accepts only the stable run UUID returned by submission. |
| Source asset persistence | `CatalogueSourceDocument` stores source UUIDs, checksum, source ref, supplier, contract ID/version, document type and source format. | The source loader verifies path safety, file signature and SHA-256 before extraction. |
| Run lifecycle | `IngestionRun` supports `queued`, `running`, `completed`, `completed_with_warnings`, `failed` and `cancelled`; `started_at` is nullable for queued runs. | The lifecycle service atomically claims queued runs and writes truthful terminal state. |
| Supplier-source contracts | `supplier_source_contract_runtime.resolve_supplier_contract` rejects unknown, ambiguous and unsupported contracts. | Orchestration resolves the exact recorded ID/version only. |
| Stage services | `ExtractedEvidenceService`, `InterpretedClaimService`, `CatalogueValidationService` and `MasteringService` are framework-neutral. | Prefect tasks call these services without importing FastAPI or leaking ORM objects between tasks. |
| v1 import removed | `POST /catalogues/import` returns a 410 tombstone pointing at `/catalogues/ingestions`. | The queued Prefect path is the only upload flow. |

## Prefect Dependency

`prefect==3.3.7` is pinned in `apps/api/requirements.txt`.

This version is compatible with the repository's current Pydantic 2.9.x pin and
Python 3.11 Docker target while still supporting Python 3.13 in local tests.
Newer Prefect 3.4.x releases require a newer Pydantic range, so they are not
used until the backend dependency set is intentionally upgraded.

Prefect imports are confined to `apps/api/orchestration/`. Pydantic contracts,
supplier-source contracts, SQLAlchemy models, mappers, stage services and
FastAPI routers do not import Prefect.

## Flow and Task Topology

Primary flow:

```python
catalogue_ingestion_flow(*, ingestion_run_id: UUID) -> CatalogueFlowResult
```

The flow input is the public run UUID. It reloads the run, source document and
recorded supplier contract from persistence on execution. It never accepts raw
file paths, uploaded bytes, unvalidated supplier IDs, contract dictionaries or
catalogue business data as flow parameters.

Task sequence:

| Task | Responsibility | Retry policy |
|---|---|---|
| `load-and-claim-catalogue-run` | Atomically move `queued` to `running`. | No retry. Duplicate claim is a typed result. |
| `complete-raw-stage` | File-only raw stage: verify the stored original (existence, size, path safety, signature, checksum), reject password-protected PDFs, record byte size / structural page count / durable completed-or-failed marker, and return a typed `RawStageResult` with identifiers and file metadata but no content. No AI, OCR, parsing or extraction is reachable here. | No retry for deterministic source errors. |
| `resolve-recorded-supplier-contract` | Resolve exact supplier contract ID/version recorded on the run. | No retry. |
| `extract-source-evidence` | Extraction stage: reload the stored original through its durable `source_ref` (re-verified), then produce verbatim, source-located observations (no semantic parsing). File bytes never cross a task boundary. | Two retries only for transient provider/network failures. |
| `capture-extracted-evidence` | Persist `ExtractedEvidenceV1` records (extraction-stage output — distinct from the file-only raw stage) through the stage service. | No retry; idempotency handles replay. |
| `interpret-raw-evidence` | Propose typed fields from persisted evidence using the recorded contract. Non-catalogue lines (titles, headers) are skipped from claim persistence; provider outages degrade to unproposed fields routed to review. | Two retries only for transient provider failures. |
| `build-interpreted-claims` | Persist `InterpretedClaimV1` records linked to extracted evidence. | No retry; idempotency handles replay. |
| `evaluate-interpreted-claims` | Persist durable validation issues. | No retry; issue keys deduplicate. |
| `prepare-pending-review-candidates` | Create pending-review mastering candidates for unblocked interpreted claims. | No retry; blocking issues are expected business outcomes. |
| `finalize-catalogue-run` | Write terminal success or warning state. | One DB retry. |
| `record-catalogue-run-failure` | Write sanitized failure state in a fresh transaction. | One DB retry. |

No task passes a live SQLAlchemy `Session`, file handle or ORM instance across
Prefect task boundaries. Task payloads are dataclasses, UUIDs, strings, counts
and validated dictionaries.

## Sequence

```mermaid
sequenceDiagram
    participant Dispatch as Dispatcher
    participant Flow as catalogue_ingestion_flow
    participant DB as Persistence
    participant Store as Source storage
    participant Extract as Evidence adapter
    participant Stage as Stage services
    participant HITL as Human review later

    Dispatch->>DB: scan bounded queued runs
    Dispatch->>Flow: run UUID
    Flow->>DB: claim queued -> running
    Flow->>Store: RAW STAGE - verify stored original (size, signature, checksum, encryption)
    Flow->>DB: persist raw completion (byte size, page count, status) - no content
    Flow->>DB: resolve recorded supplier contract
    Flow->>Extract: EXTRACTION STAGE - reload original via durable source_ref
    Flow->>Extract: extract verbatim source evidence
    Extract-->>Flow: typed, source-located observations
    Flow->>Stage: capture extracted evidence
    Flow->>Extract: interpret persisted evidence (contract-guided)
    Extract-->>Flow: staged-row proposals with per-field evidence
    Flow->>Stage: build interpreted claims
    Flow->>Stage: evaluate validation issues
    Flow->>Stage: prepare pending-review candidates
    Flow->>DB: complete or complete_with_warnings
    Flow-->>HITL: stop before review, approval, application or publication
```

## Source Loading and Checksum Verification

`load_and_verify_source_asset` is the authoritative source loader for
orchestration.

It:

- loads the run by UUID and its `CatalogueSourceDocument`;
- resolves `source_ref` beneath `CATALOGUE_UPLOAD_DIR`;
- rejects blank, absolute, traversal or root-escaping source references;
- reads in bounded chunks;
- enforces `CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES`, default `25 MB`;
- recomputes SHA-256 and compares it with the persisted source checksum;
- checks source signatures for PDF/PDF table, spreadsheet and CSV formats;
- returns a typed source asset without exposing absolute paths.

Missing, altered, oversized, unreadable or signature-mismatched files fail the
run with sanitized operational errors. The flow never continues with unverified
bytes.

## Exact Supplier Contract Resolution

The flow resolves the supplier-source contract using only the identity persisted
on the run and source document:

- supplier ID;
- supplier-source contract ID;
- supplier-source contract version;
- document type;
- source format.

There is no supplier-only fallback in orchestration. Unknown versions,
cross-supplier mismatches, document-type mismatches, source-format mismatches,
`PARTIALLY_VERIFIED`, `UNVERIFIED` and deprecated contracts fail safely.

Runtime-supported source contracts remain:

| Supplier | Contract | Version | Source format | Runtime status |
|---|---|---:|---|---|
| Alfamedic | `alfamedic.price_list.v1` | `v1` | PDF table | Supported |
| Hill's | `hills.price_list.v1` | `v1` | PDF table | Supported |
| Vetapet | `vetapet.vet_price_list.v1` and related declarations | `v1` | PDF table | Not runtime selectable |
| KPN/Kangaroo | Kangaroo declarations | `v1` | PDF table | Not runtime selectable; technical debt |

No supplier contract was promoted by this orchestration task.

## Staging-Layer Hardening (Stage 3 / Stage 4)

**PDF page extraction policy.** Each page is classified document-level (never
semantically) by text quality AND meaningful-text coverage plus structural
image presence: no/garbled text → vision; reliable text with ≥3 meaningful
lines → text-only; reliable but sparse text with page images → hybrid (text +
vision); reliable sparse text with no images → text-only. Noise lines (bare
page numbers, footers, rulers, dates) do not count toward coverage, so a
watermark or page number can never mark an image-bearing page complete. Hybrid
pages keep BOTH evidence sets — no deduplication at this layer; legitimate
repeated supplier rows survive verbatim.

**Vision provider outcome contract.** The provider returns a typed page
envelope with a required `page_outcome` of `evidence` or
`no_catalogue_evidence`. An empty observation array is accepted ONLY with an
explicit `no_catalogue_evidence` classification (counted in `empty_units`,
never as a fake observation); an empty array without classification, or
`evidence` with no observations, is a malformed-provider error carrying the
page/unit key. This prevents provider truncation from masquerading as an empty
page.

**Completeness accounting.** A unit (PDF page, XLSX sheet, CSV parse, image)
counts as completed only when fully observed or explicitly classified empty.
No observations + no explicit empty classification + no errors is `NO_EVIDENCE`
(failed), never `COMPLETE`. Any unit error downgrades the result to `PARTIAL`.

**Provider seam + retry classification.** `_call_gemini_vision` is the only
place the vision provider client (google-genai) is instantiated (imported
lazily, function-level). Failures are classified by HTTP status on the SDK
error: 408/409/429 and 5xx → retryable; 401/403 → non-retryable configuration
error; other 4xx → non-retryable provider error; network-level errors fall
back to a message heuristic. Raw provider text is never persisted or returned.
Interpretation (step 5) is a separate Anthropic call — see below.

**Observation identity.** Vision observation keys are `page:<n>:obs:<digest>:<ordinal>`
where digest = sha256(raw_text + raw_cells + bounding_box). Identical evidence
keeps identical identity across reordered provider retries; duplicate rows get
distinct ordinals; materially changed evidence yields a different digest and
surfaces as a controlled `IdempotencyConflict` at capture rather than
corrupting persisted evidence. Stage 4 capture is an atomic batch (commit only
after all observations are staged); a mid-batch failure leaves nothing
committed.

**Run failure lifecycle.** Once a run is claimed, every terminal failure in
Stage 3/4 moves it to `failed` with a sanitized code: `CatalogueOrchestrationError`
and `CatalogueStageError` map to stable codes; anything unexpected maps to
`INTERNAL_PIPELINE_ERROR` with full diagnostics logged internally only. A
run can never remain `running`. A failure-recording failure is logged and
never masks the original error; retries still occur only for typed transient
provider errors.

## Typed Evidence Extraction, then Post-Raw Interpretation

Extraction and interpretation are now separate pipeline boundaries. The legacy
combined extract-and-interpret path (`services.extraction_service.extract`)
serves only v1 endpoints and is not reachable from orchestration.

**Extraction** (`services.catalogue_evidence_extraction` behind the
`catalogue_extraction_adapter` policy) is contract-independent and records only
what the source contains and where: spreadsheet cells, CSV rows, PDF text lines
(with a vision fallback for scanned pages), each as a typed
`ExtractedEvidence` observation with a stable observation key, source location,
provider metadata and optional Decimal confidence. `FAILED` results fail the
run (retryable provider errors retry); `PARTIAL` results carry per-unit errors
into run warnings with rejected-unit accounting. Every observation is persisted
as an Extracted Evidence observation — including titles and column headers, which are evidence
even when they are not items.

**Interpretation** (`services.catalogue_interpretation`) runs after Raw
persistence and is the only place evidence becomes proposals:

- cell-backed observations map deterministically through the contract's
  declared source columns; rows repeating the declared headers are recognized
  as header rows and skipped from claim persistence;
- text-backed observations are interpreted by the model provider against the
  contract's prompt section, keyed by observation, with verdicts: a fields
  object, or null for non-catalogue lines (titles, banners, footers);
- every proposal carries `FieldEvidence` pointing at its supporting Raw
  Observation;
- a missing or failed provider degrades safely: observations stage with raw
  strings preserved and no proposals, routing to human review. Nothing is
  invented and the run does not fail.

Interpretation does not fabricate price basis, packaging quantities,
sellable-unit counts or MBB semantics; ambiguous source text stays verbatim in
`StagingRawFields` and unresolved semantics surface as validation issues.

## Raw to Staging to Validation to Candidate

The adapter converts source evidence into existing stage service commands:

| Stage | Adapter behavior |
|---|---|
| Extracted Evidence | Preserves raw text/cells, source location, extraction method, model metadata and confidence. Deterministic idempotency key is run scoped by the stage service. |
| Interpreted Claim | Keeps printed source strings in `ClaimRawFields` and typed interpretations in `ProposedCatalogueFields`. |
| Validation Issue | Calls `CatalogueValidationService.evaluate_staging`; durable issue rows are created or reused. |
| Mastering Candidate | Calls `MasteringService.prepare_candidate` only for staging items with no open blocking issue. Candidates stay `PENDING_REVIEW`. |

Ambiguous operational semantics stay unresolved. A raw cost like `By Quote`
creates a blocking validation issue and prevents candidate preparation. Raw MBB
text is kept as evidence unless a typed condition and benefit can be proved.

## Human Review Stop Point

The automatic boundary stops after pending-review candidate creation. It does
not:

- resolve validation issues;
- invent reviewer identities;
- record approval or rejection;
- apply supplier commercial state;
- create or supersede supplier prices, packaging or MBB terms;
- publish Serving Items;
- treat extraction confidence as approval.

`completed` means the machine ingestion attempt produced truthful staging and
candidate output without operational warnings. `completed_with_warnings` means
rows were rejected, blocking validation issues remain, durable warnings were
created, or other non-fatal issues need attention. Business approval remains a
separate human-review workflow.

## Run Lifecycle and Concurrency

`catalogue_run_lifecycle.py` owns orchestration transitions:

```text
queued -> running
running -> completed
running -> completed_with_warnings
running -> failed
queued/running -> cancelled
```

Starting a run uses an atomic database compare-and-set:

```sql
UPDATE catalogue_ingestion_runs
SET status = 'running', started_at = :now, completed_at = NULL
WHERE run_uuid = :run_uuid AND status = 'queued'
```

Only one worker can claim a queued run. A second worker receives a typed
duplicate-claim result, and terminal replay returns the existing terminal
metrics. Database constraints and stage-service idempotency remain the final
guard against duplicate raw, staging, issue and candidate rows.

Failures are recorded in a fresh transaction where possible. Failure summaries
are sanitized and exclude stack traces, credentials, absolute storage paths,
SQL text and catalogue contents.

Stale `running` recovery is intentionally documented rather than automatically
implemented in this task. An operator can inspect the run and either rerun after
resetting it to `queued` through a controlled maintenance action or mark it
`failed`/`cancelled` with a recorded reason. A future recovery command can add a
time-based stale-running policy.

## Dispatch and Reconciliation

The repository uses a scheduled/looping reconciler pattern. The dispatcher:

- queries a bounded, deterministic batch of queued runs;
- invokes `catalogue_ingestion_flow` with each run UUID;
- avoids terminal runs;
- relies on the atomic claim for duplicate-dispatch safety;
- can run as a one-shot command or as a long-running worker loop.

Commands:

```bash
cd apps/api
python -m orchestration.catalogue_dispatch --batch-size 10
python -m orchestration.catalogue_dispatch --loop --interval-seconds 30 --batch-size 10
```

The current Docker Compose file includes a `prefect-server` service and a
catalogue worker profile. The Prefect UI/API runs from the same API image and
stores local Prefect state in the `prefect_data` Docker volume. The UI is bound
to localhost by default:

```bash
cd apps/api
docker compose up -d --build prefect-server
# UI: http://localhost:4200
```

The worker profile starts the queued-run dispatcher loop and points it at the
container-local Prefect API:

```bash
docker compose --profile catalogue-worker up -d catalogue-worker
```

This worker runs the dispatcher loop from the same API image and shares the
same data volume for source files. `PREFECT_UI_BIND` defaults to `127.0.0.1` so
the unauthenticated Prefect UI is not exposed publicly by accident. Use an SSH
tunnel, VPN, private network, or authenticated reverse proxy before binding it
to `0.0.0.0`.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `CATALOGUE_UPLOAD_DIR` | `/data/catalogue_uploads` | Root used by submission and orchestration source loading. |
| `CATALOGUE_ORCHESTRATION_MAX_SOURCE_BYTES` | `26214400` | Max verified source size for orchestration reads. |
| `CATALOGUE_DISPATCH_BATCH_SIZE` | `10` | Docker worker bounded queued-run batch size. |
| `CATALOGUE_DISPATCH_INTERVAL_SECONDS` | `30` | Docker worker loop sleep between dispatch scans. |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | unset | Vision/OCR fallback for scanned/image pages (missing key fails those pages as configuration errors). Text-layer PDFs, spreadsheets and CSV need no key. |
| `ANTHROPIC_API_KEY` | unset | Post-Raw interpretation (step 5); missing key degrades to unproposed fields routed to review. Also legacy v1 extraction/tagging. |
| `PREFECT_API_URL` | `http://prefect-server:4200/api` in Docker | Prefect API URL used by the catalogue worker. Tests and local one-shot flows can still run with Prefect's local/ephemeral mode. |
| `PREFECT_UI_BIND` | `127.0.0.1` | Host interface for the Prefect UI port. |
| `PREFECT_UI_PORT` | `4200` | Host port for the Prefect UI. |
| `PREFECT_UI_API_URL` | `http://localhost:4200/api` | Browser-visible Prefect API URL used by the UI. |

## Retry Classification

Retries are bounded and narrow:

- retry: transient extraction provider/network/rate-limit style failures;
- retry: transient DB failures on finalize/failure-record tasks;
- no retry: unsupported contracts, contract mismatch, checksum mismatch,
  malformed source, missing evidence, deterministic Pydantic failures, invalid
  lifecycle transitions, ambiguous packaging/cost/MBB semantics.

Warnings and validation issues are business outcomes, not infrastructure
failures.

## SQLite and PostgreSQL Notes

SQLite remains supported for local development and the current deployment. The
claim query uses a single conditional `UPDATE`, which SQLite executes
atomically for one database file. PostgreSQL remains compatible with the same
compare-and-set pattern and can later add stronger worker visibility using row
locks if needed.

The worker and API must share the configured upload root. For SQLite, only one
database file should be used by both containers. For PostgreSQL, both processes
must point to the same `DATABASE_URL`.

## Existing Compatibility

- `POST /catalogues/import` is removed (410); the legacy review queue still serves previously imported items.
- `/catalogues/ingestions` still returns after durable submission only.
- The v2 status endpoint reads the run state written by orchestration.
- Stage services stay framework-neutral.
- Public inventory reads are not cut over to serving publications here.

## Deferred Work

- HITL endpoints and UI for validation issues and mastering candidates.
- Review-decision orchestration after human action.
- Applying approved candidates to supplier commercial state.
- Serving publication and public read cutover.
- Explicit stale-running recovery command.
- Rich Prefect deployment manifests if a managed/self-hosted Prefect server is
  adopted.
- Spreadsheet/CSV orchestration support when those supplier formats become
  runtime-supported with row-level evidence.
- KPN/Kangaroo supplier-source contract completion after supplier ownership and
  row-evidence debt is resolved.
