"""Unified SQLAlchemy models for the Rosetta IMS backend.

One registry, two families:

- ``models.legacy`` — the long-lived inventory/ops domain (users, auth, audit,
  products, suppliers, stock, pricing, tags, reparse, legacy catalogue import).
- catalogue pipeline modules — the evidence-first ingestion pipeline
  (source documents, ingestion runs, extracted evidence observations, staging, validation,
  mastering, serving) that superseded the legacy synchronous catalogue flow.

Every model is re-exported here so call sites use one import surface
(``import models``; ``models.Product``, ``models.IngestionRun``, …) and all
tables register on the single shared ``Base.metadata``.
"""

from models.legacy import *  # noqa: F401,F403 — legacy domain + Base
from models.ingestion_run import (  # noqa: F401
    IngestionRun,
    IngestionRunMetrics,
    IngestionRunStatus,
)
from models.catalogue_submission import CatalogueSubmissionIdempotency  # noqa: F401
from models.catalogue_pipeline import (  # noqa: F401
    CatalogueMasteringCandidate,
    CatalogueExtractionAttempt,
    CataloguePackagingConfiguration,
    CatalogueProductFamily,
    CatalogueExtractedEvidence,
    CatalogueRawStageAttempt,
    CatalogueReviewDecision,
    CatalogueServingPublication,
    CatalogueSourceDocument,
    CatalogueNormalizedRow,
    CatalogueNormalizedRowEvidence,
    CatalogueSupplierMbbTerm,
    CatalogueSupplierPrice,
    CatalogueSupplierProduct,
    CatalogueValidationIssue,
)
