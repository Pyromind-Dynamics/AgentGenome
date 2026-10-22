"""Capture pipeline: traces in, published experience out.

The heavy lifting lives in the stage modules; this module only wires them and
decides what to do when a stage degrades (no new parameters, failing replay).
Every degradation is written into the report, never swallowed.

Nothing here executes a graph: replay A is queued on the host and replay B runs
when that run finalizes, so a capture call returns as soon as the package is
frozen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from builder import CompileError, compile

from ..service import GenomeService
from ..parameters import ParameterError, normalize_value, parameter_spec, path_keys
from .align import DEFAULT_THRESHOLD, Alignment, align_traces, render_alignment
from .emit import (DEFAULT_VERSION, EmitError, Package, build_package, render_emit,
                   source_for, unresolved_literals)
from .replay import queue_replay, render_plan
from .sample import sample_inputs
from .slice import OUTPUT_CONTRACT_HINT, Slice, SliceError, render_slice, reverse_slice
from .trace import KIND_SHELL, Trace

_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)\Z")
_LIVE_RUNS = ("queued", "running", "cancelling", "waiting")


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class CapturePlan:
    capture_id: str
    asset_id: str
    version: str
    pin: str
    package_dir: Path
    scope: str
    run_id: str | None
    reused: bool
    generalized: bool | None
    notes: tuple[str, ...]
    report_path: Path
    samples: dict[str, Any] | None = None
    plan_id: str | None = None
    plan_digest: str | None = None
    requires_confirmation: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "asset_id": self.asset_id,
            "version": self.version,
            "pin": self.pin,
            "scope": self.scope,
            "run_id": self.run_id,
            "reused": self.reused,
            "generalized": self.generalized,
            "notes": list(self.notes),
            "report": str(self.report_path),
            "package": str(self.package_dir),
            "samples": (self.samples or {}).get("index"),
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "requires_confirmation": self.requires_confirmation,
        }


def next_version(versions: Sequence[str]) -> str:
    parsed = [tuple(int(part) for part in match.groups())
              for value in versions if (match := _SEMVER.fullmatch(value))]
    if not parsed:
        raise CaptureError("无法自动递增版本，请显式指定 version")
    major, minor, patch = max(parsed)
    return f"{major}.{minor}.{patch + 1}"


def _header(capture_id: str, label: str, package: Package, traces: Sequence[Trace]) -> str:
    ids = "、".join(f"`{trace.trace_id}`" for trace in traces)
    return "\n".join([
        "# 轨迹沉淀报告", "",
        f"- capture：`{capture_id}`",
        f"- 经验名：{label}",
        f"- 资产：`{package.asset_id}` v{package.version}（owner {package.owner}）",
        f"- 轨迹：{ids}",
        f"- 步骤：{'、'.join(f'{trace.trace_id} {len(trace.steps)} 条' for trace in traces)}",
        "",
    ])


def _report(capture_id: str, label: str, package: Package, traces: Sequence[Trace],
            alignment: Alignment, slice_: Slice, plan: str,
            samples: Mapping[str, Any] | None = None) -> str:
    sections = [render_alignment(alignment), render_slice(slice_, alignment.slots),
                render_emit(package)]
    if samples:
        sections.append(render_samples(samples))
    return "".join([_header(capture_id, label, package, traces), "\n",
                    "\n".join(sections), "\n", plan])


def render_samples(samples: Mapping[str, Any]) -> str:
    """The sampled inputs, as the report tells them: what was kept and what was not."""
    lines = ["### 输入采样", ""]
    if samples.get("error"):
        lines.append(f"- 采样失败：{samples['error']}")
    files = samples.get("files") or ()
    if not files:
        lines.append("- 本条链路没有可采样的根输入")
    for entry in files:
        schema = entry.get("schema") or {}
        names = schema.get("columns") or schema.get("fields") or []
        detail = "、".join(f"`{name}`" for name in names)
        size = "大小未知" if entry.get("bytes") is None else f"{entry['bytes']} B"
        lines.append(
            f"- `{entry.get('key')}` ← `{entry.get('path')}`：{entry.get('format')}"
            + (f"，字段 {detail}" if detail else "")
            + f"，{size}"
            + ("，只采样文件头部" if entry.get("truncated") else "")
            + (f"，{entry['note']}" if entry.get("note") else ""))
    lines.extend(["", f"- 采样索引：`{samples.get('index')}`", ""])
    return "\n".join(lines)


def _samples(workspace: Any, bound: Mapping[str, Any], directory: Path,
             path_names: Sequence[str] | None = None) -> dict[str, Any]:
    """Sampling is evidence: a broken input is reported, never raised."""
    selected = (
        {key: value for key, value in bound.items() if key in path_names}
        if path_names is not None
        else bound
    )
    try:
        return sample_inputs(source_for(workspace), selected, directory)
    except Exception as exc:
        return {"index": None, "files": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


def _failure_report(capture_id: str, label: str, traces: Sequence[Trace],
                    alignment: Alignment, slice_: Slice | None, package_dir: Path,
                    error: str) -> str:
    """The report a failed build leaves behind: what was planned, and why it stopped."""
    lines = [
        "# 轨迹沉淀报告（构建失败）", "",
        f"- capture：`{capture_id}`",
        f"- 经验名：{label}",
        f"- 轨迹：{'、'.join(f'`{trace.trace_id}`' for trace in traces)}",
        f"- 草稿：`{package_dir}`",
        f"- 错误：{error}",
        f"- 契约：{OUTPUT_CONTRACT_HINT}",
        "",
        render_alignment(alignment),
        "",
    ]
    lines.append("反向切片未生成：最终产物无法回溯。\n" if slice_ is None
                 else render_slice(slice_, alignment.slots) + "\n")
    return "\n".join(lines)


def _probe_plan(package: Package, new_params: Mapping[str, Any] | None,
                bound: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Replay B's parameters, or None plus the note explaining why there is none."""
    allowed = {key for key, _ in package.params}
    specs = getattr(package, "parameter_specs", {})
    requested: dict[str, Any] = {}
    for key, value in (new_params or {}).items():
        if key not in allowed:
            continue
        spec = specs.get(key)
        if spec is None:
            spec = parameter_spec(key, type_name="path", source=key)
        requested[key] = normalize_value(spec.type, value, key=key)
    if not requested:
        return None, "未提供换参数回放数据，本条经验未经泛化验证"
    if "human" in package.graph.node.needs:
        return None, "链路含人工门禁，回放 B 无法自动恢复，跳过换参数回放并按窄口径发布"
    dropped = sorted({key for key in (new_params or {})} - allowed)
    note = "换参数回放将在回放 A 成功后由服务执行"
    if dropped:
        note += f"（忽略已物化的参数：{'、'.join(f'`{key}`' for key in dropped)}）"
    if requested == dict(bound):
        return None, "换参数回放与原参数相同，不构成泛化证据"
    return requested, note


