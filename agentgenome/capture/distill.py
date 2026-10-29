"""Stage 0: a reviewable structural draft, then an explicit preflight.

The mechanical stages only solidify commands whose inputs and outputs are on the
command line, so a session full of probes, inline code and script-internal writes
cannot be captured as it stands. Distilling is the agent's job: it reads the
session, decides which steps are the business chain, and declares every path on
the command line. ``draft_distill`` exposes that structure without touching script
bytes or building a graph. ``distill`` runs the complete preflight only after the
user confirms the draft, and then holds the result to the same contract the
capture stages enforce.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from builder import CompileError

from .. import paths
from ..parameters import (
    ParameterError,
    contracts_to_specs,
    normalize_value,
)
from ..service import GenomeService
from .align import DEFAULT_THRESHOLD, Alignment, align_traces, render_alignment
from .emit import (ASYNC_TOOLS, DEFAULT_VERSION, EmitError, Package, build_package,
                   is_async_tool, read_many, render_emit, source_for,
                   unresolved_literals)
from .plan import recover_artifact_path
from .policy import POLICY_SCRIPTS_FIRST, normalize_policy
from .slice import OUTPUT_CONTRACT_HINT, Slice, SliceError, render_slice, reverse_slice
from .trace import (INLINE_MARK, KIND_HUMAN, KIND_LLM, KIND_SHELL, KIND_TOOL, KIND_WRITE,
                    ROLE_SCRIPT, TRACE_VERSION, Command, Trace, arg_paths, canonical_path,
                    capture_trace, parse_command, path_template, placeholder_key,
                    probe_command, role)

MAX_STEPS = 200
KIND_RUN = "run"
AUXILIARY_REASON = ("产出没有被链路里的后续步骤消费，不在最终产物的依赖闭包里；"
                    "要固化它，请单独用 final_artifact 指向它的产出再沉淀一条经验")
KEEP_REASON = "声明了 keep：它的产出不参与依赖闭包，但这一步的副作用属于链路"
_TOKEN_BOUNDARY = r"(?<![\w./-])"
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
# A path is either one whole `{token}` or a literal. `prelabel_{records}.jsonl`
# mixes the two, and the placeholder regex (which anchors at the end of the token)
# does not recognise it — so the whole thing became a literal file name, and every
# later stage invented a parameter out of it (`prelabel_manifest_records_2`).
_TEMPLATE_TOKEN = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
# A parameter names a role, never the size of the run that produced it: a chain
# captured from a ten-record trial must still accept any number of records.
_CARDINALITY = re.compile(r"(?:^|_)(\d{1,3})(?=_|$)")
# Platform tools that only accept a job: the receipt is not the product, and the
# next step has to wait for the platform's terminal state before it can read one.
# `ASYNC_TOOLS` and `is_async_tool` live beside the emitter so the await node and
# this check cannot drift.
_WAIT_SCRIPT = re.compile(r"(?:^|[\s/'\"])(?:wait|await)[a-z_]*pipeline\.py\b")
_WAIT_TOOL = re.compile(r"(?:^|[._])(?:wait|await)(?:_|$)")
# `cd <dir>; …` only chooses where the command runs; the directory is context.
_CD_TARGET = re.compile(r"(?:^|[;&|]\s*)cd\s+([^\s;&|]+)")
# A trial slice is a command that keeps a prefix of the data: `head -10 x > y`.
_SLICE_COMMAND = re.compile(r"(?:\bhead\b|\btail\b|\bshuf\b|\bsplit\b|\bsed\s+-n\b)")


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
    keep: bool = False
    await_receipt: str | None = None

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step": self.order,
            "kind": self.kind,
            "from": self.derived_from,
            "reads": list(self.reads),
            "writes": list(self.writes),
        }
        if self.await_receipt is not None:
            data["kind"] = "await"
            data["await"] = self.await_receipt
            data["run"] = self.run
        elif self.kind == KIND_TOOL:
            data["tool"] = self.tool_name
            data["args"] = dict(self.arguments or {})
        else:
            data["run"] = self.run
        if self.verify:
            data["verify"] = self.verify
        if self.keep:
            data["keep"] = True
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


def _verify_command(raw: Mapping[str, Any], order: int) -> str | None:
    value = raw.get("verify")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DistillError(f"第 {order} 步的 verify 必须是非空的验收命令字符串")
    return value.strip()


def _keep_flag(raw: Mapping[str, Any], order: int) -> bool:
    """Whether the step must stay in the graph even if nothing consumes its output."""
    value = raw.get("keep")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise DistillError(f"第 {order} 步的 keep 只能是 true / false")
    return value


def _run_io(raw: Mapping[str, Any], order: int, run: str,
            command: Command) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A shell step's inputs and products, honouring an explicit `output`.

    The command line is the usual source: `> out`, `--output out` and `cp` name the
    product. A script that takes its output path positionally looks like a read to
    the parser, and one that writes its own products names nothing at all, so the
    agent may declare the products itself. Every declared path has to appear in the
    command, because emit replaces exactly the tokens it can see.
    """
    declared = raw.get("output")
    if declared is None:
        declared = []
    elif isinstance(declared, str):
        declared = [declared]
    if (not isinstance(declared, list)
            or any(not isinstance(item, str) or not item.strip() for item in declared)):
        raise DistillError(f"第 {order} 步的 output 必须是路径字符串或路径字符串列表")
    outputs = [item.strip() for item in declared]
    missing = [token for token in outputs if token not in run]
    if missing:
        raise DistillError(
            f"第 {order} 步声明了 output，但命令里找不到它们："
            + "、".join(f"`{token}`" for token in missing)
            + "。emit 只把命令里出现的路径替换成节点产物，请把产出写成命令行上的路径"
            "（脚本自己写出的产物要先把输出路径加到脚本参数上）")
    writes = [*command.write_paths, *(item for item in outputs
                                     if item not in command.write_paths)]
    reads = [path for path in command.read_paths if path not in outputs]
    return tuple(reads), tuple(writes)


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
    # Compare through the canonical namespace: `/target-workspace/datasets/x`,
    # `storage/datasets/x` (old captures) and `/datasets/x` are one object, and a
    # plain substring test called the declaration missing.
    missing = [token for token in reads
               if not any(paths.same(token, value) for value in values)]
    if missing:
        raise DistillError(
            f"第 {order} 步声明了 input，但 args 里找不到它们："
            + "、".join(f"`{token}`" for token in missing)
            + "。工具只会收到 args 里的值，请把输入写进 args")
    for path in arg_paths(args):
        if not any(paths.same(path, item) for item in reads):
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


def _portable_mount_paths(
    steps: Sequence[DistilledStep], params: Mapping[str, Any] | None
) -> tuple[tuple[DistilledStep, ...], dict[str, Any]]:
    """Rewrite sandbox mount spellings into the plan's portable form.

    The trace quotes what the sandbox printed; a plan has to outlive that sandbox.
    Normalising here keeps the later checks, the review and the frozen graph from
    all reporting the same prefix as a problem the model must fix by hand.
    """
    def rewrite_path(path: str) -> str:
        return paths.portable(path)

    rewritten: list[DistilledStep] = []
    for step in steps:
        run = _MOUNT_TOKEN.sub(
            lambda match: paths.portable(match.group(0)), step.run)
        if step.await_receipt is not None:
            receipt = rewrite_path(step.await_receipt)
            writes = tuple(rewrite_path(path) for path in step.writes)
            rewritten.append(replace(
                step,
                run=f"await {receipt} -> {writes[0]}" if writes else run,
                reads=(receipt,),
                writes=writes,
                await_receipt=receipt,
            ))
            continue
        arguments = step.arguments
        if isinstance(arguments, Mapping):
            arguments = {
                key: (rewrite_path(value) if isinstance(value, str) else value)
                for key, value in arguments.items()
            }
        rewritten.append(replace(
            step,
            run=run,
            reads=tuple(rewrite_path(path) for path in step.reads),
            writes=tuple(rewrite_path(path) for path in step.writes),
            arguments=arguments,
        ))
    bound = {
        key: (rewrite_path(value) if isinstance(value, str) else value)
        for key, value in (params or {}).items()
    }
    return tuple(rewritten), bound


_MOUNT_TOKEN = re.compile(
    r"(?:/)?(?:target-workspace|workspace)"
    r"(?:/\.pyromind-agent/[^/\s\"'`;|&<>]+)?"
    r"(?:/[^\s\"'`;|&<>)]*)?"
)


