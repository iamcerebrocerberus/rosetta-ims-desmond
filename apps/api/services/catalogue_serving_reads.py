"""Consumer-safe reads over immutable catalogue serving publications."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
from schemas.catalogue_pipeline import ServingItemV1
from services import catalogue_pipeline_persistence as persistence


@dataclass(frozen=True)
class ServingPublicationPage:
    total: int
    offset: int
    limit: int
    items: tuple[ServingItemV1, ...]


@dataclass(frozen=True)
class ServingPublicationHistoryItem:
    is_current: bool
    superseded_at: str | None
    snapshot: ServingItemV1


class CatalogueServingReadService:
    """Read only approved snapshots; never reconstruct from mutable state."""

    def __init__(self, db: Session):
        self.db = db

    def list_current(
        self,
        *,
        search: str | None = None,
        supplier_id: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> ServingPublicationPage:
        query = self.db.query(models.CatalogueServingPublication).filter_by(is_current=1)
        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    models.CatalogueServingPublication.canonical_sku.ilike(term),
                    models.CatalogueServingPublication.product_variant_name.ilike(term),
                    models.CatalogueServingPublication.supplier_sku.ilike(term),
                )
            )
        if supplier_id is not None:
            query = query.filter_by(supplier_id=supplier_id)
        total = query.count()
        rows = (
            query.order_by(
                models.CatalogueServingPublication.canonical_sku,
                models.CatalogueServingPublication.supplier_id,
                models.CatalogueServingPublication.id,
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return ServingPublicationPage(
            total=total,
            offset=offset,
            limit=limit,
            items=tuple(persistence.serving_item_to_contract(row) for row in rows),
        )

    def history_for_sku(self, canonical_sku: str) -> tuple[ServingPublicationHistoryItem, ...]:
        rows = (
            self.db.query(models.CatalogueServingPublication)
            .filter_by(canonical_sku=canonical_sku)
            .order_by(
                models.CatalogueServingPublication.supplier_id,
                models.CatalogueServingPublication.id.desc(),
            )
            .all()
        )
        return tuple(
            ServingPublicationHistoryItem(
                is_current=bool(row.is_current),
                superseded_at=row.superseded_at,
                snapshot=persistence.serving_item_to_contract(row),
            )
            for row in rows
        )
