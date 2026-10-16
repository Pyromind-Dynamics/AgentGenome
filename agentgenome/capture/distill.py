"""Stage 0: an explicit plan in, a capture-ready trace out.

The mechanical stages only solidify commands whose inputs and outputs are on the
command line, so a session full of probes, inline code and script-internal writes
cannot be captured as it stands. Distilling is the agent's job: it reads the
session, decides which steps are the business chain, materialises the scripts on
disk and declares every path on the command line. This module holds the result to
the same contract the capture stages enforce, and says what to fix instead of
half-building a graph.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from builder import CompileError

from ..service import GenomeService
from .align import DEFAULT_THRESHOLD, Alignment, align_traces, render_alignment
from .emit import (DEFAULT_VERSION, EmitError, Package, build_package, read_many,
                   render_emit, source_for, unresolved_literals)
from .policy import POLICY_SCRIPTS_FIRST, normalize_policy
from .slice import OUTPUT_CONTRACT_HINT, Slice, SliceError, render_slice, reverse_slice
from .trace import (INLINE_MARK, KIND_HUMAN, KIND_LLM, KIND_SHELL, KIND_TOOL, KIND_WRITE,
                    ROLE_SCRIPT, TRACE_VERSION, Command, Trace, arg_paths, canonical_path,
                    capture_trace, parse_command, placeholder_key, probe_command, role)

MAX_STEPS = 200
KIND_RUN = "run"
AUXILIARY_REASON = ("产出没有被链路里的后续步骤消费，不在最终产物的依赖闭包里；"
                    "要固化它，请单独用 final_artifact 指向它的产出再沉淀一条经验")
_TOKEN_BOUNDARY = r"(?<![\w./-])"
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")


class DistillError(ValueError):
    pass


@dataclass(frozen=True)
class DistilledStep:
    order: int
    run: str
    derived_from: str | None
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    kind: str = KIND_RUN
    verify: str | None = None
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step": self.order,
            "kind": self.kind,
            "from": self.derived_from,
            "reads": list(self.reads),
            "writes": list(self.writes),
        }
        if self.kind == KIND_TOOL:
            data["tool"] = self.tool_name
            data["args"] = dict(self.arguments or {})
        else:
            data["run"] = self.run
        if self.verify:
            data["verify"] = self.verify
        return data


def _run_text(raw: Mapping[str, Any], order: int) -> str:
    value = raw.get("run")
    if not isinstance(value, str) or not value.strip():
        raise DistillError(f"第 {order} 步缺少 run：要执行的完整 shell 命令")
    return value.strip()


def _derived_from(raw: Mapping[str, Any], order: int) -> str | None:
    derived = raw.get("from")
    if derived is None:
        return None
    if not isinstance(derived, str) or not derived.strip():
        raise DistillError(
            f"第 {order} 步的 from 必须是非空字符串（来源轨迹，如 `<trace_id>:<seq>`）")
    return derived.strip()


def _llm_text(raw: Mapping[str, Any], order: int) -> str:
    value = raw.get("llm")
    if not isinstance(value, str) or not value.strip():
        raise DistillError(f"第 {order} 步的 llm 必须是非空的提示词模板字符串")
    return value.strip()


def _llm_io(raw: Mapping[str, Any], order: int,
            label: str = "llm") -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The declared inputs and the single artifact of a distilled prompt step.

    A shell step derives these from its command line; a prompt or a question has no such
    structure, so the agent declares them and the same chain/clone checks apply.
    """
    output = raw.get("output")
    if not isinstance(output, str) or not output.strip():
        raise DistillError(
            f"第 {order} 步是 {label} 节点，必须用 output 声明它产出的 artifact 路径，"
            "并在提示词里原样引用同一个路径")
    inputs = raw.get("input")
    if inputs is None:
        inputs = []
    elif isinstance(inputs, str):
        inputs = [inputs]
    if (not isinstance(inputs, list)
            or any(not isinstance(item, str) or not item.strip() for item in inputs)):
        raise DistillError(f"第 {order} 步的 input 必须是路径字符串列表（可省略）")
    return tuple(item.strip() for item in inputs), (output.strip(),)


def _human_text(raw: Mapping[str, Any], order: int) -> str:
    value = raw.get("human")
    if not isinstance(value, str) or not value.strip():
        raise DistillError(
            f"第 {order} 步的 human 必须是非空的问题模板字符串：交给人的工作单")
    return value.strip()


def _llm_verify(raw: Mapping[str, Any], order: int) -> str | None:
    value = raw.get("verify")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DistillError(f"第 {order} 步的 verify 必须是非空的验收命令字符串")
    return value.strip()


def _check_llm_prompt(step: DistilledStep) -> None:
    """Every declared path has to appear in the prompt so emit can slot it in."""
    missing = [token for token in (*step.reads, *step.writes) if token not in step.run]
    if missing:
        raise DistillError(
            f"第 {step.order} 步的 {step.kind} 提示词必须原样出现它声明的输入/产出路径，缺少："
            + "、".join(f"`{token}`" for token in missing))


def _tool_name(raw: Mapping[str, Any], order: int) -> str:
    value = raw.get("tool")
    if not isinstance(value, str) or not value.strip():
        raise DistillError(f"第 {order} 步的 tool 必须是非空的宿主工具名")
    name = value.strip()
    if not _TOOL_NAME.match(name):
        raise DistillError(
            f"第 {order} 步的 tool 名 `{name}` 不合法：只允许小写字母开头，"
            "其余是小写字母、数字、`.`、`_`、`-`")
    return name


