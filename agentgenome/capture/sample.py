"""Capture-time input sampling.

A capture can only be reused when the next input looks like the one it was built
from, and the package is the wrong place to look: its bytes are the evidence the
validation run executed, so rewriting them after that run would invalidate the
publication. Every capture therefore keeps a bounded head of its root inputs next
to its state, together with the schema that head reveals.

Sampling is evidence, never a gate: an input that is missing, unreadable or too
large is recorded with the reason instead of failing the capture.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

SAMPLE_BYTES = 32 * 1024
TOTAL_BYTES = 512 * 1024
ROWS = 20
INDEX_NAME = "index.json"

_TABLES = {".csv": ",", ".tsv": "\t"}
_LINES = {".jsonl", ".ndjson"}
_TEXT = {".txt", ".md", ".log", ".yaml", ".yml"}


@runtime_checkable
class HeadSource(Protocol):
    """A host that reads a file's head without pulling the whole file."""

    def head(self, path: str, limit: int) -> bytes:
        """Return at most ``limit`` bytes from the start of ``path``."""


def read_head(source: Any, path: str, limit: int) -> bytes:
    """One byte past ``limit`` when the host can, so a cut stays observable."""
    if isinstance(source, HeadSource):
        return source.head(path, limit + 1)
    return source.read(path)[: limit + 1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank(path: str, note: str) -> dict[str, Any]:
    return {"path": path, "format": "unavailable", "bytes": None, "truncated": False,
            "sha256": None, "rows": None, "schema": {}, "text": None, "note": note}


def _directory(path: str) -> dict[str, Any]:
    """A directory input: the path is the evidence, there is no head to keep."""
    entry = _blank(path, "目录输入：只记录路径与格式，不采样内容")
    entry["format"] = "dir"
    return entry


def _detect(path: str, text: str) -> tuple[str, str]:
    """The file's format and, for tables, the delimiter it uses."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _TABLES:
        return ("csv" if suffix == ".csv" else "tsv"), _TABLES[suffix]
    if suffix in _LINES:
        return "jsonl", ""
    if suffix == ".json":
        return "json", ""
    if suffix in _TEXT:
        return "text", ""
    first = next((line for line in text.splitlines() if line.strip()), "")
    if "\t" in first:
        return "tsv", "\t"
    if "," in first:
        return "csv", ","
    return "text", ""


def _shape(fmt: str, text: str, delimiter: str) -> tuple[dict[str, Any], int | None]:
    """The fields the head reveals, and how many records it carries."""
    if fmt in {"csv", "tsv"}:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        return {"columns": rows[0] if rows else [], "delimiter": delimiter}, max(len(rows) - 1, 0)
    if fmt == "jsonl":
        records, fields = 0, []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            records += 1
            if isinstance(record, dict):
                for key in record:
                    if key not in fields:
                        fields.append(key)
        return {"fields": fields}, records
    if fmt == "json":
        try:
            value = json.loads(text)
        except ValueError:
            return {"complete": False}, 0
        if isinstance(value, list):
            first = value[0] if value and isinstance(value[0], dict) else {}
            return {"type": "array", "fields": list(first)}, len(value)
        if isinstance(value, dict):
            return {"type": "object", "fields": list(value)}, 1
        return {"type": type(value).__name__}, 1
    return {}, None


def _trim(fmt: str, text: str) -> str:
    """Keep whole leading records: a sample never ends mid-row."""
    if fmt not in {"csv", "tsv", "jsonl"}:
        return text
    lines = text.splitlines(keepends=True)
    return "".join(lines[: ROWS + 1 if fmt in {"csv", "tsv"} else ROWS])


def inspect(source: Any, path: str, limit: int = SAMPLE_BYTES) -> dict[str, Any]:
    """Head, format and schema of one input file; problems are recorded, not raised."""
    try:
        data = read_head(source, path, limit)
    except IsADirectoryError:
        return _directory(path)
    except Exception as exc:
        return _blank(path, f"读取失败：{type(exc).__name__}: {exc}"[:300])
    truncated = len(data) > limit
    data = data[:limit]
    entry = _blank(path, "只保留文件头部" if truncated else "")
    entry.update(truncated=truncated, sha256=hashlib.sha256(data).hexdigest(),
                 bytes=None if truncated else len(data))
    if b"\x00" in data:
        entry["format"] = "binary"
        entry["note"] = "二进制文件只记录大小与采样哈希"
        return entry
    text = data.decode("utf-8", errors="replace")
    if truncated:
        cut = text.rfind("\n")
        if cut >= 0:
            text = text[: cut + 1]
    fmt, delimiter = _detect(path, text)
    text = _trim(fmt, text)
    schema, rows = _shape(fmt, text, delimiter)
    entry.update(format=fmt, schema=schema, rows=rows, text=text)
    return entry


def _name(key: str, path: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:64].strip("._-") or "input"
    suffix = PurePosixPath(path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".txt"
    return f"{stem}{suffix}"


def sample_inputs(source: Any, bound: Mapping[str, Any], directory: Path,
                  *, budget: int = TOTAL_BYTES) -> dict[str, Any]:
    """Sample every root input into ``directory`` and index what was kept."""
    directory.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    used = 0
    for key in sorted(bound):
        value = bound[key]
        if not isinstance(value, str) or not value:
            continue
        remaining = budget - used
        if remaining <= 0:
            entry = _blank(value, "采样预算已用完")
            files.append({"key": key, "name": None, **entry})
            continue
        entry = inspect(source, value, min(SAMPLE_BYTES, remaining))
        text = entry.pop("text")
        name = None
        if isinstance(text, str):
            name = _name(key, value)
            (directory / name).write_text(text, encoding="utf-8")
            used += len(text.encode("utf-8"))
        files.append({"key": key, "name": name, **entry})
    index = {
        "created_at": _now(),
        "directory": str(directory),
        "index": str(directory / INDEX_NAME),
        "files": files,
        "budget": {"per_file": SAMPLE_BYTES, "total": budget, "used": used},
    }
    (directory / INDEX_NAME).write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return index


def fields(entry: Mapping[str, Any] | None) -> list[str]:
    """The fields a captured or freshly inspected entry declares, without repeats."""
    if not isinstance(entry, Mapping):
        return []
    schema = entry.get("schema")
    if not isinstance(schema, Mapping):
        return []
    raw = schema.get("columns") if "columns" in schema else schema.get("fields")
    return [str(value) for value in raw] if isinstance(raw, list) else []


def compatibility(index: Mapping[str, Any] | None, contract: Mapping[str, Any] | None,
                  observed: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Compare this run's inputs with the capture's samples and declared contract."""
    captured = {str(entry.get("key")): entry
                for entry in (index or {}).get("files") or ()}
    problems: list[str] = []
    warnings: list[str] = []
    checked: list[str] = []
    for key, actual in observed:
        declared = (contract or {}).get(key)
        declared = declared if isinstance(declared, Mapping) else {}
        actual_format = str(actual.get("format"))
        if actual_format in {"unavailable", "binary", "dir"}:
            warnings.append(f"{key}：输入无法取样（{actual.get('note')}），未做契约校验")
            continue
        expected = str(declared.get("format") or (captured.get(key) or {}).get("format") or "")
        if expected and actual_format != expected.lower():
            problems.append(f"{key}：期望 {expected}，实际 {actual_format}（{actual.get('path')}）")
            continue
        seen = set(fields(actual))
        required = [str(value) for value in declared.get("required_fields") or ()]
        missing = [field for field in required if field not in seen]
        if missing:
            problems.append(f"{key}：缺少必需字段 {missing}")
        for label, names in (("声明字段", declared.get("fields")),
                             ("采样字段", fields(captured.get(key)))):
            if not isinstance(names, list):
                continue
            absent = [str(name) for name in names if str(name) not in seen]
            if absent:
                warnings.append(f"{key}：本次输入没有出现{label} {absent}")
        if actual.get("rows") == 0:
            warnings.append(f"{key}：输入没有记录（只有表头或空文件）")
        checked.append(key)
    return {"checked": checked, "problems": problems, "warnings": warnings}