def _require_solidified(package: Package) -> None:
    """A chain is either frozen whole or refused; a half-covered graph is not an asset."""
    if not package.not_solidifiable:
        return
    dropped = "；".join(f"#{order} {reason}"
                        for order, reason in package.not_solidifiable)
    raise EmitError(
        f"链路里有无法固化的步骤：{dropped}。"
        "历史经验只收完整固化的链路，先把这些步骤变成可执行节点再沉淀："
        "固定命令用 run、宿主工具调用用 tool、agent 裁量用 llm、人工门禁用 human"
        "（都用 genome_distill 的 steps 提交）；"
        "如果这些步骤不在最终产物的依赖里，用 final_artifact 重新切片；"
        "如果它们是本次任务特有的输入，把它们声明成 `{{参数名}}` 输入边界（配 params）。")


def _forbid_replay_literals(traces: Sequence[Trace], workspace: Any) -> None:
    """A captured command may not carry a workspace-relative literal into the graph.

    The graph replays a command with the package as its working directory, so a flag
    value the parser could not classify (`--images-root storage`) has to become a
    parameter instead of a literal that only resolves in the session's own cwd.
    """
    source = source_for(workspace)
    for trace in traces:
        for step in trace.steps:
            if step.kind != KIND_SHELL or not step.command:
                continue
            for flag, value in unresolved_literals(source, step.command):
                raise EmitError(
                    f"步骤 `{trace.trace_id}:{step.seq}` 的命令里 `{flag} {value}` "
                    "没有被识别成路径，但它在会话工作目录里真实存在：回放的工作目录是包目录"
                    "而不是会话目录，这个字面量在回放时会指向不存在的位置。"
                    "请用 genome_distill 提交这条链路，把这类值写成 `{参数名}` 占位符"
                    "并在 params 里给出它在会话里的真实值。")


