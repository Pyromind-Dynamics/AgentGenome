"""Stage E: the inputs a capture ran on, as a replayable regression baseline.

Replay A already proved the package runs on the captured inputs, so those inputs
plus that run's artifact digests are the baseline a later revision must not
break. The cases are generated, not frozen: they only enter an asset's suite when
a revision actually promotes a changed package, so an expectation that turns out
to be too strict costs one failed revision instead of locking the asset.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

ASSERTION_LIMIT = 12
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _fixtures(samples: Mapping[str, Any], manifest: Mapping[str, Any]
              ) -> tuple[dict[str, str], dict[str, str]] | None:
    """The sampled inputs as regression fixture files, or None when incomplete."""
    directory = Path(str(samples.get("directory", "")))
    files: dict[str, str] = {}
    params: dict[str, str] = {}
    for entry in samples.get("files") or ():
        if not isinstance(entry, Mapping):
            return None
        key, name = entry.get("key"), entry.get("name")
        if entry.get("format") == "pending" and isinstance(key, str):
            # An output location the run creates: it has no fixture to keep, but it
            # is still one of the case's parameters. Without this a capture whose
            # output directory does not exist yet never gets a baseline at all.
            params[key] = f"output:{key}"
            continue
        if (not isinstance(key, str) or not isinstance(name, str)
                or entry.get("truncated") or entry.get("format") in {"unavailable", "binary"}):
            return None
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except OSError:
            return None
        path = f"inputs/{name}"
        files[path] = text
        params[key] = f"fixture:{path}"
    if not params or set(params) != set(manifest.get("parameters") or {}):
        return None
    return files, params


def _assertions(completed: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """One digest assertion per recorded artifact, capped: bytes, not behaviour."""
    assertions: list[dict[str, Any]] = []
    for entry in completed or ():
        if not isinstance(entry, Mapping):
            continue
        node, outputs = entry.get("node"), entry.get("outputs")
        if not isinstance(node, str) or not isinstance(outputs, Mapping):
            continue
        for key, artifact in sorted(outputs.items(), key=lambda item: str(item[0])):
            digest = artifact.get("sha256") if isinstance(artifact, Mapping) else None
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                continue
            assertions.append({"node": node, "output": str(key), "kind": "hash",
                               "expected": digest})
            if len(assertions) >= ASSERTION_LIMIT:
                return assertions
    return assertions


def derive(capture_id: str, samples: Mapping[str, Any] | None, package: Path,
           completed: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """The baseline this capture offers a later revision, ready or with the reason."""
    if not isinstance(samples, Mapping) or not samples.get("files"):
        return {"ready": False, "reason": "没有可用的输入采样", "cases": []}
    try:
        manifest = json.loads((Path(package) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ready": False, "reason": "包不可读，无法生成迭代基线", "cases": []}
    fixtures = _fixtures(samples, manifest)
    if fixtures is None:
        return {"ready": False,
                "reason": "输入采样不完整（文件头/二进制/读取失败），不能作为回放基线",
                "cases": []}
    files, params = fixtures
    assertions = _assertions(completed)
    if not assertions:
        return {"ready": False, "reason": "回放 A 没有记录可断言的产物摘要", "cases": []}
    return {"ready": True, "reason": None,
            "cases": [{"id": f"baseline-{capture_id[:24]}", "files": files,
                       "params": params, "assertions": assertions}]}