def _tool_args(raw: Mapping[str, Any], order: int) -> dict[str, Any]:
    args = raw.get("args")
    if args is None:
        return {}
    if not isinstance(args, Mapping):
        raise DistillError(f"第 {order} 步的 args 必须是对象：工具收到的参数键值")
    return {str(key): value for key, value in args.items()}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _tool_io(raw: Mapping[str, Any], order: int) -> tuple[tuple[str, ...], str, dict[str, Any]]:
    """A tool step's declared inputs, its artifact, and the call's args.

    The args are values rather than a command line, so the inputs are read off them; an
    explicit `input` declaration is checked against the args and kept first, so an
    id-shaped parameter that does not spell like a path can still become a graph input.
    """
    output = raw.get("output")
    if not isinstance(output, str) or not output.strip():
        raise DistillError(
            f"第 {order} 步是 tool 节点，必须用 output 声明工具返回的 JSON 写进哪个 artifact 路径")
    declared = raw.get("input")
    if declared is None:
        declared = []
    elif isinstance(declared, str):
        declared = [declared]
    if (not isinstance(declared, list)
            or any(not isinstance(item, str) or not item.strip() for item in declared)):
        raise DistillError(f"第 {order} 步的 input 必须是路径字符串列表（可省略）")
    args = _tool_args(raw, order)
    values = _strings(args)
    reads = [item.strip() for item in declared]
    missing = [token for token in reads if not any(token in value for value in values)]
    if missing:
        raise DistillError(
            f"第 {order} 步声明了 input，但 args 里找不到它们："
            + "、".join(f"`{token}`" for token in missing)
            + "。工具只会收到 args 里的值，请把输入写进 args")
    for path in arg_paths(args):
        if path not in reads:
            reads.append(path)
    return tuple(reads), output.strip(), args


def _check_command(run: str, command: Command, order: int) -> None:
    if INLINE_MARK in command.shape:
        raise DistillError(
            f"第 {order} 步是内联代码，无法机械固化：`{run}`。"
            "请把代码落盘成脚本文件，再用命令行调用它；heredoc 与 -c 都不算落盘")
    if probe_command(command):
        raise DistillError(
            f"第 {order} 步是只读探测命令，不会进图：`{run}`。请删掉它，只留业务链路")
    if not command.read_paths and not command.write_paths:
        raise DistillError(
            f"第 {order} 步既没有输入也没有产出（命令里没有可识别的路径）：`{run}`。"
            "请把输入和产出写成命令行上的路径")


def plan_steps(steps: Sequence[Any]) -> tuple[DistilledStep, ...]:
    """Validate the agent's chain against the capture contract, step by step."""
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence) or not steps:
        raise DistillError(
            "distill 需要 steps：[{run, from?}]、[{llm, output, input?, from?}]、"
            "[{tool, args, output, input?, from?}] 或 [{human, output, input?, from?}]，"
            "按链路顺序排列")
    if len(steps) > MAX_STEPS:
        raise DistillError(f"步骤过多（{len(steps)}），最多 {MAX_STEPS} 步")
    planned: list[DistilledStep] = []
    for order, raw in enumerate(steps, start=1):
        if not isinstance(raw, Mapping):
            raise DistillError(f"第 {order} 步必须是对象：{{run, from?}}、"
                               f"{{llm, output, input?, from?}}、{{tool, args, output, from?}} "
                               f"或 {{human, output, input?, from?}}")
        forms = [key for key in ("run", "llm", "tool", "human") if key in raw]
        if len(forms) > 1:
            raise DistillError(
                f"第 {order} 步不能同时声明 {'、'.join(forms)}，只能取其一")
        derived = _derived_from(raw, order)
        if "tool" in raw:
            name = _tool_name(raw, order)
            reads, output, args = _tool_io(raw, order)
            planned.append(DistilledStep(order, f"<tool:{name}>", derived, reads, (output,),
                                         kind=KIND_TOOL, tool_name=name, arguments=args))
            continue
        if "llm" in raw:
            step = DistilledStep(order, _llm_text(raw, order), derived,
                                 *_llm_io(raw, order), kind=KIND_LLM,
                                 verify=_llm_verify(raw, order))
            _check_llm_prompt(step)
            planned.append(step)
            continue
        if "human" in raw:
            step = DistilledStep(order, _human_text(raw, order), derived,
                                 *_llm_io(raw, order, label="human"), kind=KIND_HUMAN)
            _check_llm_prompt(step)
            planned.append(step)
            continue
        run = _run_text(raw, order)
        command = parse_command(run)
        _check_command(run, command, order)
        planned.append(DistilledStep(order, run, derived,
                                     command.read_paths, command.write_paths))
    return tuple(planned)