def _replay_restartable(service: GenomeService, previous: Mapping[str, Any]) -> bool:
    """A capture whose replay died may queue again; a live one must not be duplicated."""
    if previous.get("published"):
        return False
    run_id, scope = previous.get("run_id"), previous.get("scope")
    if isinstance(run_id, str) and isinstance(scope, str):
        try:
            return service.status(scope, run_id).get("status") not in _LIVE_RUNS
        except ValueError:
            return True
    return previous.get("run_status") not in _LIVE_RUNS


def prepare_capture(service: GenomeService, *, traces: Sequence[Trace], label: str,
                    workspace: Any, scope: str, final_artifact: str | None = None,
                    threshold: float = DEFAULT_THRESHOLD, new_params: Mapping[str, Any] | None = None,
                    owner: str = "local", description: str | None = None,
                    version: str | None = None, capture_id: str | None = None,
                    asset_id: str | None = None,
                    declarations: Mapping[str, Any] | None = None,
                    replay_params: Mapping[str, Any] | None = None,
                    bindings: Mapping[str, Any] | None = None,
                    enqueue: bool = True,
                    allow_side_effects: bool = False,
                    request_id: str | None = None,
                    execution_scope: Mapping[str, Any] | None = None) -> CapturePlan:
    traces = list(traces)
    if not traces:
        raise CaptureError("至少需要一条轨迹")
    truncated = [f"{trace.trace_id} ({trace.export_dropped_steps})" for trace in traces
                 if trace.export_dropped_steps]
    if truncated:
        raise CaptureError("轨迹导出不完整（dropped_steps）："
                           + ", ".join(truncated)
                           + "；请重新导出完整会话后再沉淀整条链路")
    capture_id = capture_id or uuid4().hex
    directory = service.root / "captures" / capture_id
    alignment = align_traces(traces, threshold)
    package_dir = directory / "package"
    slice_: Slice | None = None
    queue_allowed = enqueue

    def build(candidate: str) -> Package:
        assert slice_ is not None
        return build_package(package_dir, alignment=alignment, slice_=slice_, traces=traces,
                             label=label, workspace=workspace, owner=owner,
                             version=candidate, description=description, asset_id=asset_id,
                             declarations=declarations, bindings=bindings)

    try:
        slice_ = reverse_slice(alignment, final_artifact, bindings)
        package = build(version or DEFAULT_VERSION)
        _forbid_replay_literals(traces, workspace)
        _require_solidified(package)
        if (
            service._requires_tool(compile(package.directory).node)
            and not allow_side_effects
        ):
            queue_allowed = False
    except (SliceError, EmitError, CompileError, ParameterError) as exc:
        report = _failure_report(capture_id, label, traces, alignment, slice_,
                                 package_dir, str(exc))
        service.captures.failed(
            capture_id, asset_id=asset_id or "", label=label, report=report,
            error=str(exc), traces=[trace.trace_id for trace in traces],
            package=package_dir)
        raise CaptureError(
            f"capture {capture_id} 构建失败：{exc}"
            f"（草稿：{package_dir}；报告见 genome_capture_status capture_id={capture_id}）"
        ) from exc
    versions = {item["version"]: item for item in service.versions(package.asset_id)}
    existing = {name: item["pin"] for name, item in versions.items()}

    def planned(candidate: Package, bound: Mapping[str, Any],
                samples: Mapping[str, Any], notes: tuple[str, ...],
                probe: dict[str, str] | None, reused: bool) -> CapturePlan:
        service.import_asset(candidate.directory)
        report = _report(capture_id, label, candidate, traces, alignment, slice_,
                         render_plan(probe, None, notes, planned=True), samples)
        state = service.captures.create(
            capture_id, asset_id=candidate.asset_id, version=candidate.version,
            pin=candidate.pin, scope=scope, run_id=None, package=candidate.directory,
            report=report, traces=[trace.trace_id for trace in traces],
            generalized=False if probe is None else None, probe=probe, notes=notes,
            reused=reused, samples=dict(samples), planned=True,
        )
        asset = service.get(candidate.asset_id, candidate.version)
        plan = service.plans.prepare(
            scope=scope,
            request_id=request_id or capture_id,
            kind="capture",
            asset_id=candidate.asset_id,
            version=candidate.version,
            params=dict(bound),
            source=workspace,
            requested_scope=dict(execution_scope) if execution_scope else None,
            capture_id=capture_id,
        )
        state = service.captures.attach_plan(
            capture_id,
            plan_id=plan["plan_id"],
            digest=plan["digest"],
        )
        return CapturePlan(
            capture_id, candidate.asset_id, candidate.version, candidate.pin,
            candidate.directory, scope, None, reused, state["generalized"],
            notes, Path(state["report"]), state.get("samples"), plan["plan_id"],
            plan["digest"], True,
        )

    if existing.get(package.version) == package.pin:
        previous = service.captures.for_asset(package.asset_id, package.version) or {}
        if (not versions[package.version]["published"]
                and _replay_restartable(service, previous)):
            bound = dict(replay_params) if replay_params is not None else dict(package.params)
            samples = _samples(
                workspace,
                bound,
                directory / "samples",
                path_keys(package.parameter_specs, bound),
            )
            probe, probe_note = _probe_plan(package, new_params, bound)
            if queue_allowed:
                # The bytes are already frozen but never passed validation, so the same
                # plan may run again instead of returning a reused capture with no run.
                notes = ("与已有版本字节一致，未产生新版本；上一次回放未通过，按原参数重新排队",
                         probe_note)
                try:
                    case = queue_replay(service, package, bound, scope=scope,
                                        request_id=capture_id, name="same")
                except ValueError as exc:
                    raise CaptureError(f"草稿导入失败：{exc}") from exc
                report = _report(capture_id, label, package, traces, alignment, slice_,
                                 render_plan(probe, case.run_id, notes), samples)
                state = service.captures.create(
                    capture_id, asset_id=package.asset_id, version=package.version,
                    pin=package.pin, scope=scope, run_id=case.run_id,
                    package=package.directory, report=report,
                    traces=[trace.trace_id for trace in traces],
                    generalized=False if probe is None else None, probe=probe,
                    notes=notes, samples=dict(samples))
                return CapturePlan(capture_id, package.asset_id, package.version,
                                   package.pin, package.directory, scope, case.run_id,
                                   True, state["generalized"], notes,
                                   Path(state["report"]), state.get("samples"))
            notes = (
                "与已有版本字节一致，未产生新版本；执行计划已生成，等待用户确认",
                probe_note,
            )
            return planned(package, bound, samples, notes, probe, True)
        notes = ("与已有版本字节一致，未产生新版本",)
        report = _report(capture_id, label, package, traces, alignment, slice_,
                         render_plan(None, None, notes))
        state = service.captures.create(
            capture_id, asset_id=package.asset_id, version=package.version, pin=package.pin,
            scope=scope, run_id=None, package=package.directory, report=report,
            traces=[trace.trace_id for trace in traces],
            generalized=bool(previous.get("generalized")),
            probe=None, notes=notes, reused=True,
            published=bool(versions[package.version]["published"]),
            samples=previous.get("samples"))
        return CapturePlan(capture_id, package.asset_id, package.version, package.pin,
                           package.directory, scope, None, True, state["generalized"],
                           notes, Path(state["report"]), state.get("samples"))
    if version is None and package.version in existing:
        package = build(next_version(list(existing)))

    bound = dict(replay_params) if replay_params is not None else dict(package.params)
    samples = _samples(
        workspace,
        bound,
        directory / "samples",
        path_keys(package.parameter_specs, bound),
    )
    probe, note = _probe_plan(package, new_params, bound)
    notes = (note,)
    if not queue_allowed:
        if "tool" in compile(package.directory).node.needs:
            notes = (
                "链路包含宿主工具副作用，已只生成执行计划，未自动回放",
                *notes,
            )
        else:
            notes = ("执行计划已生成，等待用户确认后回放", *notes)
        return planned(package, bound, samples, notes, probe, False)
    try:
        case = queue_replay(service, package, bound, scope=scope,
                            request_id=capture_id, name="same")
    except ValueError as exc:
        raise CaptureError(f"草稿导入失败：{exc}") from exc
    report = _report(capture_id, label, package, traces, alignment, slice_,
                     render_plan(probe, case.run_id), samples)
    state = service.captures.create(
        capture_id, asset_id=package.asset_id, version=package.version, pin=package.pin,
        scope=scope, run_id=case.run_id, package=package.directory, report=report,
        traces=[trace.trace_id for trace in traces],
        generalized=False if probe is None else None, probe=probe, notes=notes,
        samples=samples)
    return CapturePlan(capture_id, package.asset_id, package.version, package.pin,
                       package.directory, scope, case.run_id, False, state["generalized"],
                       notes, Path(state["report"]), samples)
