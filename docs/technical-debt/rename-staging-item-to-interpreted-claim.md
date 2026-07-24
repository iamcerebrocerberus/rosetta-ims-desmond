# Rename "Staging Item" to "Interpreted Claim"

Status: follow-up, not urgent

`CatalogueStagingItem` (and `catalogue.staging_item.v1`, `StagingCatalogueService`,
`build-staging-items` task) are timeline **step 6 — Intermediate layer interpreted
claims**: the supplier contract has already been applied. The actual Staging layer
(steps 3-4) produces extracted evidence observations.

Docstrings now state the mapping; a rename would touch the persisted table, the
versioned JSON contract, services, tasks and many tests, so it waits for the same
window as the `raw_observation` rename (see
rename-raw-observation-to-extracted-evidence.md) — introduce
`catalogue.interpreted_claim.v1` as a successor contract, migrate the table via
Alembic once adopted, and rename symbols in one change.
