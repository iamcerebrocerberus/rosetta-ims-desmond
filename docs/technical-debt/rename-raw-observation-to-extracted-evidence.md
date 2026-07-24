# Rename "Raw Observation" to "Extracted Evidence" — RESOLVED

Status: resolved 2026-07-24

The timeline-consistent rename is complete. "Raw Observation" was the
extraction stage's output (step 4, Staging layer), never the file-only raw
stage (steps 1-2) — that confusion is now removed at every level someone reads.

## What was renamed

| Kind | Old | New |
|---|---|---|
| Physical table | `catalogue_raw_observations` | `catalogue_extracted_evidence` |
| Link table | `catalogue_staging_raw_observations` | `catalogue_interpreted_claim_evidence` |
| Contract ID | `catalogue.raw_observation.v1` | `catalogue.extracted_evidence.v1` |
| Model class | `CatalogueRawObservation` | `CatalogueExtractedEvidence` |
| Contract class | `RawObservationV1` | `ExtractedEvidenceV1` |
| Service | `RawObservationService` | `ExtractedEvidenceService` |
| Command | `CaptureRawObservationsCommand` / `RawObservationInput` | `CaptureExtractedEvidenceCommand` / `ExtractedEvidenceInput` |
| Task | `capture-raw-observations` | `capture-extracted-evidence` |
| Persistence fns | `persist_raw_observation`, `raw_observation_to_contract` | `persist_extracted_evidence`, `extracted_evidence_to_contract` |

Existing databases migrate in place via `database.run_pre_create_renames`
(runs before `create_all`; data-preserving `ALTER TABLE ... RENAME`, stored
`contract_version` values updated, idempotent).

## Deliberate residuals (serialized field names — kept)

Serialized **contract field names** stay `raw_observation_id` / `raw_observation_ids`
and DB **column names** stay `raw_observation_uuid`, `catalogue_item_uuid`, etc.
Renaming those changes the JSON wire contract and every persisted row's keys —
a field-level contract redesign, out of scope for a naming pass. Some
constraint/index names on migrated tables also keep their historical names
(cosmetic; fresh DBs get the new-derived names). Revisit only alongside a
contract-version bump.