def plan_steps(steps: Sequence[Any]) -> tuple[DistilledStep, ...]:
    """Validate the agent's chain against the capture contract, step by step."""
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence) or not steps:
        raise DistillError(
            "distill 需要 steps：[{run, from?}]、[{await, output, from?}]、"
            "[{llm, output, input?, from?}]、[{tool, args, output, input?, from?}] "
            "或 [{human, output, input?, from?}]，按链路顺序排列")
    if len(steps) > MAX_STEPS:
        raise DistillError(f"步骤过多（{len(steps)}），最多 {MAX_STEPS} 步")
    planned: list[DistilledStep] = []
    problems: list[str] = []
    for order, raw in enumerate(steps, start=1):
        try:
            planned.append(_plan_step(raw, order))
        except DistillError as exc:
            problems.append(str(exc))
    if problems:
        raise _aggregate(problems)
    return tuple(planned)


def _plan_step(raw: Any, order: int) -> DistilledStep:
    """One step of the plan, validated against the contract the capture stages enforce."""
    if not isinstance(raw, Mapping):
        raise DistillError(f"第 {order} 步必须是对象：{{run, from?}}、"
                           f"{{await, output, from?}}、{{llm, output, input?, from?}}、"
                           f"{{tool, args, output, from?}} 或 {{human, output, from?}}")
    forms = [key for key in ("run", "await", "llm", "tool", "human") if key in raw]
    if len(forms) > 1:
        raise DistillError(
            f"第 {order} 步不能同时声明 {'、'.join(forms)}，只能取其一")
    derived = _derived_from(raw, order)
    keep = _keep_flag(raw, order)
    if "await" in raw:
        receipt = raw.get("await")
        output = raw.get("output")
        if not isinstance(receipt, str) or not receipt.strip():
            raise DistillError(f"第 {order} 步的 await 必须是异步工具产出的回执路径")
        if not isinstance(output, str) or not output.strip():
            raise DistillError(f"第 {order} 步的 await 必须用 output 声明等待到的真实产物路径")
        return DistilledStep(
            order=order,
            run=f"await {receipt.strip()} -> {output.strip()}",
            derived_from=derived,
            reads=(receipt.strip(),),
            writes=(output.strip(),),
            kind=KIND_RUN,
            verify=_verify_command(raw, order),
            keep=keep,
            await_receipt=receipt.strip(),
        )
    if "tool" in raw:
        name = _tool_name(raw, order)
        reads, output, args = _tool_io(raw, order)
        return DistilledStep(order, f"<tool:{name}>", derived, reads, (output,),
                             kind=KIND_TOOL, tool_name=name, arguments=args, keep=keep)
    if "llm" in raw:
        step = DistilledStep(order, _llm_text(raw, order), derived,
                             *_llm_io(raw, order), kind=KIND_LLM,
                             verify=_verify_command(raw, order), keep=keep)
        _check_llm_prompt(step)
        return step
    if "human" in raw:
        # A work order is prose for a person: it may name the project and the job
        # without echoing every declared path. The declaration still binds the
        # artifact, and the review reports a work order that never mentions one.
        return DistilledStep(order, _human_text(raw, order), derived,
                             *_llm_io(raw, order, label="human"), kind=KIND_HUMAN,
                             keep=keep)
    run = _run_text(raw, order)
    command = parse_command(run)
    _check_command(run, command, order)
    reads, writes = _run_io(raw, order, run, command)
    return DistilledStep(order, run, derived, reads, writes,
                         verify=_verify_command(raw, order), keep=keep)


def _aggregate(problems: Sequence[str]) -> DistillError:
    """One submission, every problem: a five-round validation loop is a slow teacher."""
    if len(problems) == 1:
        return DistillError(problems[0])
    listed = "\n".join(f"{index}. {text}" for index, text in enumerate(problems, start=1))
    return DistillError(
        f"沉淀校验发现 {len(problems)} 个问题，请一次改完再提交：\n{listed}")


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
            for path in (*step.write_paths, *step.reported_writes):
                # Last writer wins, the same rule `slice.producer_map` uses. A later
                # step overwrites the earlier product; if the two stages disagreed
                # about which step owns a path, the chain check would contradict the
                # reverse slice on the same trace.
                produced[paths.display(path)] = (trace.trace_id, step.seq, step.kind,
                                                 step.command, step)
    given = bindings(params)
    # A step that writes `{out}` produces the path that placeholder binds to, so a
    # later step reading either spelling is fed by this plan, not by the session.
    plan_paths = {paths.display(canonical_path(path, given))
                  for step in planned for path in step.writes}
    missing: list[str] = []
    boundaries: list[tuple[str | None, str]] = []
    declared: set[str] = set()
    for order, step in enumerate(planned, start=1):
        for path in step.reads:
            key = placeholder_key(path)
            raw = given.get(key, "") if key else path
            if not raw:
                continue
            concrete = paths.display(raw)
            if concrete in plan_paths:
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
                            (
                                f"链路用到的脚本 `{concrete}` 在会话里由 "
                                f"`{trace_id}:{seq}` 产出，该步骤未固化；"
                                "脚本随包搬运，读不到时仍按输入边界处理。"
                            )))
                    else:
                        boundaries.append((key,
                            (
                                f"链路以 `{concrete}` 为输入边界（参数 `{key}`），"
                                f"它在会话里由 `{trace_id}:{seq}` 产出，该步骤未固化；"
                                "要完整链路请把它补进 steps。"
                            )))
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
        (
            f"它是路径就写成 `{{{_literal_key(flag or value)}}}` 占位符，"
            "并在 params 里给出它在会话里的真实值"
            "（params 里的相对值按会话工作目录解析）；"
        ),
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
    against `/datasets/x`), so any long-enough tail of the path counts as naming
    it.
    """
    segments = [part for part in path.strip().strip("\"'`").split("/") if part]
    if not segments:
        return False
    for depth in range(min(len(segments), 4), 1, -1):
        if "/".join(segments[-depth:]) in text:
            return True
    return len(segments) == 1 and segments[0] in text


def _data_paths(paths_: Sequence[str], given: Mapping[str, str],
                *, writing: bool) -> set[str]:
    """The canonical paths of a step that carry data, scripts left out.

    A script is a fixed artifact the chain re-points at rather than a dataset, so two
    steps that touch the same `.py` file are not chained by data: a local trial run
    sharing a pipeline script with the planned submission is not a truncated chain.

    Spellings go through `paths.display` so `/datasets/x`, `datasets/x` and the
    old `storage/datasets/x` compare as the one file they are; comparing raw
    strings made a producer look unconsumed and invited a `keep` that hid the
    real mismatch.
    """
    result: set[str] = set()
    for item in paths_:
        resolved = canonical_path(item, given)
        if role(resolved, writing=writing) == ROLE_SCRIPT:
            continue
        result.add(paths.display(resolved))
    return result


def _check_unconsumed(planned: Sequence[DistilledStep], traces: Sequence[Trace],
                      params: Mapping[str, Any] | None,
                      final_artifact: str | None) -> None:
    """A listed step has to matter: consumed downstream, kept on purpose, or the output.

    The reverse slice drops every step whose product no later step reads. That is right
    for a superseded attempt, and wrong for a step whose effect is the point — creating
    the Label Studio project the person then reviews in, submitting a platform job. The
    slice must never decide that silently, so an unconsumed step is either declared with
    `keep: true` or left out of the plan.

    The session decides what counts as a chain step at all: a plan that starts at what a
    person handed back leaves the front half out on purpose, which the scope check
    already reviews, so those steps are not a silent drop. Without source traces there is
    no such witness, and nothing is blocked.
    """
    if not traces:
        return
    given = bindings(params)
    output = canonical_path(final_artifact, given) if final_artifact else None
    handover = {canonical_path(path, given)
                for _, step in _handover_steps(planned, traces, params)
                for path in step.write_paths}
    problems: list[str] = []
    for index, step in enumerate(planned):
        if step.keep:
            continue
        writes = _data_paths(step.writes, given, writing=True)
        if not writes or writes & handover or (output is not None and output in writes):
            continue
        consumed: set[str] = set()
        for later in planned[index + 1:]:
            consumed |= _data_paths(later.reads, given, writing=False)
        if writes & consumed:
            continue
        produced = "、".join(f"`{path}`" for path in sorted(writes))
        problems.append(
            f"第 {step.order} 步的产出 {produced} 没有被链路里的任何后续步骤消费，"
            "反向切片会把它当辅助步骤丢掉。按这个顺序确认，别急着加 keep：\n"
            "1. 后续步骤读的其实是同一个文件、只是写法不同（`/target-workspace/datasets/x` 与 "
            "`/datasets/x` 是同一个对象）——那就把两处写成同一种路径；\n"
            "2. 它的产出本身有意义（建项目产生的 project_id、提交任务产生的 receipt），"
            "只是没有被下一步读——那就把它声明成资产输出，而不是 keep；\n"
            "3. 只有纯副作用、产出对外无意义时，才加 `keep: true`。\n"
            "另外两种常见情形：这一步只是本地试跑或探测（只给小样用的 head、采样脚本），"
            "把它从 steps 里删掉、产出写成 `{参数名}` 并在 params 里声明成根输入；"
            "产出是交给人工复核/导出的材料，就把人工门禁写成 human 步骤"
            "（input=这一步的产出），不要让人工产物当链路的根输入。")
    if problems:
        raise _aggregate(problems)


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
    superseded = {
        index
        for index, (_, step) in enumerate(uncovered)
        if _superseded_by_plan(step, planned, given)
    }
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
            touches_plan = (
                index not in superseded
                and bool(writes[index] & planned_reads
                         or reads[index] & planned_writes)
            )
            touches_peer = any(writes[index] & reads[other]
                               or reads[index] & writes[other] for other in connected)
            if touches_plan or touches_peer:
                connected.add(index)
                changed = True
    return ([item for index, item in enumerate(uncovered) if index in connected],
            [item for index, item in enumerate(uncovered) if index not in connected])


_CLEANUP_COMMANDS = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown"}
_SHELL_OPERATORS = {">", ">>", "<", "<<", "2>", "2>>", "|", "||", "&&", ";"}
_SUDO_OPTIONS_WITH_VALUE = {
    "-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t", "-U"
}


def _without_shell_wrappers(words: list[str]) -> list[str]:
    """Drop wrappers that do not change the business command's data flow."""
    index = 0
    while index < len(words):
        name = PurePosixPath(words[index]).name
        if name == "sudo":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if option == "--":
                    break
                if option in _SUDO_OPTIONS_WITH_VALUE and index < len(words):
                    index += 1
            continue
        if name == "nice":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                if words[index] == "-n":
                    index += 2
                else:
                    index += 1
            if index < len(words) and words[index] == "--":
                index += 1
            continue
        if name == "command":
            if index + 1 >= len(words) or words[index + 1].startswith("-"):
                break
            index += 1
            continue
        break
    return words[index:]


