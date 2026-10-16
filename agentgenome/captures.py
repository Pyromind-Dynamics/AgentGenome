"""Capture registry: what a capture produced, where its report lives, and the
publication decision that follows its validation run.

Capture metadata stays next to the asset registry instead of inside the package:
the package bytes are what the validation run executed, and rewriting them after
the run would invalidate the evidence publication requires.

Replay B (the generalization probe) belongs to this finalize step, not to the
capture call: the tool returns while replay A is still queued, and the run that
proves the capture is the thread that also runs the probe.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .capture.baseline import derive
from .capture.replay import run_probe

if TYPE_CHECKING:
    from .service import GenomeService

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_TERMINAL = {"published", "draft", "reused"}
_REPORT_TEXT_LIMIT = 16 * 1024
_PREVIEW_TEXT = 2 * 1024
_PREVIEW_TOTAL = 12 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Captures:
    def __init__(self, service: "GenomeService") -> None:
        self.service = service
        self.root = service.root / "captures"
        self._lock = threading.RLock()

    @staticmethod
    def _check(capture_id: str) -> str:
        if not _ID.fullmatch(capture_id):
            raise ValueError("invalid capture id")
        return capture_id

    def directory(self, capture_id: str) -> Path:
        return self.root / self._check(capture_id)

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8")

    def create(self, capture_id: str, *, asset_id: str, version: str, pin: str, scope: str,
               run_id: str | None, package: Path, report: str, traces: list[str],
               generalized: bool | None, probe: dict[str, str] | None,
               notes: tuple[str, ...] = (), reused: bool = False,
               published: bool = False,
               samples: dict[str, Any] | None = None) -> dict[str, Any]:
        directory = self.directory(capture_id)
        report_path = directory / "report.md"
        state = {
            "capture_id": capture_id,
            "asset_id": asset_id,
            "version": version,
            "pin": pin,
            "scope": scope,
            "run_id": run_id,
            "package": str(package),
            "report": str(report_path),
            "traces": traces,
            "generalized": generalized,
            "probe": probe,
            "samples": samples,
            "notes": list(notes),
            "replay": {"same_params": None, "new_params": None,
                       "generalized": generalized, "notes": list(notes)},
            "reused": reused,
            "stage": "reused" if reused else "replaying",
            "published": published,
            "run_status": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        self._write(directory / "state.json", state)
        if run_id:
            self._write(self.root / "by-run" / f"{run_id}.json", {"capture_id": capture_id})
        self._write(self.root / "assets" / asset_id / f"{version}.json", {"capture_id": capture_id})
        return state

    def failed(self, capture_id: str, *, asset_id: str, label: str, report: str,
               error: str, traces: list[str], package: Path) -> dict[str, Any]:
        """Record a capture that never reached a package, so status can explain it.

        The build fails before anything is queued, so there is no run to finalize and
        no asset version to point at; the report and the draft directory are the whole
        output, and both have to survive the failing call.
        """
        directory = self.directory(capture_id)
        report_path = directory / "report.md"
        state = {
            "capture_id": capture_id,
            "asset_id": asset_id,
            "label": label,
            "version": None,
            "pin": None,
            "scope": None,
            "run_id": None,
            "package": str(package),
            "report": str(report_path),
            "traces": list(traces),
            "generalized": None,
            "probe": None,
            "samples": None,
            "notes": [error],
            "replay": {"same_params": None, "new_params": None, "generalized": None,
                       "notes": []},
            "reused": False,
            "stage": "failed",
            "published": False,
            "run_status": None,
            "error": error,
            "created_at": _now(),
            "updated_at": _now(),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        self._write(directory / "state.json", state)
        return state

    def state(self, capture_id: str) -> dict[str, Any]:
        path = self.directory(capture_id) / "state.json"
        if not path.is_file():
            raise ValueError("capture not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _pointed(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        return self.state(pointer["capture_id"])

    def for_run(self, run_id: str) -> dict[str, Any] | None:
        return self._pointed(self.root / "by-run" / f"{run_id}.json")

    def for_asset(self, asset_id: str, version: str) -> dict[str, Any] | None:
        return self._pointed(self.root / "assets" / asset_id / f"{version}.json")

    def sample_index(self, asset_id: str, version: str) -> dict[str, Any] | None:
        """The capture's sampled inputs, keyed by the asset version they belong to."""
        capture = self.for_asset(asset_id, version)
        samples = (capture or {}).get("samples")
        return samples if isinstance(samples, dict) else None

    def has_samples(self, asset_id: str, version: str) -> bool:
        return bool(self.sample_index(asset_id, version))

    def baseline(self, asset_id: str, version: str) -> dict[str, Any] | None:
        """The generated iteration baseline a capture offers, without its fixtures."""
        capture = self.for_asset(asset_id, version)
        baseline = (capture or {}).get("baseline")
        return baseline if isinstance(baseline, dict) else None

    def baseline_cases(self, asset_id: str, version: str) -> list[dict[str, Any]] | None:
        baseline = self.baseline(asset_id, version) or {}
        path = baseline.get("path")
        if not baseline.get("ready") or not isinstance(path, str):
            return None
        try:
            cases = json.loads(Path(path).read_text(encoding="utf-8")).get("cases")
        except (OSError, ValueError, AttributeError):
            return None
        return cases if isinstance(cases, list) else None

    def sample_preview(self, asset_id: str, version: str) -> dict[str, Any] | None:
        """What genome_get shows: sampled schema plus a bounded text preview."""
        capture = self.for_asset(asset_id, version)
        samples = (capture or {}).get("samples")
        if not isinstance(samples, dict):
            return None
        directory = Path(str(samples.get("directory", "")))
        budget = _PREVIEW_TOTAL
        files = []
        for entry in samples.get("files") or ():
            if not isinstance(entry, dict):
                continue
            item = {key: entry.get(key) for key in
                    ("key", "path", "format", "bytes", "truncated", "rows", "schema", "note")}
            item["preview"] = None
            name = entry.get("name")
            if isinstance(name, str) and budget > 0:
                try:
                    text = (directory / name).read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text:
                    take = min(_PREVIEW_TEXT, budget)
                    item["preview"] = text[:take] + ("…[采样已截断]" if len(text) > take else "")
                    budget -= min(len(text), take)
            files.append(item)
        return {"capture_id": (capture or {}).get("capture_id"),
                "index": samples.get("index"), "files": files}

    def status(self, capture_id: str) -> dict[str, Any]:
        state = self.state(capture_id)
        if not state["run_id"]:
            return self._with_report(state)
        try:
            run = self.service.status(state["scope"], state["run_id"])
        except ValueError:
            return self._with_report(
                {**state, "run": {"id": state["run_id"], "status": "unknown"}}
            )
        return self._with_report({**state, "run": run})

    def _with_report(self, state: dict[str, Any]) -> dict[str, Any]:
        """Attach the report body so the agent reads it from the tool result.

        The capture root sits outside the conversation workspace Pi file tools are
        scoped to, so the verdict has to travel with the state.
        """
        path = state.get("report")
        if not isinstance(path, str):
            return state
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return state
        if len(text) > _REPORT_TEXT_LIMIT:
            text = f"{text[:_REPORT_TEXT_LIMIT]}\n…[报告过长已截断]"
        return {**state, "report_text": text}

    def finish(self, capture_id: str, *, status: str, result: dict[str, Any],
               host: Any = None, emit: Any = None,
               cancel: threading.Event | None = None) -> dict[str, Any]:
        """Decide the capture's fate after replay A, including the generalization probe."""
        with self._lock:
            state = self.state(capture_id)
            if state["stage"] in _TERMINAL:
                return self._with_report(state)
            if status == "waiting":
                # Replay A parked at a human node. Nothing is decided yet: the same
                # capture finalizes when the resumed run reports back.
                state.update(stage="waiting", run_status=status, updated_at=_now())
                self._write(self.directory(capture_id) / "state.json", state)
                self._append_report(capture_id, self._verdict(state, result))
                return self._with_report(state)
            if status == "succeeded" and state.get("probe"):
                state = self._probe(capture_id, state, host, emit, cancel)
            if status == "succeeded":
                state = self._baseline(capture_id, state, result)
            if cancel is not None and cancel.is_set():
                status = "cancelled"
            published, error = False, None
            if status == "succeeded":
                try:
                    published = bool(self.service.publish(
                        state["asset_id"], state["version"], evidence=state["run_id"])["published"])
                except ValueError as exc:
                    error = str(exc)
            stage = "published" if published else "draft"
            state.update(stage=stage, published=published, run_status=status,
                         error=error, updated_at=_now())
            self._write(self.directory(capture_id) / "state.json", state)
            self._append_report(capture_id, self._verdict(state, result))
            return self._with_report(state)

    def _baseline(self, capture_id: str, state: dict[str, Any],
                  result: dict[str, Any]) -> dict[str, Any]:
        """Give the capture's own inputs a second life: a baseline a revision can replay."""
        baseline = derive(capture_id, state.get("samples"), Path(str(state.get("package", ""))),
                          result.get("completed"))
        path = self.directory(capture_id) / "baseline.json"
        self._write(path, baseline)
        summary = {"ready": bool(baseline.get("ready")), "reason": baseline.get("reason"),
                   "path": str(path),
                   "cases": len(baseline.get("cases") or ())}
        return {**state, "baseline": summary, "updated_at": _now()}

    def _probe(self, capture_id: str, state: dict[str, Any], host: Any,
               emit: Any, cancel: threading.Event | None) -> dict[str, Any]:
        if host is None:
            raise ValueError("宿主没有提供回放能力，无法执行换参数回放")
        state = {**state, "stage": "probing", "updated_at": _now()}
        self._write(self.directory(capture_id) / "state.json", state)
        case = run_probe(host, state["package"], state["probe"], run_id=f"{capture_id}-b",
                         run_dir=self.directory(capture_id) / "replay" / "new",
                         cancel=cancel or threading.Event(), emit=emit)
        notes = () if case.passed else ("换参数回放未通过，只能作为窄口径经验发布",)
        state.update(
            generalized=case.passed,
            replay={**(state.get("replay") or {}), "new_params": case.payload(),
                    "generalized": case.passed, "notes": list(notes)},
            notes=list(notes), updated_at=_now())
        self._write(self.directory(capture_id) / "state.json", state)
        return state

    @staticmethod
    def _verdict(state: dict[str, Any], result: dict[str, Any]) -> str:
        replay = state.get("replay") or {}
        case = replay.get("new_params")
        lines = ["### 回放验证与发布", ""]
        if case is None:
            skipped = state.get("probe") and state["run_status"] != "succeeded"
            lines.append("- 回放 B（换参数）："
                         + ("未执行（回放 A 未成功）" if skipped else "未提供换参数回放数据"))
        else:
            lines.append(f"- 回放 B（换参数）：{case['status']}")
            failure = case.get("failure")
            if isinstance(failure, dict):
                lines.append(f"  - 失败节点 `{failure.get('node')}`：{failure.get('summary')}")
            for path in case.get("evidence") or ():
                lines.append(f"  - 证据：`{path}`")
        lines.extend(f"- {note}" for note in replay.get("notes") or ())
        lines.append(f"- 回放 A（原参数）：{state['run_status']}"
                     + (f"，run `{state['run_id']}`" if state["run_id"] else ""))
        waiting = result.get("waiting")
        if isinstance(waiting, dict):
            lines.append(f"  - 停在人工节点 `{waiting.get('node')}`，等待复核："
                         f"{waiting.get('question')}")
            lines.append("  - 人工完成后调用 genome_resume 继续同一条回放，"
                         "本条经验在回放成功前不会发布")
        failure = result.get("failure")
        if isinstance(failure, dict):
            lines.append(f"  - 失败节点 `{failure.get('node')}`：{failure.get('summary')}")
        remaining = result.get("remaining")
        if isinstance(remaining, list):
            stopped = next((item for item in remaining if isinstance(item, str) and "." in item), None)
            if stopped:
                lines.append(f"  - 停在节点 `{stopped}`")
        evidence = result.get("evidence")
        if isinstance(evidence, dict):
            lines.extend(f"  - 证据：`{path}`" for _, path in sorted(evidence.items()))
        outputs = result.get("outputs")
        if isinstance(outputs, dict) and outputs:
            lines.append(f"- 产物：{', '.join(f'`{key}`' for key in sorted(outputs))}")
        if state["published"]:
            lines.append(f"- 已发布：`{state['asset_id']}` v{state['version']}（pin `{state['pin'][:12]}`）")
            lines.append(f"- 泛化：{'通过' if state['generalized'] else '未通过，仅窄口径复用'}")
        else:
            reason = state.get("error") or {
                "failed": "回放 A 未通过",
                "waiting": "回放 A 停在人工节点，等待复核后 resume",
            }.get(state["run_status"], f"回放 A {state['run_status']}")
            lines.append(f"- 未发布，保持草稿：{reason}")
            lines.append(f"- 草稿包：`{state['package']}`")
        baseline = state.get("baseline")
        if isinstance(baseline, dict):
            if baseline.get("ready"):
                lines.append(f"- 迭代基线：已生成（{baseline.get('cases', 0)} 个用例），"
                             "改脚本的修订会直接用它")
            else:
                lines.append(f"- 迭代基线：未生成（{baseline.get('reason')}）")
        return "\n".join(lines) + "\n"

    def _append_report(self, capture_id: str, section: str) -> None:
        path = self.directory(capture_id) / "report.md"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n" + section)
