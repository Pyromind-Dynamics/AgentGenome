"""Execution-plan input analysis.

The analysis is a user-facing contract, not an execution decision.  It describes
the concrete inputs and graph shape that will be used after the user confirms a
plan.  Path reads are bounded and failures are recorded as evidence.
"""
from __future__ import annotations

from typing import Any, Mapping

from .capture.sample import SAMPLE_BYTES, inspect

_TEXT_PREVIEW = 2048


def execution_scope(
    value: Mapping[str, Any] | None,
    *,
    has_path_inputs: bool,
) -> dict[str, Any]:
    if value is None:
        return {
            "mode": "unspecified" if has_path_inputs else "full",
            "description": (
                "尚未确认执行范围；先向用户展示输入证据，再选择 full、sample 或 subset"
                if has_path_inputs
                else "图没有路径输入，按参数执行完整图"
            ),
            "requires_explicit_selection": has_path_inputs,
        }
    if not isinstance(value, Mapping):
        raise ValueError("execution_scope must be an object")
    mode = value.get("mode")
    if mode not in {"full", "sample", "subset", "unspecified"}:
        raise ValueError("execution_scope.mode must be full, sample, subset, or unspecified")
    description = value.get("description", "")
    if not isinstance(description, str):
        raise ValueError("execution_scope.description must be a string")
    result: dict[str, Any] = {
        "mode": mode,
        "description": description.strip(),
        "requires_explicit_selection": mode == "unspecified",
    }
    for key in ("records", "source_path"):
        raw = value.get(key)
        if raw is None:
            continue
        if key == "records":
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise ValueError("execution_scope.records must be a non-negative integer")
        elif not isinstance(raw, str) or not raw:
            raise ValueError("execution_scope.source_path must be a non-empty string")
        result[key] = raw
    return result


def _input(source: Any, key: str, path: str) -> dict[str, Any]:
    entry = inspect(source, path)
    text = entry.pop("text", None)
    preview = text[:_TEXT_PREVIEW] if isinstance(text, str) else None
    rows = entry.get("rows")
    return {
        "key": key,
        "path": path,
        "format": entry.get("format"),
        "bytes": entry.get("bytes"),
        "head_bytes": SAMPLE_BYTES,
        "truncated": bool(entry.get("truncated")),
        "head_sha256": entry.get("sha256"),
        "rows": rows,
        "row_count_exact": rows is not None and not bool(entry.get("truncated")),
        "schema": entry.get("schema") or {},
        "preview": preview,
        "note": entry.get("note"),
    }


def _nodes(node: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(current: Any) -> None:
        kind = "tool" if current.tool is not None else (current.do_kind or "group")
        entry: dict[str, Any] = {
            "path": current.path,
            "kind": kind,
            "needs": sorted(current.needs),
            "inputs": sorted(current.input),
            "outputs": sorted(current.output),
            "danger": bool(current.danger),
            "verify": [rule.kind for rule in current.verify],
        }
        if current.tool is not None:
            entry["tool"] = current.tool.name
            entry["side_effect"] = True
        if current.do_kind == "human":
            entry["human_question"] = current.do
        result.append(entry)
        for child in current.children:
            visit(child)

    visit(node)
    return result


def analyze_graph(
    graph: Any,
    asset: Mapping[str, Any],
    params: Mapping[str, Any],
    source: Any,
    *,
    requested_scope: Mapping[str, Any] | None,
    compatibility: Mapping[str, Any] | None = None,
    capture_id: str | None = None,
) -> dict[str, Any]:
    specs = asset.get("parameters") or {}
    path_params = [
        (key, str(params[key]))
        for key, spec in specs.items()
        if spec.get("type") == "path" and isinstance(params.get(key), str)
    ]
    inputs = [_input(source, key, path) for key, path in path_params]
    nodes = _nodes(graph.node)
    tools = [entry for entry in nodes if entry["kind"] == "tool"]
    humans = [entry for entry in nodes if entry["kind"] == "human"]
    outputs = {
        key: spec.type
        for key, spec in graph.node.output.items()
    }
    scope = execution_scope(requested_scope, has_path_inputs=bool(path_params))
    return {
        "asset": {
            "id": asset["id"],
            "version": asset["version"],
            "pin": asset["pin"],
            "published": bool(asset.get("published")),
        },
        "capture_id": capture_id,
        "parameters": dict(params),
        "inputs": inputs,
        "graph": {
            "nodes": nodes,
            "human_gates": humans,
            "tool_side_effects": tools,
            "expected_outputs": outputs,
        },
        "execution_scope": scope,
        "compatibility": dict(compatibility or {}),
        "requires_user_confirmation": True,
    }