def _check_chain(traces: Sequence[Trace], planned: Sequence[DistilledStep],
                 params: Mapping[str, Any] | None) -> tuple[tuple[str | None, str], ...]:
    """A plan may not silently drop a session step its inputs depend on.

    Only the session knows what really produced a path. When the plan reads a path
    that an earlier successful session step wrote, that producer has to be either
    part of the plan, or an explicitly declared chain boundary (a placeholder bound
    to that path). Boundaries are reported, never silently accepted. Each boundary
    keeps its parameter name so a later stage can tell whether it still applies.

    A script is an asset rather than data: the package carries the file itself, so a
    dropped step that wrote one — the copy that staged a pipeline the chain then runs,
    a generator that emitted a helper — does not leave the chain incomplete.
    """
    produced: dict[str, tuple[str, int, str, str]] = {}
    for trace in traces:
        for step in trace.steps:
            if (step.kind not in (KIND_SHELL, KIND_WRITE, KIND_TOOL)
                    or step.rejected or not step.success):
                continue
            for path in step.write_paths:
                produced.setdefault(path, (trace.trace_id, step.seq, step.kind,
                                           step.command, step))
    given = bindings(params)
    # A step that writes `{out}` produces the path that placeholder binds to, so a
    # later step reading either spelling is fed by this plan, not by the session.
    plan_paths = {canonical_path(path, given) for step in planned for path in step.writes}
    missing: list[str] = []
    boundaries: list[tuple[str | None, str]] = []
    declared: set[str] = set()
    for order, step in enumerate(planned, start=1):
        for path in step.reads:
            key = placeholder_key(path)
            concrete = given.get(key, "") if key else path
            if not concrete or concrete in plan_paths:
                continue
            producer = produced.get(concrete)
            if producer is None:
                continue
            trace_id, seq, kind, command, producer_step = producer
            asset = role(concrete, writing=False) == ROLE_SCRIPT
            if key:
                if concrete not in declared:
                    declared.add(concrete)
                    if not asset and _command_expressible(producer_step):
                        raise DistillError(
                            f"第 {order} 步把 `{concrete}` 声明为输入边界（参数 `{key}`），"
                            f"但它在会话里由固定命令产出：`{trace_id}:{seq}` `{command}`。"
                            "固定调用形式的步骤必须进 steps，不能降级成输入边界；"
                            "只有编辑工具、人工或纯 agent 产生的产物才允许作为输入边界。"
                        )
                    if asset:
                        boundaries.append((key,
                            f"链路用到的脚本 `{concrete}` 在会话里由 `{trace_id}:{seq}` 产出，"
                            "该步骤未固化；脚本随包搬运，读不到时仍按输入边界处理。"))
                    else:
                        boundaries.append((key,
                            f"链路以 `{concrete}` 为输入边界（参数 `{key}`），它在会话里由 "
                            f"`{trace_id}:{seq}` 产出，该步骤未固化；要完整链路请把它补进 steps。"))
                continue
            if asset:
                continue
            detail = (f"第 {order} 步读取 `{concrete}`，但它在会话里由 `{trace_id}:{seq}` 产出，"
                      f"而这一步不在沉淀链路里。")
            if kind == KIND_WRITE:
                detail += ("该产出由编辑工具写入，内容无法机械固化；请把产出改成脚本执行，")
            else:
                detail += f"产出命令：`{command}`；请把这一步补进 steps，"
            detail += ("或改用 `{参数名}` 占位符 + params 给出它的真实值："
                       "写进工作区的脚本会被随包搬运并固化成 graph 步骤，"
                       "其它产物则作为链路输入边界写进报告。")
            missing.append(detail)
    if missing:
        raise DistillError("链路不完整，缺少上游步骤：\n" + "\n".join(f"- {item}" for item in missing))
    return tuple(boundaries)


def _command_expressible(step: Any) -> bool:
    """Whether the graph could replay this session step as a fixed call.

    A session tool call the exporter could not map to a known tool is exactly as
    fixed as a shell command; it only lacks the artifact declaration a distilled
    tool step carries, which is why it stays out of the graph as captured.
    """
    if step.kind == KIND_TOOL:
        return True
    return (step.kind == KIND_SHELL and not step.inline_code
            and bool(step.command_shape))


def _literal_key(flag: str) -> str:
    """The parameter name a flag's value would take: `--images-root` → `images_root`."""
    name = re.sub(r"[^a-z0-9]+", "_", flag.lstrip("-").lower()).strip("_")
    return name or "value"


def _literal_message(order: int, flag: str, value: str) -> str:
    """Why one unclassified token is a replay hazard, and the two ways out."""
    where = f"`{flag} {value}`" if flag else f"`{value}`"
    lines = [
        f"第 {order} 步的命令里 {where} 没有被识别成路径，但它在会话工作目录里真实存在：",
        "回放的工作目录是包目录而不是会话目录，这个字面量在回放时会指向不存在的位置。",
        f"它是路径就写成 `{{{_literal_key(flag or value)}}}` 占位符，"
        "并在 params 里给出它在会话里的真实值（params 里的相对值按会话工作目录解析）；",
    ]
    if flag:
        lines.append(f"它只是和目录同名的标量就写成 `{flag}={value}`。")
    return "".join(lines)


def _check_replay_literals(planned: Sequence[DistilledStep], workspace: Any) -> None:
    """A command may not carry a workspace-relative literal into a package replay.

    Replay runs the frozen command with the package as its working directory, so a
    value such as `--images-root storage` that only resolves in the session's cwd
    points at nothing. Values the workspace does not have stay literals, so a scalar
    like `--pipeline llm-filter` still passes.
    """
    source = source_for(workspace)
    for step in planned:
        if step.kind != KIND_RUN:
            continue
        for flag, value in unresolved_literals(source, step.run):
            raise DistillError(_literal_message(step.order, flag, value))


def _candidate_steps(traces: Sequence[Trace], limit: int = 20) -> str:
    """The session steps an llm node could point at, for the rejection message."""
    lines: list[str] = []
    for trace in traces:
        for step in trace.steps:
            command = " ".join(step.command.split())[:120] or "-"
            lines.append(f"- `{trace.trace_id}:{step.seq}` [{step.kind}] {command}")
            if len(lines) == limit:
                return "\n".join([*lines, "- ……"])
    return "\n".join(lines) or "- （会话里没有可引用的步骤）"


_SCRATCH = (".pyromind-agent/", ".agent_tmp/", "agentgenome-traces/")


def _scratch_path(path: str) -> bool:
    """Trace exports, agent scratch space and absolute paths are not chain artifacts."""
    return path.startswith("/") or any(marker in path for marker in _SCRATCH)


