"""Hill's supplier-source contract declarations."""

from __future__ import annotations

from schemas.catalogue_pipeline.common import UnitOfMeasure
from schemas.catalogue_pipeline.enums import IssueSeverity, SourceFormat, UnitCode
from schemas.catalogue_pipeline.supplier_contracts.common import (
    SUPPLIER_SOURCE_SCHEMA_VERSION,
    AmbiguityRule,
    PackagingSourceSemantics,
    PricingSourceSemantics,
    SemanticResolutionStatus,
    SourceFieldContract,
    SourceFieldRequirement,
    SourceFieldRole,
    SourceStructure,
    SourceTableRegion,
    SupplierContractSupportStatus,
    SupplierDocumentType,
    SupplierSourceContractV1,
    SupplierSourceEvidenceType,
    SupplierSourceReference,
    SupplierValidationRule,
)
from schemas.catalogue_pipeline.supplier_contracts.registry import register_supplier_source_contract

from ._shared import DECLARATION_CREATED_AT, DECLARATION_CREATED_BY, evidence, pipeline_mapping


_EVIDENCE = [
    evidence(
        SupplierSourceEvidenceType.REAL_SOURCE_CATALOGUE_SAMPLE,
        "external-sample:Hill's.pdf",
        "I supplied a 9-page PDF sample confirming bilingual headers, Effective dates, Gross Wholesale Price, Product Code, Size, and Order Multiple columns.",
    ),
    evidence(
        SupplierSourceEvidenceType.REAL_SOURCE_CATALOGUE_SAMPLE,
        "external-sample:Hill's 2026 new price.pdf",
        "I supplied a 6-page 2026 Prescription Diet (Feline) edition; a live extraction run confirmed it replaces the Life Stage column with Disease Category while keeping every other declared column.",
    ),
    evidence(
        SupplierSourceEvidenceType.PARSER_BEHAVIOR,
        "apps/api/services/supplier_source_contract_runtime.py",
        "Runtime adapter applies supported Pydantic source-contract semantics for per-unit cost, order multiple, constant brand/category, and weight parsing.",
    ),
    evidence(
        SupplierSourceEvidenceType.EXISTING_PRODUCTION_TEST_EXTRACTION_FIXTURE,
        "apps/api/tests/test_supplier_source_contract_runtime.py::test_hills_runtime_applies_supported_contract_semantics",
        "Tests representative Hill's rows against the Pydantic-backed runtime adapter.",
    ),
    evidence(
        SupplierSourceEvidenceType.BUSINESS_DOMAIN_DOCUMENTATION,
        "docs/architecture/catalogue-domain/catalogue-entity-dictionary.md",
        "Domain dictionary uses Hill's as the worked example for source evidence and supplier-price gaps.",
    ),
]