def _join_shell(words: list[str]) -> str:
    return " ".join(
        word if word in _SHELL_OPERATORS else shlex.quote(word)
        for word in words
    )


def _meaningful_shell(command: str) -> str | None:
    """The business command without setup/cleanup clauses surrounding a retry."""
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:&&|;|\n)\s*", command)
        if part.strip()
    ]
    kept: list[str] = []
    for part in parts:
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        words = _without_shell_wrappers(words)
        if not words or PurePosixPath(words[0]).name in _CLEANUP_COMMANDS:
            continue
        kept.append(_join_shell(words))
    return " && ".join(kept) or None


def _superseded_by_plan(
    step: Any,
    planned: Sequence[DistilledStep],
    given: Mapping[str, str],
) -> bool:
    """A retry with the same business command and data flow is a side branch.

    Agents often prefix a retry with `rm -rf`/`mkdir -p` or a cache cleanup. Those
    clauses are not chain steps; comparing the whole shell string would make the
    retry look like a new continuation and force the user to re-confirm a scope they
    already settled.
    """
    if step.kind != KIND_SHELL:
        return False
    command = _meaningful_shell(step.command)
    if command is None:
        return False
    trace_command = parse_command(command)
    trace_reads = _data_paths(trace_command.read_paths, given, writing=False)
    trace_writes = _data_paths(trace_command.write_paths, given, writing=True)
    for item in planned:
        if item.kind != KIND_RUN:
            continue
        planned_command = _meaningful_shell(item.run)
        if planned_command != command:
            continue
        planned_reads = _data_paths(item.reads, given, writing=False)
        planned_writes = _data_paths(item.writes, given, writing=True)
        if trace_reads == planned_reads and trace_writes == planned_writes:
            return True
    return False


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
        if step.await_receipt is not None:
            rewritten.append(step)
            continue
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
                                           arguments=args, keep=step.keep))
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
        # Re-parsing after freezing a script placeholder must not turn an
        # explicitly declared positional output back into an input.
        declared = tuple(path for path in step.writes if path in run)
        writes = tuple(dict.fromkeys((*command.write_paths, *declared)))
        reads = tuple(path for path in command.read_paths if path not in declared)
        rewritten.append(DistilledStep(step.order, run, step.derived_from,
                                       reads, writes, kind=step.kind,
                                       verify=step.verify, keep=step.keep))
    materialized = tuple(sorted((key, value) for key, value in frozen.items()
                                if key in replaced))
    return (tuple(rewritten), {key: value for key, value in given.items()
                               if key not in replaced},
            materialized, tuple(notes))


def _bound_values(package: Package, planned: Sequence[DistilledStep],
                  params: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
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
    given = tuple((str(key), value) for key, value in params.items())
    keys = {key for key, _ in given}
    used: set[str] = set()
    for step in planned:
        for path in (*step.reads, *step.writes):
            key = placeholder_key(path)
            if key is not None:
                used.add(key)
    # Root parameters are derived from path segments, so their names can be a
    # directory (`test_100`) that a caller has no way to guess. The package still
    # holds the concrete path the trace read, so bind it rather than failing the
    # confirmation on a key nobody could have supplied.
    if expected - keys:
        derived = {
            key: value for key, value in package.params
            if key not in keys and not placeholder_key(value)
        }
        if derived:
            given = (*given, *derived.items())
            keys = {key for key, _ in given}
    # Extra keys are harmless: they are either already bound into the graph or
    # simply unused. A missing root is the defect worth stopping for.
    if expected - keys:
        raise DistillError(
            f"params 必须覆盖链路的根输入：需要 {sorted(expected)}，"
            f"收到 {sorted(keys)}。根输入来自命令里的路径与 {{占位符}}")
    normalized: list[tuple[str, Any]] = []
    for key, value in given:
        if key not in expected:
            continue
        spec = package.parameter_specs.get(key)
        normalized.append(
            (key, value if spec is None else normalize_value(spec.type, value, key=key))
        )
    return tuple(normalized)


def _template_path_issues(
    planned: Sequence[DistilledStep],
    params: Mapping[str, Any] | None = None,
    final_artifact: str | None = None,
) -> list[str]:
    """Allow a directory parameter followed by a safe relative artifact path.

    The supported reusable form is ``{data_dir}/manifest.jsonl``: the root path
    is a user input, while the suffix is a deterministic artifact location beneath
    that directory. Templates in filenames, middle path segments, or prefixed by
    a mount/object root remain invalid because they cannot be mapped unambiguously.
    """
    matches: list[tuple[int, str]] = []
    missing: set[str] = set()
    seen: set[str] = set()
    values = params or {}
    paths_to_check = [
        (step.order, path)
        for step in planned
        for path in (*step.reads, *step.writes)
    ]
    if final_artifact is not None:
        paths_to_check.append((len(planned) or 1, final_artifact))
    for order, path in paths_to_check:
        if path in seen or placeholder_key(path) is not None or _TEMPLATE_TOKEN.search(path) is None:
            continue
        seen.add(path)
        template = path_template(path)
        if template is None:
            matches.append((order, path))
            continue
        key, _ = template
        if key not in values or not isinstance(values[key], str) or not values[key].strip():
            missing.add(key)
    problems: list[str] = []
    if matches:
        examples = "；".join(f"第 {order} 步 `{path}`" for order, path in matches[:3])
        remainder = f"；另有 {len(matches) - 3} 个同类路径" if len(matches) > 3 else ""
        problems.append(
            f"发现 {len(matches)} 个不支持的路径模板：{examples}{remainder}。"
            "路径变量只支持两种形式：整个路径是 `{input_path}`，或数据集根目录位于开头、"
            "后接安全相对路径（`{data_dir}/manifest.jsonl`）。"
            "不要把变量嵌进文件名，也不要在它前面写 `/datasets/` 或 `/target-workspace/`；"
            "`data_dir` 的 params 值本身就是完整 Storage 对象路径。"
        )
    if missing:
        problems.append(
            "这些路径模板根目录必须在 params 中绑定为完整目录路径："
            + "、".join(f"`{key}`" for key in sorted(missing))
        )
    return problems


def _intermediate_token_issues(
    planned: Sequence[DistilledStep], params: Mapping[str, Any] | None
) -> list[str]:
    """A chain's own product is decided by its producer, not by a `{token}`.

    Writing an intermediate product as a placeholder leaves emit with no literal file
    name, so `artifact.at` cannot tell where the file belongs. That only surfaces later:
    a manifest that names sibling images sits in the run directory while the images stay
    in Storage, and the platform preflight rejects it as "relative image path not found".
    A placeholder bound to `params` is a declared root input and stays legal.
    """
    given = bindings(params)
    read_keys = {
        key for step in planned for path in step.reads
        if (key := placeholder_key(path)) is not None
    }
    issues: list[str] = []
    for step in planned:
        # A run step's product is whatever its command writes; a human or llm step
        # declares where its answer lands, so a `{token}` there is the only spelling
        # that makes sense.
        if step.kind != KIND_RUN:
            continue
        for path in step.writes:
            key = placeholder_key(path)
            if key is None or key in given or key not in read_keys:
                continue
            issues.append(
                f"第 {step.order} 步的产出 `{path}` 是链路中间产物，但没有在 params 中绑定路径。"
                "若产物应跟随数据集目录，请改成 `{data_dir}/相对文件名`，并在 params 中把"
                "data_dir 绑定为完整目录；编译后的 artifact 会落在每次运行输入目录下。"
                "否则，把该占位符作为独立完整路径绑定到 params。不要把中间产物当作额外根输入。"
            )
    return issues


def _step_material(step: DistilledStep) -> list[Any]:
    """What makes one step distinct, for the trace id a retry of the plan reuses."""
    if step.kind == KIND_TOOL:
        return [step.kind, step.tool_name, dict(step.arguments or {}), list(step.writes)]
    return ["await", step.await_receipt, list(step.writes)] if step.await_receipt else [step.kind, step.run]


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
    kept = {"keep": True} if step.keep else {}
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
            **kept,
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
            **kept,
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
            **kept,
        }
    command = step.run
    if step.await_receipt is not None:
        if len(step.writes) != 1:
            raise DistillError("await 节点必须声明且仅声明一个真实产物 output")
        command = f"cat {shlex.quote(step.await_receipt)} > {shlex.quote(step.writes[0])}"
    entry = {
        "kind": "tool",
        "entry_id": entry_id,
        "timestamp": None,
        "tool": "terminal",
        "call_id": f"distilled-{step.order}",
        "args": {"command": command},
        "result": {"is_error": False, "text": "distilled"},
        **kept,
    }
    # A declared product the command line does not spell as a write would read as an
    # input downstream, so the entry carries the declaration into the capture stages.
    parsed = set(parse_command(step.run).write_paths)
    declared = [path for path in step.writes if path not in parsed]
    if declared:
        entry["declared_writes"] = declared
    return entry


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
    return {
        str(key): value
        for key, value in (params or {}).items()
        if isinstance(value, str)
    }