def _uncovered_steps(planned: Sequence[DistilledStep],
                     traces: Sequence[Trace],
                     params: Mapping[str, Any] | None) -> list[Any]:
    """Session steps that produced a workspace file but are not part of the plan.

    A plan may legitimately start halfway, but only on purpose: a step that wrote a
    file and is missing from the chain is exactly how a capture silently loses the
    front half of a session, so it has to be named before anything is frozen. The
    witness is the file itself, not the `from` link the agent may or may not have
    written: a plan covers a session step when it produces the same path.
    """
    given = bindings(params)
    planned_paths = {canonical_path(path, given)
                     for step in planned for path in step.writes}
    uncovered = []
    for trace in traces:
        for step in trace.steps:
            if not step.success or step.rejected or not _command_expressible(step):
                continue
            produced = {canonical_path(path, given) for path in step.write_paths}
            if produced & planned_paths:
                continue
            if not any(not _scratch_path(path) for path in produced):
                continue
            uncovered.append((trace.trace_id, step))
    return uncovered


def _step_line(trace_id: str, step: Any) -> str:
    command = " ".join(step.command.split())[:160] or "-"
    produced = (f" → {', '.join(f'`{path}`' for path in step.write_paths)}"
                if step.write_paths else "")
    return f"`{trace_id}:{step.seq}` [{step.kind}] `{command}`{produced}"


def _names_artifact(text: str, path: str) -> bool:
    """Whether a user turn names this artifact, whichever root it spells.

    The turn may write the path with the host's own prefix (`/workspace/datasets/x`
    against `storage/datasets/x`), so any long-enough tail of the path counts as
    naming it.
    """
    segments = [part for part in path.strip().strip("\"'`").split("/") if part]
    if not segments:
        return False
    for depth in range(min(len(segments), 4), 1, -1):
        if "/".join(segments[-depth:]) in text:
            return True
    return len(segments) == 1 and segments[0] in text


def _data_paths(paths: Sequence[str], given: Mapping[str, str],
                *, writing: bool) -> set[str]:
    """The canonical paths of a step that carry data, scripts left out.

    A script is a fixed artifact the chain re-points at rather than a dataset, so two
    steps that touch the same `.py` file are not chained by data: a local trial run
    sharing a pipeline script with the planned submission is not a truncated chain.
    """
    return {path for path in (canonical_path(item, given) for item in paths)
            if role(path, writing=writing) != ROLE_SCRIPT}


def _handover_steps(planned: Sequence[DistilledStep], traces: Sequence[Trace],
                    params: Mapping[str, Any] | None) -> list[Any]:
    """Dropped steps the plan leaves behind by starting at a human handover.

    A chain can hand work to a person mid-session (Label Studio 复核、人工导出): the
    session runs the front half, the user comes back saying they produced the artifact,
    and the plan continues from there. That turn is the witness the handover belongs to
    this chain — starting at the artifact the person handed back would absorb the human
    gate instead of freezing it. The witness has to be the turn naming an artifact the
    plan takes as a root input: a session carries plenty of ordinary mid-chain
    instructions, so anything looser would stop every distillation that leaves a step
    out.
    """
    given = bindings(params)
    planned_writes = _data_paths([path for step in planned for path in step.writes],
                                 given, writing=True)
    planned_reads = _data_paths([path for step in planned for path in step.reads],
                                given, writing=False)
    roots = planned_reads - planned_writes
    dropped: dict[str, list[Any]] = {}
    for trace_id, step in _uncovered_steps(planned, traces, params):
        dropped.setdefault(trace_id, []).append(step)
    handovers: list[Any] = []
    for trace in traces:
        steps = dropped.get(trace.trace_id, [])
        if not steps:
            continue
        turns = [step for step in trace.steps
                 if step.kind == KIND_HUMAN and step.command]
        for step in steps:
            later = [turn for turn in turns if turn.seq > step.seq]
            if any(_names_artifact(turn.command, root)
                   for turn in later for root in roots):
                handovers.append((trace.trace_id, step))
    return handovers


def _scope_split(planned: Sequence[DistilledStep], traces: Sequence[Trace],
                 params: Mapping[str, Any] | None) -> tuple[list[Any], list[Any]]:
    """Split the left-out session steps into chain continuations and side branches.

    A dropped step that still touches the chain — it reads what a planned step wrote,
    or feeds what a planned step reads — is what a truncated chain looks like, and it
    has to be named before anything is frozen. A dropped step that touches nothing in
    the plan (a local trial run, a probe writing a debug file, an attempt a later step
    replaced) is a side branch: the plan stands on its own without it, so it is
    reported instead of blocking the freeze.
    """
    given = bindings(params)
    planned_writes = _data_paths([path for step in planned for path in step.writes],
                                 given, writing=True)
    planned_reads = _data_paths([path for step in planned for path in step.reads],
                                given, writing=False)
    uncovered = _uncovered_steps(planned, traces, params)
    writes = [_data_paths(step.write_paths, given, writing=True)
              for _, step in uncovered]
    reads = [_data_paths(step.read_paths, given, writing=False)
             for _, step in uncovered]
    connected: set[int] = set()
    changed = True
    while changed:
        changed = False
        for index in range(len(uncovered)):
            if index in connected:
                continue
            touches_plan = bool(writes[index] & planned_reads
                                or reads[index] & planned_writes)
            touches_peer = any(writes[index] & reads[other]
                               or reads[index] & writes[other] for other in connected)
            if touches_plan or touches_peer:
                connected.add(index)
                changed = True
    return ([item for index, item in enumerate(uncovered) if index in connected],
            [item for index, item in enumerate(uncovered) if index not in connected])


