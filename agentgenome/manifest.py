"""Optional manifest metadata: applicability and contracts.

These fields are additive. A package without them stays valid; a package with
them answers "when does this apply, and what does it consume" without reading
the source trace.
"""
from __future__ import annotations

from typing import Any, Mapping

DETERMINISM = ("script", "agent-assisted", "human-gated")
MATCH_KEYS = ("applies_when", "not_applies_when", "input_contract", "output_contract",
              "determinism")
_CONTRACT_KEYS = {"format", "fields", "required_fields", "optional_fields", "description"}
_LIST_KEYS = {"fields", "required_fields", "optional_fields"}


class ManifestError(ValueError):
    pass


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty string")
    return value.strip()


def _text_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ManifestError(f"{where} must be a list of strings")
    return [_text(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _contract(value: Any, where: str) -> None:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{where} must be an object keyed by name")
    for name, raw in value.items():
        key = _text(name, f"{where} key")
        if not isinstance(raw, Mapping):
            raise ManifestError(f"{where}.{key} must be an object")
        for field, item in raw.items():
            if field not in _CONTRACT_KEYS:
                raise ManifestError(f"{where}.{key}.{field} is not supported")
            if field in _LIST_KEYS:
                _text_list(item, f"{where}.{key}.{field}")
            else:
                _text(item, f"{where}.{key}.{field}")


def validate(manifest: Mapping[str, Any]) -> None:
    for key in ("applies_when", "not_applies_when"):
        if key in manifest:
            _text_list(manifest[key], key)
    for key in ("input_contract", "output_contract"):
        if key in manifest:
            _contract(manifest[key], key)
    if "determinism" in manifest and manifest["determinism"] not in DETERMINISM:
        raise ManifestError(f"determinism must be one of {list(DETERMINISM)}")


def view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The compact shape genome_list returns: enough to match without genome_get."""
    result = {key: manifest[key] for key in ("id", "version", "name", "description")}
    for key in ("applies_when", "not_applies_when", "input_contract", "determinism"):
        if key in manifest:
            result[key] = manifest[key]
    return result


def declared(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest's applicability and contract declarations, as captured."""
    return {key: manifest[key] for key in MATCH_KEYS if key in manifest}
