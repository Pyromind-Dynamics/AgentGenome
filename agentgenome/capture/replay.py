"""Stage D: prove a captured package before it is published.

Replay A re-runs the recorded parameters through the service, so the run that
proves the capture is the same run publication accepts as evidence. Replay B runs
a different input straight through the host ports and is the only thing that can
claim the experience generalizes. Neither path rewrites a command: a failing
replay is evidence about the capture, not something to patch here.

Both replays outlive the capture call: A is queued on the host, B runs in the
finalize step of A (see ``run_probe``), so neither blocks a tool call.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from core.api import run

from ..parameters import normalize_params, parameter_spec, path_keys
from .emit import Package

if TYPE_CHECKING:
    from ..service import GenomeService, Host


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayCase:
    name: str
    status: str
    run_id: str | None
    params: tuple[tuple[str, Any], ...]
    failure: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    evidence: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "succeeded"

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "run_id": self.run_id,
            "params": dict(self.params),
            "failure": self.failure,
            "outputs": self.outputs,
            "evidence": list(self.evidence),
        }


def values(package: Package, params: Mapping[str, Any]) -> dict[str, Any]:
    expected = {key for key, _ in package.params}
    if set(params) != expected:
        raise ReplayError(
            f"参数不匹配：需要 {sorted(expected)}，收到 {sorted(params)}")
    specs = dict(package.parameter_specs)
    for key, type_name in package.param_types.items():
        specs.setdefault(
            key,
            parameter_spec(key, type_name=type_name, source=key),
        )
    try:
        return normalize_params(specs, params)
    except ValueError as exc:
        raise ReplayError(str(exc)) from exc


def probe_replay(host: Host, package: Package, params: Mapping[str, Any], *,
                 run_id: str, run_dir: str | Path, name: str = "new",
                 cancel: threading.Event | None = None,
                 emit: Callable[[dict[str, Any]], None] | None = None) -> ReplayCase:
    """Run the package outside the registry, straight through the host ports."""
    values_ = values(package, params)
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    paths = path_keys(package.parameter_specs, values_)
    ports, resolved = host.prepare(run_id, package.directory, values_,
                                   paths, cancel or threading.Event(),
                                   emit or events.append)
    if ports.artifacts is None:
        raise ReplayError("宿主没有提供 artifact 端口，无法回放验证")
    result = run(package.graph, resolved, ports, directory)
    return ReplayCase(
        name=name,
        status=str(result.status),
        run_id=None,
        params=tuple(sorted(values_.items())),
        failure=None if result.failure is None else {**result.failure, **_stopped(directory)},
        outputs=result.outputs,
        evidence=(str(directory / "ledger.jsonl"),),
    )


def _stopped(directory: Path) -> dict[str, Any]:
    """The engine reports the outermost node; the ledger keeps the failing child."""
    ledger = directory / "ledger.jsonl"
    if not ledger.is_file():
        return {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "node_failed" and isinstance(event.get("node"), str):
            return {"node": event.get("node")}
    return {}


def queue_replay(service: GenomeService, package: Package, params: Mapping[str, Any], *,
                 scope: str, request_id: str, name: str = "same") -> ReplayCase:
    """Import the draft and queue replay A; the host executes it like any run."""
    values_ = values(package, params)
    service.import_asset(package.directory)
    state = service.submit(scope, request_id, package.asset_id, package.version,
                           values_, allow_draft=True)
    return ReplayCase(name=name, status=state["status"], run_id=state["id"],
                      params=tuple(sorted(values_.items())))


def run_probe(host: Host, package_dir: str | Path, params: Mapping[str, Any], *,
              run_id: str, run_dir: str | Path,
              cancel: threading.Event | None = None,
              emit: Callable[[dict[str, Any]], None] | None = None) -> ReplayCase:
    """Replay B after the capture call returned; a broken replay is a verdict."""
    requested = tuple(sorted(params.items()))
    try:
        package = Package.load(package_dir)
        return probe_replay(host, package, params, run_id=run_id, run_dir=run_dir,
                            name="new", cancel=cancel, emit=emit)
    except Exception as exc:
        return ReplayCase(name="new", status="error", run_id=None, params=requested,
                          failure={"node": None, "summary": f"回放无法执行：{exc}"})


def read_case(service: GenomeService, scope: str, case: ReplayCase) -> ReplayCase:
    if case.run_id is None:
        return case
    state = service.status(scope, case.run_id)
    result = state.get("result") if isinstance(state.get("result"), dict) else {}
    evidence = result.get("evidence")
    return replace(
        case,
        status=state["status"],
        failure=result.get("failure"),
        outputs=result.get("outputs"),
        evidence=tuple(sorted(evidence.values())) if isinstance(evidence, dict) else case.evidence,
    )


def render_plan(probe: Mapping[str, Any] | None, run_id: str | None,
                notes: Sequence[str] = (), *, planned: bool = False) -> str:
    """What the capture queued; the verdict is appended once both replays ran."""
    same = (
        "已生成执行计划，等待用户确认"
        if planned
        else (f"已排队，run `{run_id}`" if run_id
              else "未排队（与已有版本字节一致）")
    )
    lines = [
        "### 回放计划", "",
        "- 回放 A（原参数）：" + same,
        "- 回放 B（换参数）：" + ("已排定，待回放 A 成功后执行" if probe
                                 else "未提供换参数回放数据"),
    ]
    lines.extend(f"- {note}" for note in notes)
    lines.extend(["", "回放结论在回放完成后追加到本报告。"])
    return "\n".join(lines) + "\n"
