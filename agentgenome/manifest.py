"""Optional manifest metadata: applicability and contracts.

These fields are additive. A package without them stays valid; a package with
them answers "when does this apply, and what does it consume" without reading
the source trace.
"""
from __future__ import annotations

from typing import Any, Mapping

from .parameters import PARAMETER_TYPES

DETERMINISM = ("script", "agent-assisted", "human-gated")
MATCH_KEYS = ("applies_when", "not_applies_when", "input_contract", "output_contract",
              "determinism")
_CONTRACT_KEYS = {"type", "format", "fields", "required_fields", "optional_fields", "description"}
_LIST_KEYS = {"fields", "required_fields", "optional_fields"}
PARAM_TYPES = PARAMETER_TYPES


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


def _contract_problems(value: Any, where: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where} must be an object keyed by name"]
    problems: list[str] = []
    for name, raw in value.items():
        try:
            key = _text(name, f"{where} key")
        except ManifestError as exc:
            problems.append(str(exc))
            continue
        if not isinstance(raw, Mapping):
            problems.append(f"{where}.{key} must be an object")
            continue
        for field, item in raw.items():
            if field not in _CONTRACT_KEYS:
                problems.append(
                    f"{where}.{key}.{field} is not supported; "
                    f"allowed fields: {', '.join(sorted(_CONTRACT_KEYS))}"
                )
                continue
            if field in _LIST_KEYS:
                try:
                    _text_list(item, f"{where}.{key}.{field}")
                except ManifestError as exc:
                    problems.append(str(exc))
            elif field == "type":
                if item not in PARAM_TYPES:
                    problems.append(
                        f"{where}.{key}.type must be one of {list(PARAM_TYPES)}"
                    )
            else:
                try:
                    _text(item, f"{where}.{key}.{field}")
                except ManifestError as exc:
                    problems.append(str(exc))
    return problems


def validate(manifest: Mapping[str, Any]) -> None:
    problems: list[str] = []
    for key in ("applies_when", "not_applies_when"):
        if key in manifest:
            try:
                _text_list(manifest[key], key)
            except ManifestError as exc:
                problems.append(str(exc))
    for key in ("input_contract", "output_contract"):
        if key in manifest:
            problems.extend(_contract_problems(manifest[key], key))
    if "determinism" in manifest and manifest["determinism"] not in DETERMINISM:
        problems.append(f"determinism must be one of {list(DETERMINISM)}")
    if len(problems) == 1:
        raise ManifestError(problems[0])
    if problems:
        listed = "\n".join(
            f"{index}. {problem}" for index, problem in enumerate(problems, start=1)
        )
        raise ManifestError(
            f"manifest validation found {len(problems)} problems:\n{listed}"
        )


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
