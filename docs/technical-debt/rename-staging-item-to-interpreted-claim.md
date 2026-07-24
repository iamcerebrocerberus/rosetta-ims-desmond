# Rename "Staging Item" to "Interpreted Claim" — RESOLVED

Status: resolved 2026-07-24

The timeline-consistent rename is complete. "Staging Item" was really the
Intermediate layer's interpreted claim (step 6) — the supplier contract has
already been applied by interpretation (step 5). It was never the Staging
layer (steps 3-4, extracted evidence). That confusion is now removed.

## What was renamed

| Kind | Old | New |
|---|---|---|
| Physical table | `catalogue_staging_items` | `catalogue_interpreted_claims` |
| Contract ID | `catalogue.staging_item.v1` | `catalogue.interpreted_claim.v1` |
| Model class | `CatalogueStagingItem` | `CatalogueInterpretedClaim` |
| Contract class | `StagingCatalogueItemV1` | `InterpretedClaimV1` |
| Sub-model | `StagingRawFields` | `ClaimRawFields` |
| Service | `StagingCatalogueService` | `InterpretedClaimService` |
| Command | `BuildStagingItemCommand` / `EvaluateStagingCommand` | `BuildInterpretedClaimCommand` / `EvaluateInterpretedClaimCommand` |
| Task | `build-staging-items` / `evaluate-staging-items` | `build-interpreted-claims` / `evaluate-interpreted-claims` |
| Persistence fns | `persist_staging_item`, `staging_item_to_contract` | `persist_interpreted_claim`, `interpreted_claim_to_contract` |

Existing databases migrate in place via `database.run_pre_create_renames`
(data-preserving rename before `create_all`, idempotent).

## Deliberate residuals (serialized field names — kept)

The claim's own identity field stays `catalogue_item_id` / `catalogue_item_uuid`
and its evidence-lineage field stays `raw_observation_ids` — serialized contract
field names whose rename is a field-level contract redesign (a contract-version
bump), out of scope for a naming pass. See
`rename-raw-observation-to-extracted-evidence.md`.
