import pytest

from agentgenome.parameters import ParameterError, contracts_to_specs


def test_contracts_use_explicit_types_defaults_and_legacy_file_hints() -> None:
    specs = contracts_to_specs(
        {
            "limit": {"type": "number"},
            "enabled": {"default": True},
            "label": {"default": "all"},
            "raw": {"format": "csv", "required_fields": ["text"]},
        }
    )

    assert specs["limit"].type == "number"
    assert specs["enabled"].type == "boolean"
    assert specs["label"].type == "string"
    assert specs["raw"].type == "path"


def test_untyped_scalar_contract_fails_explicitly() -> None:
    with pytest.raises(
        ParameterError,
        match="parameter 'limit' contract must declare type",
    ):
        contracts_to_specs({"limit": {"description": "row cap"}})