HILLS_PRICE_LIST_V1 = register_supplier_source_contract(
    SupplierSourceContractV1(
        schema_version=SUPPLIER_SOURCE_SCHEMA_VERSION,
        contract_id="hills.price_list.v1",
        contract_version="v1",
        supplier=SupplierSourceReference(supplier_id=14, supplier_name="Hill's", supplier_code=None),
        document_type=SupplierDocumentType.PRICE_LIST,
        format_name="Hill's PDF price list (Science Diet and Prescription Diet editions)",
        source_format=SourceFormat.PDF_TABLE,
        support_status=SupplierContractSupportStatus.SUPPORTED,
        evidence=_EVIDENCE,
        source_structure=SourceStructure(
            source_format=SourceFormat.PDF_TABLE,
            expected_sections=["Feline", "Canine", "Prescription Diet", "Science Diet"],
            table_regions=[
                SourceTableRegion(
                    name="price_rows",
                    selector="Bilingual PDF product tables with Product Code, product description, size, wholesale, retail, and order multiple columns.",
                    notes="Raw PDF sample was supplied externally; region details are captured from source text and parser tests.",
                )
            ],
            required_headers=[
                "Product Code / 產品編號",
                "Product Range / 產品系列",
                "Product Description / 產品名稱",
                "Size / 重量",
                "Gross Wholesale Price / 每箱·罐",
                "Order Multiple / 訂貨單位",
            ],
            optional_headers=[
                # Edition-specific dimension: Science Diet editions print Life
                # Stage; Prescription Diet editions print Disease Category.
                "Life Stage / 生命階段",
                "Disease Category / 疾病種類",
                "Recommended Retail Selling Price / 建議零售價",
                "Regular Retail Price / 正價",
            ],
            row_eligibility_rules=["One product variant per price-table row."],
            source_location_expectations=["page number", "table row", "source column"],
        ),
        fields=[
            SourceFieldContract(
                field_key="supplier_sku",
                role=SourceFieldRole.SUPPLIER_SKU,
                requirement=SourceFieldRequirement.REQUIRED,
                source_column="Product Code / 產品編號",
                description="Stable Hill's product code.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="description",
                role=SourceFieldRole.PRODUCT_NAME,
                requirement=SourceFieldRequirement.REQUIRED,
                composed_from=[
                    "Product Range / 產品系列",
                    "Disease Category / 疾病種類",
                    "Life Stage / 生命階段",
                    "Product Description / 產品名稱",
                ],
                description="Joined product range, disease category (Prescription Diet editions), life stage (Science Diet editions), and product description; absent columns drop out of the join.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="pack_size",
                role=SourceFieldRole.PACKAGING,
                requirement=SourceFieldRequirement.REQUIRED,
                source_column="Size / 重量",
                description="Raw size/content string; may contain content measure or a case-prefix plus content measure.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="cost",
                role=SourceFieldRole.SOURCE_PRICE,
                requirement=SourceFieldRequirement.REQUIRED,
                source_column="Gross Wholesale Price / 每箱·罐",
                description="Supplier cost source field used by the supported runtime adapter.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="rrp",
                role=SourceFieldRole.RRP,
                requirement=SourceFieldRequirement.OPTIONAL,
                source_column="Recommended Retail Selling Price / 建議零售價",
                description="Recommended retail price when present.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="order_multiple",
                role=SourceFieldRole.ORDER_INCREMENT,
                requirement=SourceFieldRequirement.REQUIRED,
                source_column="Order Multiple / 訂貨單位",
                description="Order multiple; never proof for price divisor.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="brand",
                role=SourceFieldRole.BRAND,
                requirement=SourceFieldRequirement.OPTIONAL,
                constant_value="Hill's",
                description="Single-brand catalogue constant.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="species",
                role=SourceFieldRole.SPECIES,
                requirement=SourceFieldRequirement.OPTIONAL,
                source_path="section_header",
                description="Feline/Canine section banners map to cat/dog.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="segment",
                role=SourceFieldRole.SEGMENT,
                requirement=SourceFieldRequirement.OPTIONAL,
                source_path="product_range",
                description="Prescription Diet vs Science Diet segment cue from the product range.",
                evidence=_EVIDENCE,
            ),
            SourceFieldContract(
                field_key="category",
                role=SourceFieldRole.CATEGORY,
                requirement=SourceFieldRequirement.OPTIONAL,
                constant_value="Food",
                description="Current parser treats Hill's price-list rows as Food.",
                evidence=_EVIDENCE,
            ),
        ],
        pricing=PricingSourceSemantics(
            cost_source_field="cost",
            rrp_source_field="rrp",
            price_basis=UnitOfMeasure(code=UnitCode.UNIT),
            price_basis_status=SemanticResolutionStatus.VERIFIED,
            autoswap_cost_rrp_allowed=False,
            notes="Source sample and parser tests establish Gross Wholesale Price as a per sellable unit price.",
        ),
        packaging=PackagingSourceSemantics(
            packaging_source_field="pack_size",
            price_basis=UnitOfMeasure(code=UnitCode.UNIT),
            content_measure_source_field="pack_size",
            order_increment_source_field="order_multiple",
            break_pack_allowed=None,
            interpretation_rules=[
                "Treat Size / 重量 as content measure for one sellable unit when a weight/volume unit is printed.",
                "Ignore a leading case prefix such as 24/ when deriving sellable-unit content measure.",
                "Order Multiple / 訂貨單位 is ordering semantics, not cost divisor evidence.",
            ],
            unresolved_semantics=[
                "Purchase UOM varies by row and is not fixed by the format declaration.",
                "Break-pack purchasing is not proven by the checked-in evidence.",
            ],
        ),
        validation_rules=[
            SupplierValidationRule(
                rule_id="hills.cost_below_rrp",
                description="Wholesale cost should be lower than RRP when both are present.",
                source_expression="cost_price < rrp",
                severity=IssueSeverity.ERROR,
                issue_code="HILLS_COST_NOT_BELOW_RRP",
                review_guidance="Check whether the wholesale and retail price columns were swapped before approving the item.",
                evidence=_EVIDENCE,
            ),
            SupplierValidationRule(
                rule_id="hills.order_multiple_positive",
                description="Order multiple must be positive when present.",
                source_expression="order_increment_qty >= 1",
                severity=IssueSeverity.ERROR,
                issue_code="HILLS_ORDER_MULTIPLE_NOT_POSITIVE",
                review_guidance="Confirm the printed order multiple before approving the purchasing terms.",
                evidence=_EVIDENCE,
            ),
        ],
        known_ambiguities=[
            AmbiguityRule(
                issue_code="HILLS_SUPPLIER_CODE_NOT_IN_SEED",
                condition="The source contract can identify Hill's by supplier_id/name, but this clean checkout does not seed a verified Hill's supplier code.",
                review_guidance="Confirm the supplier master code before recording a supplier_code on this contract.",
                blocks_supported_status=False,
            )
        ],
        pipeline_mapping=pipeline_mapping(
            "supplier_sku",
            "description",
            "pack_size",
            "cost",
            "rrp",
            "order_multiple",
            "brand",
            "species",
            "segment",
            "category",
        ),
        created_at=DECLARATION_CREATED_AT,
        created_by=DECLARATION_CREATED_BY,
    )
)