def _review_scope(planned: Sequence[DistilledStep], traces: Sequence[Trace],
                  params: Mapping[str, Any] | None,
                  confirmed: bool, reason: str | None) -> tuple[list[Any], str | None]:
    """Stop and ask the user before freezing a chain that drops connected steps.

    The agent chooses the scope, so the mechanical witness of a truncated chain is
    the session itself: a dropped step that feeds the plan or continues it. Naming
    those steps is what turns "did you mean this half?" into a question the user can
    answer. A plan that starts at what a person handed back mid-session is the same
    shape seen from the other end: the human gate belongs in the chain as a `human`
    step, so the steps that ran before the handover have to be named too. Side
    branches need no confirmation — the caller records them in the report — because
    the plan does not depend on them.
    """
    connected, branches = _scope_split(planned, traces, params)
    handover = [item for item in _handover_steps(planned, traces, params)
                if item not in set(connected)]
    if not connected and not handover:
        return branches, None
    if not confirmed:
        named = [*connected, *handover]
        listed = "\n".join(f"- {_step_line(trace_id, step)}"
                           for trace_id, step in named)
        raise DistillError(
            f"链路范围需要用户复核：会话里还有 {len(named)} 个步骤不在这次沉淀的链路里："
            f"\n{listed}\n"
            "它们要么把产物喂给链路、要么接在链路后面，要么在用户点名交回产物的那一步"
            "之前跑过（人工交接的前半段：人工复核、人工导出这类步骤要用 human 节点进图，"
            "不能从人交回来的产物开始）。先把它们补进 steps（固定命令用 run、固定工具调用用 tool、人的"
            "复核用 human、agent 裁量用 llm），这样沉淀下来的才是用户走过的那条链路；"
            "如果用户确认这次只要这一段，请带 review_confirmed=true 和 review_reason "
            "重新提交，把确认过的边界写进报告。")
    confirmed_reason = (reason or "").strip()
    if not confirmed_reason:
        raise DistillError(
            "review_confirmed=true 需要同时给出 review_reason：把用户确认过的边界写进"
            "沉淀报告，否则这次范围复核没有依据。")
    return branches, confirmed_reason


def _check_llm_promotions(planned: Sequence[DistilledStep], traces: Sequence[Trace],
                          policy: str) -> None:
    """An llm node may not stand in for a session step the graph could run or must gate.

    A fixed command belongs in `run` (LLM calls inside a script do not make it free-form),
    a human gate belongs in `human`, and an editor-written artifact is not an agent task at
    all — it has to become a script output or a declared input boundary. Only a genuine
    agent-discretion step becomes `do: {llm}`.

    `scripts-first` (default) enforces that mechanically: every llm node has to name the
    session step it came from, and that step may not be a fixed command. `adaptive-first`
    trusts the agent's classification instead and only keeps the human/editor checks.
    """
    if not traces:
        return
    index = {f"{trace.trace_id}:{step.seq}": step for trace in traces for step in trace.steps}
    strict = policy == POLICY_SCRIPTS_FIRST
    for step in planned:
        if step.kind == KIND_HUMAN:
            source = index.get(step.derived_from) if step.derived_from else None
            if source is not None and _command_expressible(source):
                raise DistillError(
                    f"第 {step.order} 步声明成人工节点，但它在会话里是固定"
                    f"{'工具调用' if source.kind == KIND_TOOL else '命令'}："
                    f"`{step.derived_from}` `{source.tool_name or source.command}`。"
                    "机械步骤不该停下来等人：请用 "
                    f"{'tool' if source.kind == KIND_TOOL else 'run'} 步骤提交它。")
            continue
        if step.kind != KIND_LLM:
            continue
        source = index.get(step.derived_from) if step.derived_from else None
        if source is None:
            if not strict:
                continue
            if step.derived_from:
                raise DistillError(
                    f"第 {step.order} 步的 from `{step.derived_from}` 不在 source_traces 里。"
                    "请填这一步真正来源的 `<trace_id>:<seq>`，会话里可选的步骤：\n"
                    f"{_candidate_steps(traces)}")
            raise DistillError(
                f"第 {step.order} 步声明成 llm 节点但没有 from：请补上这一步来源的 "
                "`<trace_id>:<seq>`，插件据此核对它不是固定命令。会话里可选的步骤：\n"
                f"{_candidate_steps(traces)}")
        if source.kind == KIND_TOOL:
            raise DistillError(
                f"第 {step.order} 步声明成 llm 节点，但它在会话里是固定工具调用："
                f"`{step.derived_from}` `{source.tool_name}`。固定调用有确定的调用形式，"
                "请用 tool 步骤（tool/args/output）提交，交给 graph 直接执行。")
        if strict and _command_expressible(source):
            raise DistillError(
                f"第 {step.order} 步声明成 llm 节点，但它在会话里是固定命令："
                f"`{step.derived_from}` `{source.command}`。有 LLM 参与、产出内容会变都不等于"
                "自由裁量：只要调用形式固定，就用 run 提交，交给 graph 重跑。")
        if source.kind in (KIND_HUMAN, KIND_WRITE):
            if source.kind == KIND_HUMAN:
                raise DistillError(
                    f"第 {step.order} 步声明成 llm 节点，但它在会话里是人工门禁："
                    f"`{step.derived_from}`。人做的那一步不该再交给 agent 重跑："
                    "请用 human 步骤（human/output）提交，图会在这一步停下等人，"
                    "唤醒后从下一个节点继续。")
            raise DistillError(
                f"第 {step.order} 步声明成 llm 节点，但它在会话里是编辑工具写入："
                f"`{step.derived_from}`。编辑工具写出的内容没有记录下来的调用形式，"
                "不能靠重跑复现：请把这一步改成脚本产出（run 步骤），"
                "或把它的产物声明成本次任务的输入边界（`{参数名}` + params），"
                "不要包装成 llm 节点。")


def _replace_token(run: str, token: str, value: str) -> tuple[str, int]:
    """Replace one whole shell token, keeping the original quoting style intact."""
    quoted = re.escape(token)
    pattern = re.compile(
        _TOKEN_BOUNDARY + r"(?:'" + quoted + r"'|\"" + quoted + r"\"|" + quoted
        + r")(?![\w./-])"
    )
    return pattern.subn(value, run)


