import pytest

from agentgenome.manifest import ManifestError, declared, validate, view


def manifest(**overrides):
    base = {"id": "asset", "version": "1.0.0", "name": "Asset", "description": "demo"}
    return {**base, **overrides}


def test_view_carries_only_declared_applicability():
    assert view(manifest()) == {
        "id": "asset", "version": "1.0.0", "name": "Asset", "description": "demo"}
    declared_manifest = manifest(
        applies_when=["clean a raw question CSV"],
        not_applies_when=["input is already messages-schema JSONL"],
        input_contract={"data_file": {"format": "csv", "required_fields": ["qid"]}},
        determinism="agent-assisted",
    )
    assert "has_references" not in view(declared_manifest)
    assert "has_gaps" not in view(declared_manifest)
    assert view(declared_manifest)["input_contract"]["data_file"]["required_fields"] == ["qid"]


def test_validate_rejects_unknown_contract_fields():
    with pytest.raises(ManifestError, match="not supported"):
        validate(manifest(input_contract={"data_file": {"format": "csv", "columns": ["qid"]}}))


def test_validate_rejects_unknown_determinism():
    with pytest.raises(ManifestError, match="determinism must be one of"):
        validate(manifest(determinism="half-scripted"))


def test_declared_keeps_only_matching_metadata():
    assert declared(manifest(applies_when=["raw CSV"], owner="local")) == {
        "applies_when": ["raw CSV"]}


def test_legacy_manifests_still_validate():
    """Packages captured before the schema was trimmed keep importing."""
    validate(manifest(references=[{"path": "references/case.md"}],
                      gaps=[{"reason": "llm judgement"}]))