def _path_reads(
    step: DistilledStep,
    params: Mapping[str, Any] | None,
    input_contract: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Reads that really name files, excluding typed scalar placeholders."""
    try:
        specs = contracts_to_specs(input_contract)
    except ParameterError:
        specs = {}
    values = params or {}
    given = bindings(params)
    reads: list[str] = []
    for path in step.reads:
        key = placeholder_key(path)
        if key is not None:
            spec = specs.get(key)
            if spec is not None and spec.type != "path":
                continue
            if spec is None and key in values and not isinstance(values[key], str):
                continue
        resolved = canonical_path(path, given)
        if resolved not in reads:
            reads.append(resolved)
    return tuple(reads)


def _same_path(left: str, right: str) -> bool:
    """Compare path spellings through the canonical (namespace, path)."""
    return paths.same(left, right)


_PATH_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _looks_like_path(value: str) -> bool:
    """Whether a token is a file reference at all.

    A bare word reaches the plan as a read whenever the parser cannot tell a name
    from a path — `project_ref: pcb_prelabel_10_review`, a model profile, a task
    name. Probing the workspace for those turns a scalar into a fake missing
    input, so only path-shaped tokens are checked.
    """
    text = value.strip()
    if not text or text.startswith("-"):
        return False
    if text.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in text or "\\" in text:
        return True
    return bool(_PATH_SUFFIX.search(text))


def _spell_out(path: str, bindings: Mapping[str, str]) -> str:
    concrete = canonical_path(path, bindings)
    return f"`{path}`" if concrete == path else f"`{path}`（= `{concrete}`）"


def _is_artifact_path(value: str) -> bool:
    if placeholder_key(value) is not None:
        return True
    if value != value.strip() or any(char.isspace() for char in value):
        return False
    if any(char in '，。；：、（）[]<>"\'`' for char in value):
        return False
    if value.startswith(("http://", "https://")):
        return False
    return value.startswith(("/", "./", "../", "~/")) or "/" in value


def _final_artifact_issue(
    final_artifact: str,
    steps: Sequence[DistilledStep],
    params: Mapping[str, Any] | None,
) -> str | None:
    given = bindings(params)
    outputs = [
        path
        for step in steps
        for path in step.writes
    ]
    available = "、".join(_spell_out(path, given) for path in outputs[:12])
    if not _is_artifact_path(final_artifact):
        return (
            f"final_artifact `{final_artifact}` 必须是单个产物路径，"
            "不能是描述文本；"
            + (f"请从这些步骤产出中选择：{available}" if available else "计划没有声明任何产出")
        )
    wanted = canonical_path(final_artifact, given)
    if any(canonical_path(path, given) == wanted for path in outputs):
        return None
    return (
        f"final_artifact `{final_artifact}` 目前没有对应步骤产出；"
        + (f"可用产出：{available}" if available else "计划没有声明任何产出")
    )


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
            token = canonical_path(path, given)
            if paths.same(token, wanted):
                return path
    # A script that publishes beside its input names the product itself, so the
    # declared artifact and the step's own product are the same file under two
    # spellings the plan never had to repeat.
    name = PurePosixPath(paths.display(wanted)).name
    for step in steps:
        for path in step.writes:
            if PurePosixPath(paths.display(canonical_path(path, given))).name == name:
                return path
    produced = [_spell_out(path, given) for step in steps for path in step.writes]
    detail = ("计划里的产出：" + "、".join(produced[:8])
              if produced else "计划里没有任何产出")
    raise DistillError(
        f"final_artifact `{final_artifact}` 没有被任何步骤声明为产出；{detail}。"
        f"把产物路径写到该步骤的 output，并让它出现在命令行上（`--output 路径`、`> 路径`）；"
        f"脚本自己决定输出位置时，先给脚本加一个输出参数。{OUTPUT_CONTRACT_HINT}")


def _step_semantics(
    step: DistilledStep,
    params: Mapping[str, Any] | None,
    input_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The part of a step preflight may not change without another review."""
    given = bindings(params)
    data: dict[str, Any] = {
        "step": step.order,
        "kind": "await" if step.await_receipt is not None else step.kind,
        "source": step.derived_from,
        "inputs": sorted(_path_reads(step, params, input_contract)),
        "outputs": sorted(canonical_path(path, given) for path in step.writes),
        "keep": step.keep,
        "verify": step.verify,
    }
    if step.kind == KIND_TOOL:
        data["tool"] = step.tool_name
    return data


def _expected_graph_steps(
    steps: Sequence[DistilledStep],
    final_artifact: str | None,
    params: Mapping[str, Any] | None,
) -> tuple[int, ...]:
    """The one-based steps the final artifact depends on.

    This mirrors the reverse slice using only declared reads and writes. It lets the
    review name which steps will enter the graph without materializing scripts or
    compiling a package.
    """
    if not steps:
        return ()
    if final_artifact is None:
        return tuple(step.order for step in steps)
    given = bindings(params)

    def path_key(path: str) -> str:
        return paths.display(canonical_path(path, given))

    producers: dict[str, int] = {}
    for order, step in enumerate(steps):
        for path in step.writes:
            producers[path_key(path)] = order
    root = producers.get(path_key(final_artifact))
    if root is None:
        return ()
    kept = {root, *(order for order, step in enumerate(steps) if step.keep)}
    stack = list(kept)
    while stack:
        order = stack.pop()
        for path in steps[order].reads:
            producer = producers.get(path_key(path))
            if producer is not None and producer < order and producer not in kept:
                kept.add(producer)
                stack.append(producer)
    return tuple(order + 1 for order in sorted(kept))


def _script_references(steps: Sequence[DistilledStep],
                       params: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Scripts named by the plan. Preflight, not planning, reads their bytes."""
    given = bindings(params)
    found: dict[str, dict[str, str]] = {}
    for step in steps:
        for path in step.reads:
            resolved = canonical_path(path, given)
            if role(resolved, writing=False) != ROLE_SCRIPT:
                continue
            found.setdefault(
                resolved,
                {
                    "path": path,
                    "resolved": resolved,
                    "status": "preflight_pending",
                },
            )
    return list(found.values())


def _cardinality_in(key: str) -> str | None:
    match = _CARDINALITY.search(key)
    return match.group(1) if match else None


def _cardinality_issue(key: str, count: str) -> str:
    """A path parameter must not advertise the size of the run that produced it."""
    stem = _CARDINALITY.sub("_", key).replace("__", "_").strip("_")
    return (
        f"参数 `{key}` 的名字里带了记录数 `{count}`：参数名描述角色，不描述那次运行的条数，"
        f"否则模板就被固化成只处理 {count} 条。请把它改名成 `{stem or 'input'}`，"
        "把条数交给 execution_scope.records（或一个 number 参数）；"
        "如果这个文件本身是试跑切片（head/split/shuf 的产物），"
        "请改用它的源文件作为根输入，让模板按新输入重新决定条数。"
    )


def _is_async_tool(name: str) -> bool:
    return is_async_tool(name)


def _awaits(step: DistilledStep, receipt: str | None) -> bool:
    """Whether this step is what turns a task handle into a real product.

    The wait is a *data* relationship, not a script name: whatever command takes
    the receipt and writes the platform product is the await step. A name check
    stays as a fallback for chains that spell it `wait_*.py`, but a workspace is
    free to call it `poll_task.py` or `df_await.py`.
    """
    if step.await_receipt is not None:
        return receipt is None or _same_path(step.await_receipt, receipt)
    if step.kind == KIND_TOOL:
        return bool(_WAIT_TOOL.search(step.tool_name or ""))
    if step.kind != KIND_RUN:
        return False
    if receipt and any(_same_path(path, receipt) for path in step.reads):
        return True
    return bool(_WAIT_SCRIPT.search(step.run))



_SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".ts", ".mjs", ".cjs")
# A step whose whole job is moving the platform's product to a stable name is a
# materialization, not a transformation: turning it into the await loses nothing.
_COPY_COMMANDS = frozenset({"cp", "mv", "rsync"})
# A plan may annotate its copy with echo/count output; stdout is not an artifact.
_NOOP_COMMANDS = frozenset({"echo", "true", ":", "wc"})


def _bind_submit_receipts(
    steps: Sequence[DistilledStep], given: Mapping[str, Any] | None
) -> tuple[DistilledStep, ...]:
    """Point the step after an async submit at the receipt it has to consume.

    The platform fills its run directory only after the task ends, and that
    directory is named per run (``data_preparation/<uuid>/``), so a replay can
    never reproduce it. A step copying from there is really waiting for the
    receipt — but the receipt is named by the engine
    (``<artifacts>/<trace>/<seq>-<tool>.json``), which a plan cannot spell.

    Rebinding that one read keeps a correct chain recognisable as an await node
    instead of rejecting it over a path the author had no way to know.
    """
    bound = given or {}
    bindings = {
        canonical_path(value, bound)
        for value in bound.values()
        if isinstance(value, str)
    }
    produced = {canonical_path(path, bound) for step in steps for path in step.writes}
    result: list[DistilledStep] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if (
            step.kind == KIND_TOOL
            and _is_async_tool(step.tool_name or "")
            and step.writes
            and index + 1 < len(steps)
        ):
            following = steps[index + 1]
            if following.kind == KIND_RUN:
                receipt = step.writes[0]
                rebindable = [
                    path
                    for path in following.reads
                    if PurePosixPath(path).suffix.lower() not in _SCRIPT_SUFFIXES
                    and not any(_same_path(path, receipt) for _ in (receipt,))
                    and canonical_path(path, bound) not in produced
                    and canonical_path(path, bound) not in bindings
                ]
                # Exactly one candidate, and the step only copies it: the await
                # node yields that product, so dropping the command loses nothing.
                # A step that also transforms it keeps the old contract — it has
                # to read the receipt itself, or the transform would disappear.
                if len(rebindable) == 1 and _is_pure_copy(following.run, rebindable[0]):
                    stale = rebindable[0]
                    result.append(step)
                    # The command has to carry the receipt too: the steps are
                    # re-parsed from their text downstream, so a read that only
                    # exists in `reads` is dropped again.
                    destination = next(
                        (path for path in following.writes if not _same_path(path, stale)),
                        None,
                    )
                    if destination is None:
                        result.append(step)
                        index += 1
                        continue
                    result.append(
                        replace(
                            following,
                            run=f"cat {shlex.quote(receipt)} > {shlex.quote(destination)}",
                            reads=(receipt,),
                        )
                    )
                    index += 2
                    continue
        result.append(step)
        index += 1
    return tuple(result)


def _is_pure_copy(command: str, source: str) -> bool:
    """Whether the command only copies `source`, plus no-op annotations.

    `cp receipt out.jsonl; echo waiting for the callback` is still a
    materialization: the echo says what the step is for, it does not do anything.
    """
    segments = [part.strip() for part in re.split(r"&&|\|\||;", command) if part.strip()]
    if not segments:
        return False
    head = segments[0]
    if any(operator in head for operator in ("|", ">", "<")):
        return False
    try:
        words = shlex.split(head)
    except ValueError:
        return False
    if not words or PurePosixPath(words[0]).name not in _COPY_COMMANDS:
        return False
    if not any(word == source for word in words[1:]):
        return False
    for segment in segments[1:]:
        if any(operator in segment for operator in ("|", ">", "<")):
            return False
        try:
            annotation = shlex.split(segment)
        except ValueError:
            return False
        if not annotation or PurePosixPath(annotation[0]).name not in _NOOP_COMMANDS:
            return False
    return True

def _async_wait_issues(steps: Sequence[DistilledStep]) -> list[str]:
    """A submit tool hands back a receipt, not the product.

    An async platform tool returns as soon as the platform accepts the job; the
    work keeps going there. A chain that reads the product straight after it is
    reading a directory the platform has not filled yet, so every later step
    fails. The await step is what turns the receipt into a real artifact, and it
    is identified by consuming the receipt — not by what the script is called.
    """
    issues: list[str] = []
    for index, step in enumerate(steps):
        if step.kind != KIND_TOOL or not _is_async_tool(step.tool_name or ""):
            continue
        receipt = step.writes[0] if step.writes else None
        if any(_awaits(item, receipt) for item in steps[index + 1:]):
            continue
        handle = receipt or "<受理单>"
        issues.append(
            f"第 {step.order} 步 `{step.tool_name}` 是异步任务：它的产物只是任务句柄，"
            "任务还在平台侧跑，真实产物此时还不存在。必须紧跟一个等待步骤：由它读受理单 "
            f"`{handle}` 并写出真实产物，把该产物声明成这一步的 output，后续步骤只读这个产物。"
            "判据是这一步消费了受理单，脚本名不限；沉淀会把该步骤固化成引擎的 await 节点"
            "（不占终端、不占 worker，由任务回调唤醒），不会固化成轮询脚本。"
            "缺少等待步骤时，回放会在任务还没结束就进入下一个节点，下游必然报错。"
        )
    return issues


def _slice_boundary_issues(
    traces: Sequence[Trace],
    params: Mapping[str, Any] | None,
) -> list[str]:
    """A root input that a slicing command produced is a trial run, not data.

    `head -10 manifest.jsonl > manifest_10.jsonl` exists to keep a trial small.
    Taking its output as the chain's root input bakes that decision into the
    template: the graph can only ever process the same records. The source file
    is the boundary; how many records to process belongs to the run.
    """
    if not traces or not params:
        return []
    roots = [(key, str(value)) for key, value in params.items() if isinstance(value, str)]
    written_at: list[tuple[str, int]] = []
    for trace in traces:
        for step in trace.steps:
            for path in (*step.write_paths, *step.reported_writes):
                written_at.append((path, step.seq))
    issues: list[str] = []
    for trace in traces:
        for step in trace.steps:
            command = step.command if isinstance(step.command, str) else ""
            if not command or not _SLICE_COMMAND.search(command):
                continue
            for written in (*step.write_paths, *step.reported_writes):
                matched = [key for key, value in roots if _same_path(value, written)]
                if not matched:
                    continue
                key = matched[0]
                source = next(
                    (path for path in step.read_paths
                     if path != written and PurePosixPath(path).suffix),
                    None,
                )
                match = _SLICE_COMMAND.search(command)
                snippet = command[match.start():match.start() + 70] if match else command[:70]
                producer = next(
                    (seq for path, seq in written_at
                     if source and _same_path(path, source) and seq != step.seq),
                    None,
                )
                if source and producer is not None:
                    # The source is session-generated too, so naming it only moves
                    # the same mistake one step upstream.
                    boundary = (
                        f"`{source}` 也是第 {producer} 步产出的，真正的根输入还要往前推到"
                        "用户提供的原始数据（例如待处理的图片目录），并把中间这几步都固化成脚本。"
                    )
                elif source:
                    boundary = f"请改用它的源文件 `{source}` 作为根输入，"
                else:
                    boundary = "请改用源文件作为根输入，"
                issues.append(
                    f"参数 `{key}` 指向 `{written}`，它是会话里第 {step.seq} 步的试跑切片"
                    f"（`{snippet}`），不是源数据。把切片当根输入，模板就只能处理这批样本；"
                    + boundary
                    + "把处理条数交给 execution_scope.records（或一个 number 参数），"
                    "切片逻辑本身作为链路里的一个步骤。"
                )
    return list(dict.fromkeys(issues))


def _generated_root_issues(
    traces: Sequence[Trace],
    params: Mapping[str, Any] | None,
    steps: Sequence[DistilledStep],
) -> list[str]:
    """A root input the session itself produced is not an input.

    The chain starts from the files the user brought. A file this session wrote —
    a manifest it assembled, a review file it derived — belongs to a step, not to
    the contract: taking it as a root freezes the template to that one batch and
    hides the step that made it, which is usually the step that needs a script.
    Slicing commands are reported by their own check.
    """
    if not traces or not params:
        return []
    given = bindings(params)
    produced = {
        canonical_path(path, given) for step in steps for path in step.writes
    }
    issues: list[str] = []
    for key, value in params.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if role(value, writing=False) == ROLE_SCRIPT:
            # A script the session wrote is a boundary the package materializes,
            # not data the user brings; its own check decides how it is frozen.
            continue
        if any(_same_path(value, path) for path in produced):
            continue
        found = None
        for trace in traces:
            for step in trace.steps:
                command = step.command if isinstance(step.command, str) else ""
                if command and _SLICE_COMMAND.search(command):
                    continue
                written = (*step.write_paths, *step.reported_writes)
                if any(_same_path(value, path) for path in written):
                    found = step
                    break
            if found is not None:
                break
        if found is None:
            continue
        issues.append(
            f"参数 `{key}` 指向 `{value}`，它是会话里第 {found.seq} 步产出的文件，"
            "不是用户提供的输入。根输入只能是用户自己拿来的文件（例如要处理的图片目录）；"
            f"请把第 {found.seq} 步固化成链路里的一步，由脚本产出这个文件，"
            f"再把 `{key}` 换到真正的源数据上。"
        )
    return list(dict.fromkeys(issues))


def _relocation_issues(
    steps: Sequence[DistilledStep],
    params: Mapping[str, Any] | None,
) -> list[str]:
    """A manifest derived from Storage must not move out of its directory.

    A JSONL manifest addresses its images relative to the manifest's own
    directory. Slicing one into the run directory keeps the relative paths but
    changes what they resolve to, so the platform's pre-submit check finds no
    images and the replay dies at the first tool call. The product belongs back
    in the Storage directory it came from.
    """
    given = bindings(params)
    issues: list[str] = []
    for step in steps:
        for written in step.writes:
            namespace, path = paths.canonical(canonical_path(written, given))
            if namespace != paths.WORKSPACE or not path.endswith(".jsonl"):
                continue
            for read in step.reads:
                source_ns, source = paths.canonical(canonical_path(read, given))
                if source_ns != paths.STORAGE or not source.endswith(".jsonl"):
                    continue
                if PurePosixPath(source).parent == PurePosixPath(path).parent:
                    continue
                issues.append(
                    f"第 {step.order} 步把 `{path}` 写到了运行目录，但它是从 Storage 清单 "
                    f"`{source}` 派生的。清单里的图片等相对路径是按清单所在目录解析的，"
                    f"换到运行目录就失效，平台提交前预检会报「清单目录下找不到图片」。"
                    f"把这一步的 output 写成 `/{PurePosixPath(source).parent}/"
                    f"{PurePosixPath(path).name}`，或让脚本把相对路径改写成绝对路径。"
                )
    return list(dict.fromkeys(issues))


def _spelling_issues(draft: DistillDraft) -> list[str]:
    """A plan may not remember a sandbox path.

    `/target-workspace/.pyromind-agent/<id>/…` is what a sandbox prints for the
    conversation's own directory. The platform addresses Storage by its object
    path (`/datasets/…`), so a plan carrying the mount leaks a deployment detail
    into every replay.
    """
    issues: list[str] = []
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for key, value in (draft.params or {}).items():
        if isinstance(value, str):
            candidates.append((f"参数 `{key}`", value))
    for step in draft.steps:
        # A `cd` target is the execution context the command runs in, not a file
        # the plan hands to a later step, so it is not a contract path.
        stepped_into = {
            match.group(1).strip("\"'")
            for match in _CD_TARGET.finditer(step.run or "")
        }
        for path in (*step.reads, *step.writes):
            if "{" in path:
                continue
            if any(_same_path(path, target) for target in stepped_into):
                continue
            candidates.append((f"第 {step.order} 步的路径", path))
    for where, value in candidates:
        if value in seen:
            continue
        seen.add(value)
        if paths.mount_spelling(value):
            issues.append(
                f"{where} 用了沙箱挂载路径 `{value}`。挂载前缀属于执行环境，"
                f"平台按对象路径读 Storage；请改成 `{paths.display(value)}`，"
                "需要在沙箱里执行时由执行方自己拼挂载前缀。"
            )
    return issues


def _static_issues(draft: DistillDraft) -> tuple[list[str], list[str]]:
    """Cheap plan checks, split by whether they stop a replay.

    A plan that runs but processes a trial slice, or reads a file the session
    produced, is still worth capturing: it executes, it just bakes in a narrower
    assumption. A plan whose placeholder has no value cannot run at all. Only the
    second kind blocks — otherwise a plan is rejected on advice and never gets the
    one thing that would settle it, a real replay.
    """
    blocking: list[str] = []
    warnings: list[str] = []
    issues = blocking
    given = bindings(draft.params)
    values = draft.params or {}
    issues.extend(_template_path_issues(
        draft.steps, draft.params, draft.final_artifact))
    try:
        specs = contracts_to_specs(draft.input_contract)
    except ParameterError as exc:
        issues.append(str(exc))
        specs = {}
    for key, spec in specs.items():
        if spec.type in ("number", "records"):
            continue
        count = _cardinality_in(key)
        if count is not None:
            warnings.append(_cardinality_issue(key, count))
    issues.extend(_async_wait_issues(draft.steps))
    warnings.extend(_slice_boundary_issues(draft.traces, draft.params))
    warnings.extend(_generated_root_issues(draft.traces, draft.params, draft.steps))
    warnings.extend(_spelling_issues(draft))
    issues.extend(_relocation_issues(draft.steps, draft.params))
    read_keys = {
        key
        for step in draft.steps
        for path in step.reads
        if (key := placeholder_key(path)) is not None
    }
    for key, spec in specs.items():
        if key not in values and key in read_keys:
            issues.append(f"参数 `{key}` 缺少值")
            continue
        if key not in values:
            continue
        try:
            normalize_value(spec.type, values[key], key=key)
        except ParameterError as exc:
            issues.append(str(exc))
    for item in draft.unresolved:
        if (
            item.startswith("这些占位符没有在 params 里给出真实值")
            or item.startswith("final_artifact")
        ):
            issues.append(item)
    for step in draft.steps:
        if step.kind != KIND_HUMAN:
            continue
        # A work order may name a file by its concrete path or by the `{param}` the
        # plan bound to it. Both say the same thing; only insisting on the expanded
        # spelling forces the agent to hard-code a path the rest of the plan
        # deliberately parameterised, which is the opposite of what capture wants.
        tokens = {key for path in (*step.reads, *step.writes)
                  if (key := placeholder_key(path)) is not None}
        for path in (*_path_reads(step, draft.params, draft.input_contract), *step.writes):
            if path in step.run:
                continue
            named = next((key for key, value in bindings(draft.params).items()
                          if key in tokens and _same_path(value, path)), None)
            if named is not None and f"{{{named}}}" in step.run:
                continue
            warnings.append(
                f"人工节点第 {step.order} 步的工作单没有写明路径 `{path}`，"
                "也没有写明它在命令里的 `{占位符}` 写法"
            )
    metadata = getattr(draft.workspace, "metadata", None)
    batch = getattr(draft.workspace, "metadata_many", None)
    if not callable(metadata) and not callable(batch):
        return _split(blocking, warnings)
    produced = {
        canonical_path(path, given)
        for step in draft.steps
        for path in step.writes
    }
    checked: set[str] = set()
    pending: list[str] = []
    for step in draft.steps:
        for path in step.reads:
            key = placeholder_key(path)
            spec = specs.get(key) if key is not None else None
            if spec is not None and spec.type != "path":
                continue
            if (
                spec is None
                and key is not None
                and key in values
                and not isinstance(values[key], str)
            ):
                continue
            resolved = canonical_path(path, given)
            if (
                resolved in checked
                or any(_same_path(resolved, output) for output in produced)
                or "{" in resolved
                or "}" in resolved
            ):
                continue
            checked.add(resolved)
            if not _looks_like_path(resolved):
                # `project_ref: pcb_prelabel_10_review` reaches the plan as a read
                # because the parser cannot tell a name from a path. Asking the
                # workspace whether that word exists invents a missing input.
                continue
            pending.append(resolved)
    # One probe for the whole set: a per-path metadata call is a full sandbox
    # command, and the review used to spend minutes on a handful of files.
    answers = _metadata_answers(draft.workspace, pending, batch, metadata)
    for resolved in pending:
        info = answers.get(resolved)
        if isinstance(info, Mapping) and info.get("exists") is False:
            issues.append(f"输入不存在：`{resolved}`")
        elif isinstance(info, Mapping) and info.get("error"):
            warnings.append(f"无法确认输入 `{resolved}` 是否存在：{info['error']}")
    return _split(blocking, warnings)


def _split(blocking: list[str], warnings: list[str]) -> tuple[list[str], list[str]]:
    return (list(dict.fromkeys(blocking)), list(dict.fromkeys(warnings)))


def _static_blocking_issues(draft: DistillDraft) -> list[str]:
    """The checks that stop a confirmation; kept for callers that only ask those."""
    return _static_issues(draft)[0]


def _metadata_answers(
    workspace: Any,
    paths: Sequence[str],
    batch: Any,
    single: Any,
) -> dict[str, Any]:
    """Exists/size for every path, batched when the host can do it."""
    if not paths:
        return {}
    if callable(batch):
        try:
            return dict(batch(list(paths)))
        except Exception:  # noqa: BLE001 - fall back to one probe per path
            pass
    answers: dict[str, Any] = {}
    for path in paths:
        try:
            answers[path] = single(path)
        except Exception as exc:  # noqa: BLE001 - the host may be unavailable
            answers[path] = {"error": str(exc)}
    return answers


@dataclass(frozen=True)
class DistillDraft:
    """The user-facing capture plan before any workspace or package work happens."""

    label: str
    steps: tuple[DistilledStep, ...]
    source_traces: tuple[str, ...]
    params: Mapping[str, Any] | None
    final_artifact: str | None
    policy: str
    graph_steps: tuple[int, ...]
    branches: tuple[str, ...] = ()
    scope_warnings: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    trace_evidence: tuple[tuple[str, str], ...] = ()
    input_contract: Mapping[str, Any] | None = None
    workspace: Any = None
    traces: tuple[Trace, ...] = ()

    @property
    def semantic(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "steps": [
                _step_semantics(step, self.params, self.input_contract)
                for step in self.steps
            ],
            "source_traces": list(self.source_traces),
            "params": dict(self.params or {}),
            "trace_evidence": [
                {"path": path, "sha256": digest}
                for path, digest in self.trace_evidence
            ],
            "final_artifact": self.final_artifact,
            "policy": self.policy,
            "graph_steps": list(self.graph_steps),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.semantic,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def review(self, validation: Mapping[str, Any]) -> dict[str, Any]:
        steps = []
        for step in self.steps:
            item = _step_semantics(step, self.params, self.input_contract)
            item["command"] = step.run
            if step.kind == KIND_TOOL:
                item["args"] = dict(step.arguments or {})
            steps.append(item)
        human_gates = [
            {
                "step": step.order,
                "work_order": step.run,
                "inputs": list(_path_reads(step, self.params, self.input_contract)),
                "outputs": list(step.writes),
            }
            for step in self.steps
            if step.kind == KIND_HUMAN
        ]
        side_effects = [
            {
                "step": step.order,
                "kind": step.kind,
                "command": (
                    f"<tool:{step.tool_name}>"
                    if step.kind == KIND_TOOL
                    else step.run
                ),
                "reason": (
                    "host tool call"
                    if step.kind == KIND_TOOL
                    else "declared keep"
                ),
            }
            for step in self.steps
            if step.kind == KIND_TOOL or step.keep
        ]
        unresolved = list(self.unresolved)
        blocking, warnings = _static_issues(self)
        if not validation.get("will_start_replay"):
            blocking.append(
                "缺少有界验证回放（bounded validation replay）所需的 "
                "validation_params 和 sample/subset execution_scope；"
                "请补充最小输入后重新生成计划，否则不能确认沉淀或发布。"
            )
        blocking = list(dict.fromkeys(blocking))
        warnings = list(dict.fromkeys(warnings))
        validation = dict(validation)
        validation["ready"] = not blocking
        return {
            "plan_type": "structural",
            "label": self.label,
            "source_traces": list(self.source_traces),
            "trace_evidence": [
                {"path": path, "sha256": digest}
                for path, digest in self.trace_evidence
            ],
            "steps": steps,
            "scripts": _script_references(self.steps, self.params),
            "branches": list(self.branches),
            "scope_warnings": list(self.scope_warnings),
            "human_gates": human_gates,
            "side_effects": side_effects,
            "final_artifact": self.final_artifact,
            "expected_graph_steps": list(self.graph_steps),
            "unresolved": unresolved,
            "validation": validation,
            "blocking_issues": blocking,
            # Advice, not defects: the plan runs, it just bakes in an assumption.
            "warnings": warnings,
            "preflight": {
                "status": "pending_confirmation",
                "reads_scripts": False,
                "builds_package": False,
                "compiles_graph": False,
            },
        }

    def payload(self, validation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "structural_plan",
            "digest": self.digest,
            "review": self.review(validation),
        }


def draft_distill(
    *,
    label: str,
    steps: Sequence[Any],
    source_traces: Sequence[str] = (),
    traces: Sequence[Trace] = (),
    final_artifact: str | None = None,
    params: Mapping[str, Any] | None = None,
    policy: str = POLICY_SCRIPTS_FIRST,
    trace_evidence: Sequence[tuple[str, str]] = (),
    input_contract: Mapping[str, Any] | None = None,
    workspace: Any = None,
) -> DistillDraft:
    """Build the structural review without reading scripts or compiling a graph."""
    try:
        normalized_policy = normalize_policy(policy)
    except ValueError as exc:
        raise DistillError(str(exc)) from exc
    planned = _bind_submit_receipts(plan_steps(steps), params)
    planned, params = _portable_mount_paths(planned, params)
    source_ids = tuple(
        dict.fromkeys(source_traces or tuple(trace.trace_id for trace in traces))
    )
    branches, connected = _scope_split(planned, traces, params)
    handover = [
        item for item in _handover_steps(planned, traces, params)
        if item not in set(connected)
    ]
    scope_warnings = tuple(
        f"预检可能要求复核：{_step_line(trace_id, step)}"
        for trace_id, step in (*connected, *handover)
    )
    unresolved: list[str] = []
    if not traces:
        unresolved.append(
            "没有提供来源轨迹：确认后仍会做脚本、依赖和图编译预检，"
            "但无法核对会话里是否漏掉上游步骤。"
        )
    else:
        exported = {trace.trace_id for trace in traces}
        missing = [trace_id for trace_id in source_ids if trace_id not in exported]
        if missing:
            unresolved.append(
                "source_traces 里这些会话没有对应的导出轨迹："
                + "、".join(f"`{item}`" for item in missing)
            )
        truncated = [
            f"`{trace.trace_id}`（dropped_steps={trace.export_dropped_steps}）"
            for trace in traces
            if trace.export_dropped_steps
        ]
        if truncated:
            unresolved.append(
                "来源轨迹导出不完整：" + "、".join(truncated)
                + "。确认后的预检会拒绝按不完整轨迹固化整条链路。"
            )
    if final_artifact is not None and not _is_artifact_path(final_artifact):
        recovered = recover_artifact_path(
            final_artifact,
            [path for step in planned for path in step.writes],
        )
        if recovered is not None:
            unresolved.append(
                "已从 final_artifact 描述中的唯一可产出路径"
                f"恢复为 `{recovered}`"
            )
            final_artifact = recovered
    if final_artifact is not None:
        issue = _final_artifact_issue(final_artifact, planned, params)
        if issue is not None:
            unresolved.append(issue)
    produced_params = {
        key
        for step in planned
        for path in step.writes
        if (key := placeholder_key(path)) is not None
    }
    missing_params = sorted(
        {
            key
            for step in planned
            for path in step.reads
            if (
                (key := placeholder_key(path)) is not None
                and key not in (params or {})
                and key not in produced_params
            )
        }
    )
    if missing_params:
        unresolved.append(
            "这些占位符没有在 params 里给出真实值："
            + "、".join(f"`{{{key}}}`" for key in missing_params)
        )
    graph_steps = _expected_graph_steps(planned, final_artifact, params)
    return DistillDraft(
        label=label,
        steps=planned,
        source_traces=source_ids,
        params=params,
        final_artifact=final_artifact,
        policy=normalized_policy,
        graph_steps=graph_steps,
        branches=tuple(
            _step_line(trace_id, step) for trace_id, step in branches
        ),
        scope_warnings=scope_warnings,
        unresolved=tuple(unresolved),
        trace_evidence=tuple(trace_evidence),
        input_contract=input_contract,
        workspace=workspace,
        traces=tuple(traces),
    )


def preflight_matches_draft(
    draft: DistillDraft,
    plan: DistillPlan,
) -> tuple[bool, str | None]:
    """Whether full preflight preserved exactly what the user reviewed."""
    preflight = {
        "steps": [
            _step_semantics(step, draft.params, draft.input_contract)
            for step in plan.steps
        ],
        "source_traces": list(plan.chain_traces),
        "final_artifact": plan.package.final_artifact,
        "policy": plan.policy,
        "graph_steps": [node.order + 1 for node in plan.package.nodes],
    }
    expected = draft.semantic
    if preflight["steps"] != expected["steps"]:
        return False, "步骤类型、来源、输入输出或 keep 标记发生变化"
    if preflight["source_traces"] != expected["source_traces"]:
        return False, "来源轨迹发生变化"
    expected_final = expected["final_artifact"]
    actual_final = preflight["final_artifact"]
    given = bindings(draft.params)
    if (
        canonical_path(expected_final, given) if expected_final else None
    ) != (
        canonical_path(actual_final, given) if actual_final else None
    ):
        return False, "最终产物发生变化"
    if preflight["policy"] != expected["policy"]:
        return False, "固化策略发生变化"
    if preflight["graph_steps"] != expected["graph_steps"]:
        return False, "预检后的入图步骤发生变化"
    return True, None


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
    bound: tuple[tuple[str, Any], ...] = ()
    materialized: tuple[tuple[str, str], ...] = ()
    materialize_notes: tuple[str, ...] = ()
    policy: str = POLICY_SCRIPTS_FIRST
    scope_review: str | None = None
    branches: tuple[str, ...] = ()
    kept: tuple[int, ...] = ()

    @property
    def report_path(self) -> Path:
        return self.directory / "report.md"

    def payload(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace.trace_id,
            "trace": self.document,
            "report": str(self.report_path),
            "package": str(self.package.directory),
            "pin": self.package.pin,
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
            "kept": [{"step": order + 1, "reason": KEEP_REASON}
                     for order in self.kept],
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
    if plan.kept:
        lines.extend(["", "- 强制保留（声明了 keep，产出不参与依赖闭包）："
                      + "、".join(f"#{order}" for order in plan.kept)])
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
            review_reason: str | None = None,
            dry_run: bool = False) -> DistillPlan:
    """Turn a distilled chain into a compiled package the capture stages accept."""
    try:
        policy = normalize_policy(policy)
    except ValueError as exc:
        raise DistillError(str(exc)) from exc
    truncated = [f"{trace.trace_id} ({trace.export_dropped_steps})" for trace in traces
                 if trace.export_dropped_steps]
    if truncated:
        raise DistillError(
            "来源轨迹导出不完整（dropped_steps）：" + ", ".join(truncated)
            + "；请重新导出完整会话后再沉淀整条链路。"
              "review_confirmed 无法验证导出里不存在的步骤")
    planned = _bind_submit_receipts(plan_steps(steps), params)
    planned, params = _portable_mount_paths(planned, params)
    # Every check that reads the plan runs before anything is frozen, and they all
    # report together: a chain that is wrong in three ways should not take three
    # submissions to fix.
    problems: list[str] = []
    if (declarations or {}).get("output_contract") and not final_artifact:
        problems.append("output_contract 声明了产出，distill 必须显式指定 final_artifact，"
                        "不能从步骤顺序推断最终产物")
    problems.extend(_template_path_issues(planned, params, final_artifact))
    problems.extend(_async_wait_issues(planned))
    problems.extend(_slice_boundary_issues(traces, params))
    try:
        _check_llm_promotions(planned, traces, policy)
    except DistillError as exc:
        problems.append(str(exc))
    final_token: str | None = None
    if final_artifact is not None:
        try:
            final_token = _check_final_artifact(final_artifact, planned, params)
        except DistillError as exc:
            problems.append(str(exc))
    # The chain check reads the plan as the agent wrote it: a `{script}` placeholder is
    # an explicit boundary there, while the frozen path it is rewritten to would look
    # like a dropped upstream step.
    boundaries: tuple[tuple[str | None, str], ...] = ()
    try:
        boundaries = _check_chain(traces, planned, params)
    except DistillError as exc:
        problems.append(str(exc))
    branches: list[Any] = []
    scope_review: str | None = None
    try:
        branches, scope_review = _review_scope(
            planned, traces, params, review_confirmed, review_reason)
    except DistillError as exc:
        problems.append(str(exc))
    try:
        _check_replay_literals(planned, workspace)
    except DistillError as exc:
        problems.append(str(exc))
    # Only a declared final artifact prunes the slice, so only then can a listed step
    # be dropped for having an unconsumed product.
    # An unconsumed product used to block here. It is advice — the message itself
    # says "confirm in this order, do not rush to keep" — and blocking on advice
    # left plans that would run perfectly well unable to reach a replay.
    problems.extend(_intermediate_token_issues(planned, params))
    if problems:
        raise _aggregate(problems)
    rewritten, remaining, materialized, materialize_notes = _materialize_scripts(
        planned, params, workspace)
    frozen = {key for key, _ in materialized}
    boundaries = tuple(text for key, text in boundaries if key not in frozen)
    if final_token is not None and (key := placeholder_key(final_token)) is not None:
        final_token = dict(materialized).get(key, final_token)
    document = trace_document(label, rewritten, source_traces=source_traces)
    trace = capture_trace(json.dumps(document))
    alignment = align_traces([trace], threshold)
    directory = (
        Path(tempfile.mkdtemp(prefix="agentgenome-dry-"))
        if dry_run
        else service.root / "distills" / trace.trace_id
    )
    try:
        try:
            slice_ = reverse_slice(alignment, final_token, bindings(params))
            package = build_package(directory / "package", alignment=alignment,
                                    slice_=slice_, traces=[trace], label=label,
                                    workspace=workspace, owner=owner,
                                    version=version or DEFAULT_VERSION,
                                    description=description, asset_id=asset_id,
                                    declarations=declarations,
                                    bindings=bindings(params))
        except (SliceError, EmitError, CompileError, ParameterError) as exc:
            raise DistillError(f"{exc}（草稿：{directory / 'package'}）") from exc
        bound = _bound_values(package, rewritten, remaining if params is not None else None)
        plan = DistillPlan(trace, document, rewritten, directory, package, slice_.auxiliary,
                           tuple(trace.trace_id for trace in traces), boundaries, bound,
                           materialized, materialize_notes, policy,
                           scope_review,
                           tuple(_step_line(trace_id, step) for trace_id, step in branches),
                           slice_.kept)
        plan.report_path.parent.mkdir(parents=True, exist_ok=True)
        plan.report_path.write_text(_report(label, plan, alignment, slice_), encoding="utf-8")
        return plan
    finally:
        if dry_run:
            shutil.rmtree(directory, ignore_errors=True)