def _replace_arg_token(value: Any, token: str, replacement: str) -> tuple[Any, int]:
    """Replace a `{param}` token inside a tool call's args, returning the hits."""
    if isinstance(value, str):
        return value.replace(token, replacement), value.count(token)
    if isinstance(value, dict):
        replaced = {key: _replace_arg_token(item, token, replacement)
                    for key, item in value.items()}
        return ({key: item for key, (item, _) in replaced.items()},
                sum(count for _, count in replaced.values()))
    if isinstance(value, list):
        replaced = [_replace_arg_token(item, token, replacement) for item in value]
        return [item for item, _ in replaced], sum(count for _, count in replaced)
    return value, 0


def _replace_reads(reads: Sequence[str], frozen: Mapping[str, str]) -> tuple[str, ...]:
    """Point a step's declared reads at the concrete path a frozen `{param}` names."""
    return tuple(frozen.get(placeholder_key(item) or "", item) for item in reads)


def _materialize_scripts(
        planned: Sequence[DistilledStep], params: Mapping[str, Any] | None,
        workspace: Any) -> tuple[tuple[DistilledStep, ...], dict[str, str],
                                 tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Freeze a `{param}` that points at a script instead of leaving it an input boundary.

    The session produced the script with an editor tool, so the capture contract cannot
    take it as a graph step; declaring it a parameter would keep it outside the package
    and every reuse would have to rewrite it. Rewriting the command to the concrete path
    lets the emit stage copy the script like any other script read on the command line.
    A tool call's args carry scripts the same way — the pipeline a host tool runs is
    named there — so those placeholders are rewritten too. A script the workspace cannot
    read stays an input boundary, and the report says so.
    """
    given = {str(key): str(value) for key, value in (params or {}).items()}
    if not given:
        return tuple(planned), given, (), ()
    produced = {path for step in planned for path in step.writes}
    source = source_for(workspace)
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for step in planned:
        if step.kind not in (KIND_RUN, KIND_TOOL):
            continue
        for path in step.reads:
            key = placeholder_key(path)
            if key is None or key not in given or key in seen:
                continue
            value = given[key]
            if role(value, writing=False) != ROLE_SCRIPT or value in produced:
                continue
            seen.add(key)
            candidates.append((key, value))
    contents = read_many(source, [value for _, value in candidates])
    frozen: dict[str, str] = {}
    unreadable: set[str] = set()
    notes: list[str] = []
    for key, value in candidates:
        if contents.get(value) is None:
            unreadable.add(key)
            notes.append(
                f"参数 `{key}` 指向脚本 `{value}`，但工作区里读不到它，"
                "仍按输入边界处理（包内没有这份脚本）。")
            continue
        frozen[key] = value
    if not frozen:
        return tuple(planned), given, (), tuple(notes)
    rewritten: list[DistilledStep] = []
    replaced: set[str] = set()
    for step in planned:
        if step.kind == KIND_TOOL:
            args = dict(step.arguments or {})
            for key, value in frozen.items():
                if not any(placeholder_key(item) == key for item in step.reads):
                    continue
                args, count = _replace_arg_token(args, f"{{{key}}}", value)
                if count:
                    replaced.add(key)
            reads = _replace_reads(step.reads, frozen)
            rewritten.append(DistilledStep(step.order, step.run, step.derived_from,
                                           reads, step.writes, kind=step.kind,
                                           verify=step.verify, tool_name=step.tool_name,
                                           arguments=args))
            continue
        if step.kind != KIND_RUN:
            rewritten.append(step)
            continue
        run = step.run
        for key, value in frozen.items():
            if not any(placeholder_key(item) == key for item in step.reads):
                continue
            run, count = _replace_token(run, f"{{{key}}}", value)
            if count:
                replaced.add(key)
        command = parse_command(run)
        rewritten.append(DistilledStep(step.order, run, step.derived_from,
                                       command.read_paths, command.write_paths,
                                       kind=step.kind, verify=step.verify))
    materialized = tuple(sorted((key, value) for key, value in frozen.items()
                                if key in replaced))
    return (tuple(rewritten), {key: value for key, value in given.items()
                               if key not in replaced},
            materialized, tuple(notes))


def _bound_values(package: Package, planned: Sequence[DistilledStep],
                  params: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Replay A's concrete inputs: what the placeholders and root paths bind to.

    `params` may also carry the placeholders the chain writes or passes between its
    own steps — agents name every `{token}` their commands use. Those bindings are
    not root inputs, so they stay out of the replay; a key no step uses is still a
    mistake and gets reported.
    """
    expected = {key for key, _ in package.params}
    if params is None:
        unbound = sorted(key for key, value in package.params if placeholder_key(value))
        if unbound:
            hint = "、".join(f"`{key}`" for key in unbound)
            example = ", ".join(f"{key}: <路径>" for key in unbound)
            raise DistillError(
                f"命令里用了占位符输入：{hint}。distill 需要 params 给出回放 A 的真实值，"
                f"例如 params: {{{example}}}")
        return package.params
    given = tuple((str(key), str(value)) for key, value in params.items())
    keys = {key for key, _ in given}
    used: set[str] = set()
    for step in planned:
        for path in (*step.reads, *step.writes):
            key = placeholder_key(path)
            if key is not None:
                used.add(key)
    if keys - expected - used or expected - keys:
        raise DistillError(
            f"params 必须覆盖链路的根输入：需要 {sorted(expected)}，"
            f"收到 {sorted(keys)}。根输入来自命令里的路径与 {{占位符}}")
    return tuple((key, value) for key, value in given if key in expected)


def _step_material(step: DistilledStep) -> list[Any]:
    """What makes one step distinct, for the trace id a retry of the plan reuses."""
    if step.kind == KIND_TOOL:
        return [step.kind, step.tool_name, dict(step.arguments or {}), list(step.writes)]
    return [step.kind, step.run]


def _trace_id(label: str, steps: Sequence[DistilledStep],
              source_traces: Sequence[str]) -> str:
    """The same plan always distils to the same trace, so a retry is a reuse."""
    material = json.dumps([label, [_step_material(step) for step in steps],
                           list(source_traces)],
                          ensure_ascii=False, sort_keys=True)
    return f"distilled-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _trace_entry(step: DistilledStep) -> dict[str, Any]:
    """The capture-trace entry for one distilled step, carrying where it came from."""
    entry_id = step.derived_from or f"distilled-{step.order}"
    if step.kind == KIND_TOOL:
        return {
            "kind": "tool_call",
            "entry_id": entry_id,
            "timestamp": None,
            "tool": step.tool_name,
            "call_id": f"distilled-{step.order}",
            "args": dict(step.arguments or {}),
            "output": step.writes[0],
            "result": {"is_error": False, "text": "distilled"},
        }
    if step.kind == KIND_LLM:
        return {
            "kind": "llm",
            "entry_id": entry_id,
            "timestamp": None,
            "tool": "llm",
            "call_id": f"distilled-{step.order}",
            "args": {"prompt": step.run, "reads": list(step.reads),
                     "writes": list(step.writes), "verify": step.verify or ""},
            "result": {"is_error": False, "text": "distilled"},
        }
    if step.kind == KIND_HUMAN:
        return {
            "kind": "human",
            "entry_id": entry_id,
            "timestamp": None,
            "tool": "human",
            "call_id": f"distilled-{step.order}",
            "args": {"question": step.run, "reads": list(step.reads),
                     "writes": list(step.writes)},
            "result": {"is_error": False, "text": "distilled"},
        }
    return {
        "kind": "tool",
        "entry_id": entry_id,
        "timestamp": None,
        "tool": "terminal",
        "call_id": f"distilled-{step.order}",
        "args": {"command": step.run},
        "result": {"is_error": False, "text": "distilled"},
    }


def trace_document(label: str, steps: Sequence[DistilledStep], *,
                   source_traces: Sequence[str] = ()) -> dict[str, Any]:
    """A trace the capture stages accept, carrying where each step came from."""
    return {
        "trace_version": TRACE_VERSION,
        "trace_id": _trace_id(label, steps, source_traces),
        "session_id": None,
        "session_name": label,
        "step_count": len(steps),
        "dropped_steps": 0,
        "source_traces": list(source_traces),
        "steps": [_trace_entry(step) for step in steps],
    }


def bindings(params: Mapping[str, Any] | None) -> dict[str, str]:
    """The concrete path each `{param}` placeholder stands for in this plan."""
    return {str(key): str(value) for key, value in (params or {}).items()}


def _spell_out(path: str, bindings: Mapping[str, str]) -> str:
    concrete = canonical_path(path, bindings)
    return f"`{path}`" if concrete == path else f"`{path}`（= `{concrete}`）"


def _check_final_artifact(final_artifact: str, steps: Sequence[DistilledStep],
                          params: Mapping[str, Any] | None = None) -> str:
    """The plan write this artifact names, whichever spelling the agent used.

    A step may write `{sft_messages}` while the artifact is declared by the concrete
    path that placeholder binds to, or the other way round; both name one file, so
    the plan keeps the token it wrote and the slice follows that token.
    """
    given = bindings(params)
    wanted = canonical_path(final_artifact, given)
    for step in steps:
        for path in step.writes:
            if canonical_path(path, given) == wanted:
                return path
    produced = [_spell_out(path, given) for step in steps for path in step.writes]
    detail = ("计划里的产出：" + "、".join(produced[:8])
              if produced else "计划里没有任何产出")
    raise DistillError(
        f"final_artifact `{final_artifact}` 没有被任何步骤声明为产出；{detail}。"
        f"{OUTPUT_CONTRACT_HINT}")


@dataclass(frozen=True)
class DistillPlan:
    trace: Trace
    document: dict[str, Any]
    steps: tuple[DistilledStep, ...]
    directory: Path
    package: Package
    auxiliary: tuple[int, ...]
    chain_traces: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    bound: tuple[tuple[str, str], ...] = ()
    materialized: tuple[tuple[str, str], ...] = ()
    materialize_notes: tuple[str, ...] = ()
    policy: str = POLICY_SCRIPTS_FIRST
    scope_review: str | None = None
    branches: tuple[str, ...] = ()

    @property
    def report_path(self) -> Path:
        return self.directory / "report.md"

    def payload(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace.trace_id,
            "trace": self.document,
            "report": str(self.report_path),
            "package": str(self.package.directory),
            "steps": [step.payload() for step in self.steps],
            "params": [{"key": key, "source": path, "value": dict(self.bound).get(key)}
                       for key, path in self.package.params],
            "chain": {"checked": bool(self.chain_traces),
                      "traces": list(self.chain_traces),
                      "boundaries": list(self.boundaries)},
            "materialized": [{"key": key, "path": path}
                             for key, path in self.materialized],
            "materialize_notes": list(self.materialize_notes),
            "policy": self.policy,
            "scope_review": self.scope_review,
            "branches": list(self.branches),
            "scripts": [{"source": source, "package": target}
                        for source, target in self.package.scripts],
            "nodes": [{"step": node.order + 1, "id": node.id, "kind": node.kind,
                       "command": node.command}
                      for node in self.package.nodes],
            "not_solidifiable": [{"step": order + 1, "reason": reason}
                                 for order, reason in self.package.not_solidifiable],
            "auxiliary": [{"step": order + 1, "reason": AUXILIARY_REASON}
                          for order in self.auxiliary],
            "final_artifact": self.package.final_artifact,
            **self.package.applicability,
        }

    def summary(self) -> dict[str, Any]:
        """The payload without the trace document: a caller already has the trace."""
        return {key: value for key, value in self.payload().items() if key != "trace"}


def _report(label: str, plan: DistillPlan, alignment: Alignment,
            slice_: Slice) -> str:
    sources = [step.derived_from for step in plan.steps if step.derived_from]
    final = (f"`{plan.package.final_artifact}`" if plan.package.final_artifact
             else "未声明（全量骨架）")
    lines = [
        "# 链路蒸馏报告", "",
        f"- 蒸馏轨迹：`{plan.trace.trace_id}`",
        f"- 经验名：{label}",
        f"- 来源步骤：{'、'.join(f'`{item}`' for item in sources) or '未标注'}",
        f"- 固化策略：`{plan.policy}`"
        + ("（llm 节点必须带可解析的 from，且来源不能是固定命令）"
           if plan.policy == POLICY_SCRIPTS_FIRST else "（按 agent 判断接受 llm 节点）"),
        f"- 最终产物：{final}",
        "- 链路完整性：" + ("已按 " + "、".join(f"`{item}`" for item in plan.chain_traces)
                             + " 校验" if plan.chain_traces else "未提供来源轨迹，未校验"),
        "- 输入绑定：" + ("、".join(f"`{key}` = `{value}`" for key, value in plan.bound)
                          or "无根输入"),
        f"- 草稿：`{plan.directory}`",
        "",
        "| # | 类型 | 命令 / 提示 | 来源 | 输入 | 产出 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if plan.boundaries:
        lines.extend([""] + [f"- 输入边界：{item}" for item in plan.boundaries])
    if plan.materialized:
        lines.extend([""] + [f"- 脚本参数已物化：`{key}` → `{path}`（随包搬运，不再作为输入边界）"
                             for key, path in plan.materialized])
    if plan.materialize_notes:
        lines.extend([""] + [f"- {note}" for note in plan.materialize_notes])
    if plan.scope_review:
        lines.extend(["", f"- 范围复核：{plan.scope_review}"])
    if plan.branches:
        lines.extend([""] + [f"- 旁支步骤（未进链路，链路不依赖它）：{item}"
                             for item in plan.branches])
    for step in plan.steps:
        command = step.run.replace("|", "\\|")
        lines.append(f"| {step.order} | {step.kind} | `{command}` | {step.derived_from or '-'} | "
                     f"{'、'.join(step.reads) or '-'} | {'、'.join(step.writes) or '-'} |")
    lines.extend(["", render_alignment(alignment), "\n",
                  render_slice(slice_, alignment.slots), "\n",
                  render_emit(plan.package)])
    return "\n".join(lines)


def distill(service: GenomeService, *, label: str, steps: Sequence[Any],
            workspace: Any, final_artifact: str | None = None,
            asset_id: str | None = None, description: str | None = None,
            source_traces: Sequence[str] = (), owner: str = "local",
            version: str | None = None,
            threshold: float = DEFAULT_THRESHOLD,
            declarations: Mapping[str, Any] | None = None,
            traces: Sequence[Trace] = (),
            params: Mapping[str, Any] | None = None,
            policy: str = POLICY_SCRIPTS_FIRST,
            review_confirmed: bool = False,
            review_reason: str | None = None) -> DistillPlan:
    """Turn a distilled chain into a compiled package the capture stages accept."""
    try:
        policy = normalize_policy(policy)
    except ValueError as exc:
        raise DistillError(str(exc)) from exc
    planned = plan_steps(steps)
    _check_llm_promotions(planned, traces, policy)
    final_token: str | None = None
    if final_artifact is not None:
        final_token = _check_final_artifact(final_artifact, planned, params)
    # The chain check reads the plan as the agent wrote it: a `{script}` placeholder is
    # an explicit boundary there, while the frozen path it is rewritten to would look
    # like a dropped upstream step.
    boundaries = _check_chain(traces, planned, params)
    branches, scope_review = _review_scope(
        planned, traces, params, review_confirmed, review_reason)
    _check_replay_literals(planned, workspace)
    rewritten, remaining, materialized, materialize_notes = _materialize_scripts(
        planned, params, workspace)
    frozen = {key for key, _ in materialized}
    boundaries = tuple(text for key, text in boundaries if key not in frozen)
    if final_token is not None and (key := placeholder_key(final_token)) is not None:
        final_token = dict(materialized).get(key, final_token)
    document = trace_document(label, rewritten, source_traces=source_traces)
    trace = capture_trace(json.dumps(document))
    alignment = align_traces([trace], threshold)
    directory = service.root / "distills" / trace.trace_id
    try:
        slice_ = reverse_slice(alignment, final_token, bindings(params))
        package = build_package(directory / "package", alignment=alignment,
                                slice_=slice_, traces=[trace], label=label,
                                workspace=workspace, owner=owner,
                                version=version or DEFAULT_VERSION,
                                description=description, asset_id=asset_id,
                                declarations=declarations,
                                bindings=bindings(params))
    except (SliceError, EmitError, CompileError) as exc:
        raise DistillError(f"{exc}（草稿：{directory / 'package'}）") from exc
    bound = _bound_values(package, rewritten, remaining if params is not None else None)
    plan = DistillPlan(trace, document, rewritten, directory, package, slice_.auxiliary,
                       tuple(trace.trace_id for trace in traces), boundaries, bound,
                       materialized, materialize_notes, policy,
                       scope_review,
                       tuple(_step_line(trace_id, step) for trace_id, step in branches))
    plan.report_path.parent.mkdir(parents=True, exist_ok=True)
    plan.report_path.write_text(_report(label, plan, alignment, slice_), encoding="utf-8")
    return plan
