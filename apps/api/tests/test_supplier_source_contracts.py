import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas.catalogue_pipeline import registry_snapshot as pipeline_registry_snapshot
from schemas.catalogue_pipeline.supplier_contracts import (
    HILLS_PRICE_LIST_V1,
    SUPPLIER_SOURCE_SCHEMA_VERSION,
    SupplierContractSupportStatus,
    SupplierSourceContractRegistry,
    SupplierSourceContractV1,
    get_supported_supplier_source_contract,
    get_supplier_source_contract,
    iter_supplier_source_contracts,
    supplier_source_registry_snapshot,
)
from schemas.catalogue_pipeline.supplier_contracts.common import SemanticResolutionStatus


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "catalogue_pipeline" / "supplier_source" / "v1"
VALID_FIXTURES = sorted((FIXTURE_ROOT / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((FIXTURE_ROOT / "invalid").glob("*.json"))
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_CONTRACT_IDS = [
    "alfamedic.price_list.v1",
    "hills.price_list.v1",
    "kangaroo.earthz_pet_price_sheet.v1",
    "kangaroo.mixed_price_catalogue.v1",
    "kangaroo.purina_proplan_veterinary_diets.v1",
    "vetapet.non_vet_price_list.v1",
    "vetapet.vet_price_list.v1",
]

EXPECTED_INVALID_MESSAGES = {
    "content_measure_reused_as_sellable_units.json": "content measure source cannot be reused",
    "forbidden_extra_field.json": "Extra inputs are not permitted",
    "invalid_support_status.json": "Input should be",
    "required_field_without_source.json": "source field requires source_column",
    "supplier_without_id_or_code.json": "supplier source reference requires supplier_id or supplier_code",
    "supported_with_unresolved_price_basis.json": "SUPPORTED supplier contracts require VERIFIED price basis",
    "unknown_field_reference.json": "references unknown field_key",
    "unsupported_currency.json": "Input should be 'HKD'",
    "wrong_schema_version.json": "catalogue.supplier_source_contract.v1",
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _synthetic_hills_contract(**updates) -> SupplierSourceContractV1:
    payload = _load(FIXTURE_ROOT / "valid" / "hills.price_list.v1.json")
    payload["contract_id"] = updates.pop("contract_id", "synthetic_hills.price_list.v1")
    payload["supplier"] = updates.pop(
        "supplier",
        {"supplier_id": 777, "supplier_name": "Synthetic Hill's Supplier", "supplier_code": "SYNH"},
    )
    payload["format_name"] = updates.pop("format_name", "Synthetic Hill's price list")
    payload.update(updates)
    return SupplierSourceContractV1.model_validate(payload)


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_supplier_source_fixtures_validate_and_are_registered(path):
    payload = _load(path)

    contract = SupplierSourceContractV1.model_validate(payload)
    registration = get_supplier_source_contract(contract.contract_id, contract.contract_version)

    assert registration.declaration == contract
    assert registration.model is SupplierSourceContractV1
    assert contract.schema_version == "catalogue.supplier_source_contract.v1"
    assert contract.contract_id.endswith(f".{contract.contract_version}")
    assert contract.pipeline_mapping.raw_observation_contract_id == "catalogue.extracted_evidence.v1"
    assert contract.pipeline_mapping.staging_item_contract_id == "catalogue.normalized_row.v1"


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_invalid_supplier_source_fixtures_fail_for_expected_reason(path):
    expected = EXPECTED_INVALID_MESSAGES[path.name]

    with pytest.raises(ValidationError) as excinfo:
        SupplierSourceContractV1.model_validate(_load(path))

    assert expected in str(excinfo.value)


def test_supplier_source_registry_is_deterministic_and_separate_from_pipeline_registry():
    registrations = iter_supplier_source_contracts()
    ids = [item.contract_id for item in registrations]

    assert ids == EXPECTED_CONTRACT_IDS
    assert sorted(supplier_source_registry_snapshot()) == [(item.contract_id, item.contract_version) for item in registrations]
    assert set(pipeline_registry_snapshot()) == {
        "catalogue.extraction_profile.v1",
        "catalogue.extracted_evidence.v1",
        "catalogue.normalized_row.v1",
        "catalogue.mastering_candidate.v1",
        "catalogue.validation_issue.v1",
        "catalogue.serving_item.v1",
    }


def test_unknown_supplier_source_contracts_and_versions_fail_without_fallback():
    with pytest.raises(ValueError, match="Unknown supplier source contract"):
        get_supplier_source_contract("unknown_supplier.price_list.v1", "v1")

    with pytest.raises(ValueError, match="Unknown supplier source contract"):
        get_supplier_source_contract("hills.price_list.v1", "v2")


def test_duplicate_supplier_source_registration_fails():
    registry = SupplierSourceContractRegistry()
    registry.register(HILLS_PRICE_LIST_V1)

    with pytest.raises(ValueError, match="duplicate supplier source contract registration"):
        registry.register(HILLS_PRICE_LIST_V1)


def test_conflicting_supplier_format_identity_registration_fails():
    first = _synthetic_hills_contract(contract_id="synthetic_hills.price_list.v1")
    conflicting = _synthetic_hills_contract(contract_id="synthetic_hills.price_list_copy.v1")
    registry = SupplierSourceContractRegistry()

    registry.register(first)

    with pytest.raises(ValueError, match="conflicting supplier source contract identity"):
        registry.register(conflicting)


def test_supplier_source_contract_rejects_blank_identity_and_invalid_supported_status_combination():
    payload = _load(FIXTURE_ROOT / "valid" / "hills.price_list.v1.json")
    payload["contract_id"] = " "

    with pytest.raises(ValidationError, match="identity text cannot be blank"):
        SupplierSourceContractV1.model_validate(payload)

    payload = _load(FIXTURE_ROOT / "valid" / "kangaroo.mixed_price_catalogue.v1.json")
    payload["schema_version"] = SUPPLIER_SOURCE_SCHEMA_VERSION
    payload["support_status"] = "SUPPORTED"
    payload["pricing"]["price_basis_status"] = "VERIFIED"
    payload["known_ambiguities"] = []

    with pytest.raises(ValidationError, match="numeric supplier_id"):
        SupplierSourceContractV1.model_validate(payload)


def test_non_supported_contracts_cannot_be_selected_for_production_interpretation():
    statuses = {item.contract_id: item.support_status for item in iter_supplier_source_contracts()}

    assert statuses["hills.price_list.v1"] == SupplierContractSupportStatus.SUPPORTED
    assert statuses["alfamedic.price_list.v1"] == SupplierContractSupportStatus.SUPPORTED
    assert statuses["vetapet.vet_price_list.v1"] == SupplierContractSupportStatus.PARTIALLY_VERIFIED
    assert statuses["vetapet.non_vet_price_list.v1"] == SupplierContractSupportStatus.PARTIALLY_VERIFIED
    assert statuses["kangaroo.mixed_price_catalogue.v1"] == SupplierContractSupportStatus.PARTIALLY_VERIFIED
    assert statuses["kangaroo.purina_proplan_veterinary_diets.v1"] == SupplierContractSupportStatus.PARTIALLY_VERIFIED
    assert statuses["kangaroo.earthz_pet_price_sheet.v1"] == SupplierContractSupportStatus.UNVERIFIED

    assert get_supported_supplier_source_contract("hills.price_list.v1", "v1").contract_id == "hills.price_list.v1"
    assert get_supported_supplier_source_contract("alfamedic.price_list.v1", "v1").contract_id == "alfamedic.price_list.v1"
    for contract_id in set(EXPECTED_CONTRACT_IDS) - {"hills.price_list.v1", "alfamedic.price_list.v1"}:
        with pytest.raises(ValueError, match="not SUPPORTED"):
            get_supported_supplier_source_contract(contract_id, "v1")


def test_packaging_content_measure_is_not_sellable_unit_count():
    hills = get_supplier_source_contract("hills.price_list.v1", "v1").declaration

    assert hills.packaging.content_measure_source_field == "pack_size"
    assert hills.packaging.sellable_units_per_purchase_unit_source_field is None
    assert hills.packaging.order_increment_source_field == "order_multiple"
    assert any("content measure" in rule for rule in hills.packaging.interpretation_rules)


def test_ambiguous_cost_basis_remains_unresolved_for_unverified_vetapet_non_vet():
    non_vet = get_supplier_source_contract("vetapet.non_vet_price_list.v1", "v1").declaration

    assert non_vet.support_status == SupplierContractSupportStatus.PARTIALLY_VERIFIED
    assert non_vet.pricing.price_basis is None
    assert non_vet.pricing.price_basis_status == SemanticResolutionStatus.UNRESOLVED
    assert any(issue.issue_code == "VETAPET_NON_VET_PRICE_BASIS_UNRESOLVED" for issue in non_vet.known_ambiguities)


def test_kangaroo_contracts_use_supplier_code_without_fabricated_numeric_id():
    mixed = get_supplier_source_contract("kangaroo.mixed_price_catalogue.v1", "v1").declaration
    proplan = get_supplier_source_contract("kangaroo.purina_proplan_veterinary_diets.v1", "v1").declaration
    earthz = get_supplier_source_contract("kangaroo.earthz_pet_price_sheet.v1", "v1").declaration

    assert mixed.supplier.supplier_id is None
    assert mixed.supplier.supplier_code == "KPN"
    assert proplan.pricing.price_basis.code == "PACK"
    assert earthz.pricing.price_basis is None
    assert earthz.source_format == "PDF"


def test_supported_status_requires_real_evidence():
    payload = _load(FIXTURE_ROOT / "valid" / "hills.price_list.v1.json")
    payload["support_status"] = "SUPPORTED"
    payload["pricing"]["price_basis_status"] = "VERIFIED"
    payload["known_ambiguities"] = []
    payload["evidence"] = [
        {
            "evidence_type": "MISSING",
            "reference": "missing source evidence",
            "note": "Missing evidence alone is insufficient for SUPPORTED.",
        }
    ]

    with pytest.raises(ValidationError, match="evidence beyond missing evidence"):
        SupplierSourceContractV1.model_validate(payload)


def test_supplier_source_schema_artifacts_are_current_and_specific_to_registered_contracts():
    result = subprocess.run(
        [sys.executable, "scripts/export_supplier_source_contract_schemas.py", "--check"],
        cwd=BACKEND_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr

    schema_path = (
        REPO_ROOT
        / "docs"
        / "contracts"
        / "catalogue-pipeline"
        / "supplier-source"
        / "v1"
        / "hills.price_list.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == "catalogue.supplier_source_contract.v1"
    assert schema["properties"]["contract_id"]["const"] == "hills.price_list.v1"
    assert schema["properties"]["contract_version"]["const"] == "v1"
    assert schema["additionalProperties"] is False
    encoded = json.dumps(schema)
    assert "SupplierContractSupportStatus" in encoded


def test_supplier_source_import_does_not_import_fastapi_app_or_database():
    code = (
        "import sys; "
        "import schemas.catalogue_pipeline.supplier_contracts as sc; "
        "assert 'main' not in sys.modules; "
        "assert 'database' not in sys.modules; "
        "print([r.contract_id for r in sc.iter_supplier_source_contracts()])"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_ROOT, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
