# Catalogue pipeline layer contract

This document fixes the vocabulary and ownership boundaries for the new
`POST /catalogues/ingestions` pipeline.

| Layer | Operations | Durable outputs |
| --- | --- | --- |
| Raw | Accept the submission; preserve the original bytes; verify file integrity and file-level metadata; record append-only attempts | Source document, ingestion run, raw-stage attempts |
| Staging | Extract verbatim, source-located text/cells from the stored original; persist extraction evidence | Extraction attempts and extracted evidence |
| Intermediate | Execute the supplier contract; produce normalized claims; validate; resolve supplier offerings and canonical products; collect human corrections and decisions | Normalized claims, validation issues, mastering candidates/revisions, review decisions |
| Serving | Apply approved commercial state and publish business-ready snapshots | Supplier offerings, approved prices and packaging, publication snapshots |

## Non-negotiable boundaries

- Raw never imports, initializes, calls, or dispatches an AI/OCR/extraction
  provider.
- Extraction starts only after Raw completes and consumes the durable stored
  source reference, not the request upload stream.
- Extracted evidence is Staging data, not Raw data.
- Applying a supplier contract changes evidence into an interpreted proposal
  and therefore starts Intermediate.
- Supplier SKU is supplier-scoped and is never automatically a canonical
  Rosetta SKU.
- Serving consumes only explicitly approved Intermediate state.

## Current delivery status

Submission and Raw are the stable baseline. Later layers remain under active
implementation. Read endpoints must not imply that incomplete downstream
behavior is approved for publication.
