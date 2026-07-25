# "Staging Item" → "Interpreted Claim" → "Normalized Row" — RESOLVED

Status: resolved 2026-07-24; layer semantics corrected 2026-07-25

This artifact has been renamed twice as its role clarified. It is now the
**Normalized Row**: the Staging layer's deterministic, contract-conformed
output. The supplier source contract maps each extracted-evidence cell (and
`composed_from` joins and constants) to a business field with NO model — so the
record belongs to Staging (steps 3-4), alongside the verbatim extracted
evidence.

The earlier "Interpreted Claim / Intermediate layer" framing was **superseded**
when contract conformance was made deterministic and moved into Staging. The
free-text model interpretation step was removed entirely. The Intermediate
layer is now purely business interpretation (validation + mastering
candidates), which consumes the normalized rows.

## Final names

| Kind | Original | Current |
|---|---|---|
| Physical table | `catalogue_staging_items` | `catalogue_normalized_rows` |
| Evidence link table | `catalogue_staging_item_raw_observations` | `catalogue_normalized_row_evidence` |
| Contract ID | `catalogue.staging_item.v1` | `catalogue.normalized_row.v1` |
| Model class | `CatalogueStagingItem` | `CatalogueNormalizedRow` |
| Contract class | `StagingCatalogueItemV1` | `NormalizedRowV1` |
| Typed fields | `proposed_fields` (model proposals) | `normalized_fields` (deterministic) |
| Service | `StagingCatalogueService` | `NormalizedRowService` |
| Persistence fns | `persist_staging_item`, `staging_item_to_contract` | `persist_normalized_row`, `normalized_row_to_contract` |

Schema is authoritative via `create_all` (no migration history — the production
database is recreated fresh; superseded tables are dropped, not migrated).

## Deliberate residuals (serialized field names — kept)

The row's own identity field stays `catalogue_item_id` / `catalogue_item_uuid`
and its evidence-lineage field stays `raw_observation_ids` — pipeline-wide
serialized names whose rename is a field-level contract redesign (a
contract-version bump), out of scope for a naming pass. See
`rename-raw-observation-to-extracted-evidence.md`.
