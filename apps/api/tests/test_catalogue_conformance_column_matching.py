"""Conformance column matching is robust to real OCR header variance.

Regression from a live Gemini vision smoke of the Hill's price list: the vision
provider labels bilingual columns with a SPACE (not the contract's " / ") and
renders the CJK side differently from the contract text — e.g. it returns
"Gross Wholesale Price 折扣前批發價（每包／罐）" where the contract declares
"Gross Wholesale Price / 每箱·罐". Matching must tolerate both (separator
insensitivity + English-portion fallback) or every row is unconformable.
"""

from __future__ import annotations

import os
import tempfile
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
os.environ.setdefault("PREFECT_API_MODE", "offline")

from services import supplier_source_contract_runtime as runtime  # noqa: E402
from services.catalogue_conformance import conform_observations  # noqa: E402
from services.catalogue_evidence_extraction import ExtractedEvidence  # noqa: E402
from schemas.catalogue_pipeline.enums import ExtractionMethod  # noqa: E402
from schemas.catalogue_pipeline.extracted_evidence_v1 import RawCell, SourceLocation  # noqa: E402


def _observation(cells: dict[str, str]) -> ExtractedEvidence:
    return ExtractedEvidence(
        observation_key="row-1",
        source_location=SourceLocation(row_number=1, source_object_key="row-1"),
        raw_cells=tuple(
            RawCell(cell_reference=None, row_number=1, column_index=index + 1, column_name=column, raw_value=value)
            for index, (column, value) in enumerate(cells.items())
        ),
        extraction_method=ExtractionMethod.MODEL_VISION,
        provider="test",
    )


def test_real_gemini_bilingual_headers_map_through_the_contract():
    hills = runtime.load_contract(14)
    # Column labels EXACTLY as gemini-flash returned them for the Hill's page:
    # space separators, and a cost column whose CJK diverges from the contract.
    gemini_row = {
        "Product Code 產品編號": "10447",
        "Product Range 產品系列": "Science Plan",
        "Life Stage 生命階段": "Adult",
        "Product Description 產品名稱": "Chicken 82g",
        "Size 重量": "82g",
        "Gross Wholesale Price 折扣前批發價（每包／罐）": "13.10",
        "Order Multiple 訂貨單位": "12",
        # Extra columns Gemini emits that the contract does not declare — ignored.
        "Regular Retail Price 正價": "19.00",
    }
    outcome = conform_observations((_observation(gemini_row),), (uuid4(),), hills)

    assert len(outcome.items) == 1
    fields = outcome.items[0].normalized_fields
    assert fields["supplier_sku"]["value"] == "10447"
    assert fields["product_name"]["value"] == "Hill's - Chicken 82g - Adult - Science Plan"
    assert fields["brand"]["value"] == "Hill's"  # contract constant
    # Cost mapped despite the CJK side differing from the contract text.
    assert fields["cost"]["amount"] == "13.10"
    assert fields["cost"]["currency"] == "HKD"


def test_header_row_of_gemini_labels_is_skipped():
    hills = runtime.load_contract(14)
    header = {c: c for c in (
        "Product Code 產品編號", "Product Range 產品系列", "Life Stage 生命階段",
        "Product Description 產品名稱", "Size 重量", "Gross Wholesale Price 折扣前批發價",
        "Order Multiple 訂貨單位",
    )}
    outcome = conform_observations((_observation(header),), (uuid4(),), hills)
    assert outcome.items == ()
    assert outcome.skipped_count == 1


def test_bilingual_cell_values_compose_a_clean_english_product_name():
    hills = runtime.load_contract(14)
    # Bilingual VALUES exactly as Gemini vision returned them for a Hill's row.
    gemini_row = {
        "Product Code 產品編號": "10445",
        "Product Range 產品系列": "健康燉肉 Healthy Cuisine",
        "Life Stage 生命階段": "幼貓 Kitten",
        "Product Description 產品名稱": "健康燉肉配方 Healthy Cuisine",
        "Size 重量": "82g",
        "Gross Wholesale Price 折扣前批發價（每包／罐）": "13.10",
    }
    row = conform_observations((_observation(gemini_row),), (uuid4(),), hills).items[0]
    # raw_fields keeps the verbatim bilingual join; normalized is the clean name:
    # English-only, de-duplicated (the range repeats in the description), " - "-joined.
    assert row.raw_fields["product_name"] == "健康燉肉 Healthy Cuisine 幼貓 Kitten 健康燉肉配方 Healthy Cuisine"
    assert row.normalized_fields["product_name"]["value"] == "Hill's - Healthy Cuisine - Kitten - 82g"
