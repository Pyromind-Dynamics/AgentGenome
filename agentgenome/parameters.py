"""Typed graph parameters shared by capture, replay, and execution."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PARAMETER_TYPES = ("path", "string", "number", "boolean", "records")
GRAPH_TYPES = {
    "path": "path",
    "string": "text",
    "number": "number",
    "boolean": "bool",
    "records": "number",
}
FILE_CONTRACT_HINTS = frozenset(
    {"format", "fields", "required_fields", "optional_fields"}
)
_INTEGER = re.compile(r"[+-]?\d+\Z")
_MISSING = object()


class ParameterError(ValueError):
    """A parameter cannot be represented by the requested type."""


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    type: str
    source: str
    value: Any = None
    default: Any = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ParameterError("parameter key must not be empty")
        if self.type not in PARAMETER_TYPES:
            raise ParameterError(
                f"parameter {self.key!r} has unsupported type {self.type!r}"
            )

    @property
    def graph_type(self) -> str:
        return GRAPH_TYPES[self.type]


def parameter_spec(
    key: str,
    *,
    type_name: str | None,
    source: str,
    value: Any = None,
    default: Any = None,
) -> ParameterSpec:
    return ParameterSpec(
        key=str(key),
        type=_normalize_type(type_name, source=source),
        source=str(source),
        value=value,
        default=default,
    )


def _normalize_type(type_name: str | None, *, source: str) -> str:
    if type_name is None or not str(type_name).strip():
        raise ParameterError(
            f"parameter {source!r} contract must declare type: "
            f"use one of {', '.join(PARAMETER_TYPES)}"
        )
    value = str(type_name).strip().lower()
    aliases = {"bool": "boolean", "integer": "number", "int": "number"}
    value = aliases.get(value, value)
    if value not in PARAMETER_TYPES:
        raise ParameterError(
            f"parameter {source!r} has unsupported type {type_name!r}"
        )
    return value


def contracts_to_specs(
    contract: Mapping[str, Any] | None,
) -> dict[str, ParameterSpec]:
    if contract is not None and not isinstance(contract, Mapping):
        raise ParameterError("input_contract must be an object keyed by parameter name")
    specs: dict[str, ParameterSpec] = {}
    for key, raw in (contract or {}).items():
        if not isinstance(raw, Mapping):
            raise ParameterError(f"parameter {key!r} contract must be an object")
        source = str(key)
        specs[str(key)] = parameter_spec(
            str(key),
            type_name=_contract_type(str(key), raw),
            source=source,
            default=raw.get("default"),
        )
    return specs


def _contract_type(key: str, spec: Mapping[str, Any]) -> str:
    """Resolve one contract's type without guessing that every scalar is a path."""
    explicit = spec.get("type")
    if explicit is not None and str(explicit).strip():
        return _normalize_type(explicit, source=key)
    if FILE_CONTRACT_HINTS.intersection(spec):
        return "path"
    default = spec.get("default", _MISSING)
    if default is not _MISSING:
        if isinstance(default, bool):
            return "boolean"
        if isinstance(default, (int, float)):
            return "number"
        if isinstance(default, str):
            return "string"
    raise ParameterError(
        f"parameter {key!r} contract must declare type: "
        f"use one of {', '.join(PARAMETER_TYPES)}"
    )


def normalize_value(type_name: str, value: Any, *, key: str) -> Any:
    kind = _normalize_type(type_name, source=key)
    if kind == "path":
        if not isinstance(value, str) or not value:
            raise ParameterError(f"parameter {key!r} must be a non-empty path")
        return value
    if kind == "string":
        if not isinstance(value, str):
            raise ParameterError(f"parameter {key!r} must be a string")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if isinstance(value, str) and _INTEGER.fullmatch(value):
                return int(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError as exc:
                    raise ParameterError(
                        f"parameter {key!r} must be a number"
                    ) from exc
            raise ParameterError(f"parameter {key!r} must be a number")
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ParameterError(f"parameter {key!r} must be a boolean")
    if isinstance(value, bool):
        raise ParameterError(f"parameter {key!r} must be a non-negative integer")
    if isinstance(value, str) and _INTEGER.fullmatch(value):
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise ParameterError(f"parameter {key!r} must be a non-negative integer")
    return value


def normalize_params(
    specs: Mapping[str, ParameterSpec],
    params: Mapping[str, Any],
    *,
    require_all: bool = True,
) -> dict[str, Any]:
    defaults = {
        key: spec.default
        for key, spec in specs.items()
        if spec.default is not None
    }
    values = {**defaults, **params}
    if require_all:
        expected = set(specs)
        if set(values) != expected:
            raise ParameterError(
                f"parameters do not match the asset inputs: "
                f"expected {sorted(expected)}, got {sorted(values)}"
            )
    unknown = set(values) - set(specs)
    if unknown:
        raise ParameterError(f"unknown parameters: {sorted(unknown)}")
    return {
        key: normalize_value(specs[key].type, value, key=key)
        for key, value in values.items()
    }


def path_bindings(
    specs: Mapping[str, ParameterSpec],
    params: Mapping[str, Any],
    *,
    include_untyped_strings: bool = False,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for key, value in params.items():
        spec = specs.get(key)
        if spec is not None:
            if spec.type == "path":
                bindings[key] = str(value)
            continue
        if include_untyped_strings and isinstance(value, str):
            bindings[key] = value
    return bindings


def path_keys(
    specs: Mapping[str, ParameterSpec],
    params: Mapping[str, Any],
) -> list[str]:
    return [
        key
        for key in params
        if specs.get(key) is not None and specs[key].type == "path"
    ]
