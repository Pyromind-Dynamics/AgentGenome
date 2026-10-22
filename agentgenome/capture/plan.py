"""Build a deterministic candidate chain from an exported Pi trace.

The model should not have to translate an entire session into the distill schema.
This module derives the candidate from artifact producers and consumers, prunes
unreferenced work, and leaves only genuine ambiguities for later semantic review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from .. import paths
from .trace import (
    Command,
    INLINE_MARK,
    KIND_HUMAN,
    KIND_LLM,
    KIND_SHELL,
    KIND_TOOL,
    KIND_WRITE,
    ROLE_SCRIPT,
    Step,
    Trace,
    parse_command,
    placeholder_key,
    probe_command,
    role,
)

_PATH_TOKEN = re.compile(
    r"(?<![\w./-])(?:~?/|\./|\.\./)?"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_-]+)?"
)
_PATH_NOISE = "\"'`()[]{}<>.,;:，。；：、"
_CARDINALITY = re.compile(r"(?:^|_)(\d{1,3})(?=_|$)")
_HUMAN_HANDOFF = re.compile(
    r"(复核|导出|标注|打完|弄好|改好|人工|手工|"
    r"label\s*studio|reviewed|exported|handoff)",
    re.IGNORECASE,
)
_MUTATING_TOOL = re.compile(
    r"(?:^|[_-])(?:create|import|upload|submit|launch|enqueue|publish|send|"
    r"delete|remove|update|write|save|start|stop|cancel|create_project|project)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)
_READONLY_TOOL = re.compile(
    r"(?:^|[_-])(?:get|list|read|search|find|query|describe|inspect|status)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)
_EPHEMERAL_STEM = re.compile(
    r"(?:^|[._-])(?:tmp|temp|test|probe|smoke|wtest)(?:$|[._-])",
    re.IGNORECASE,
)
_OUTPUT_KEYS = {
    "artifact",
    "artifact_path",
    "destination",
    "dest",
    "output",
    "output_file",
    "output_path",
    "result",
    "result_file",
    "result_path",
}
_RECEIPT_ROOT = "public_data/agentgenome-artifacts"
_MANAGEMENT_TOOL = re.compile(r"^(?:genome|agentgenome)[_-]", re.IGNORECASE)


@dataclass(frozen=True)
class CandidatePlan:
    """A mechanically extracted plan that still requires user confirmation."""

    steps: tuple[dict[str, Any], ...]
    final_artifact: str | None
    params: Mapping[str, str]
    input_contract: Mapping[str, Any]
    branches: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    generated_receipts: tuple[str, ...] = ()

    def arguments(self, original: Mapping[str, Any]) -> dict[str, Any]:
        """Return the distill arguments with the candidate filled in."""
        resolved = dict(original)
        resolved["steps"] = [dict(step) for step in self.steps]
        if self.final_artifact:
            resolved["final_artifact"] = self.final_artifact
        if self.params:
            supplied = resolved.get("params")
            resolved["params"] = {
                **dict(supplied or {}),
                **dict(self.params),
            }
        if self.input_contract:
            supplied = resolved.get("input_contract")
            resolved["input_contract"] = {
                **dict(supplied or {}),
                **dict(self.input_contract),
            }
        return resolved


@dataclass(frozen=True)
class _Action:
    index: int
    source: str
    seq: int
    kind: str
    payload: dict[str, Any]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    keep: bool = False
    generated_output: bool = False
    ambiguity: str | None = None


def _source_id(trace: Trace, step: Step) -> str:
    return f"{trace.trace_id}:{step.seq}"


def _key_base(path: str) -> str:
    """A root input is named by its role, not by the size of the trial run.

    `prelabel_manifest_10.jsonl` came from a ten-record `head`, but the chain it
    seeds has to accept whatever the next input holds, so the count stays out of
    the parameter name.
    """
    stem = _CARDINALITY.sub("_", PurePosixPath(path).stem.lower())
    text = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    if not text:
        return "value"
    return text if text[0].isalpha() else f"p_{text}"


def _path_like(value: str) -> bool:
    if not value or value.startswith(("http://", "https://")):
        return False
    if value.startswith(("/", "./", "../", "~/")):
        return True
    return "/" in value


def _paths_in_text(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(0).strip(_PATH_NOISE)
        if _path_like(token) and token not in found:
            found.append(token)
    return tuple(found)


def _paths_in_value(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if _path_like(value) else ()
    if isinstance(value, Mapping):
        return tuple(
            path
            for item in value.values()
            for path in _paths_in_value(item)
        )
    if isinstance(value, list):
        return tuple(
            path
            for item in value
            for path in _paths_in_value(item)
        )
    return ()


def recover_artifact_path(value: str, outputs: Sequence[str]) -> str | None:
    """Return the sole declared output mentioned by a prose artifact description."""
    mentioned = {
        output
        for path in _paths_in_text(value)
        for output in outputs
        if _same_path(path, output)
    }
    return mentioned.pop() if len(mentioned) == 1 else None


def _output_arg(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _OUTPUT_KEYS:
                if isinstance(item, str) and _path_like(item):
                    return item
            nested = _output_arg(item)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _output_arg(item)
            if nested:
                return nested
    return None


def _receipt_path(trace: Trace, step: Step) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", trace.trace_id).strip("_") or "trace"
    tool = re.sub(r"[^A-Za-z0-9_.-]+", "_", step.tool_name).strip("_") or "tool"
    return f"{_RECEIPT_ROOT}/{safe}/{step.seq}-{tool}.json"


def _ephemeral_write(command: Command, text: str) -> bool:
    """Reject smoke-test files that a command writes and removes in the same call."""
    has_remove = re.search(r"(?:^|[\s;|&])rm(?:\s|$)", text) is not None
    for path in command.write_paths:
        normalized = path.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if normalized.startswith(("/tmp/", "tmp/", ".tmp/")):
            return True
        if _EPHEMERAL_STEM.search(PurePosixPath(normalized).stem):
            return True
        if has_remove and normalized in text:
            return True
        if parts and parts[0] in {"tmp", ".tmp"}:
            return True
    return False


def _side_effect_tool(name: str) -> bool:
    """Conservative side-effect hint for tools whose result is not a data product."""
    return (
        not name.startswith("genome_")
        and _MUTATING_TOOL.search(name) is not None
        and _READONLY_TOOL.search(name) is None
    )


def _data_paths(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if role(path, writing=False) != ROLE_SCRIPT)


def _replace_tokens(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        text = value
        for source in sorted(replacements, key=len, reverse=True):
            text = re.sub(
                r"(?<![\w./-])" + re.escape(source) + r"(?![\w./-])",
                replacements[source],
                text,
            )
        return text
    if isinstance(value, Mapping):
        return {
            key: _replace_tokens(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    return value


def _same_path(left: str, right: str) -> bool:
    """Whether two spellings name one file.

    Suffix matching used to stand in for this, which pairs `out/x.jsonl` with
    `datasets/a/out/x.jsonl` and drops a producer edge that exists. Compare the
    canonical (namespace, path) instead.
    """
    return paths.same(left, right)


def _producer(
    path: str,
    actions: Sequence[_Action],
    before: int,
) -> tuple[int, _Action] | None:
    found: tuple[int, _Action] | None = None
    for index, action in enumerate(actions[:before]):
        if any(_same_path(path, output) for output in action.writes):
            found = index, action
    return found


def _human_candidates(
    traces: Sequence[Trace],
    actions: Sequence[_Action],
) -> dict[str, _Action]:
    candidates: dict[str, _Action] = {}
    for trace in traces:
        for step in trace.steps:
            if step.kind != KIND_HUMAN or not step.command:
                continue
            if not _HUMAN_HANDOFF.search(step.command):
                continue
            for path in _paths_in_text(step.command):
                if role(path, writing=True) == ROLE_SCRIPT:
                    continue
                prior = [
                    action for action in actions
                    if action.source == trace.trace_id and action.seq < step.seq
                ]
                inputs = list(_data_paths(
                    [path for action in prior[-2:] for path in action.writes]
                ))
                instruction = step.command
                for item in (*inputs, path):
                    if item not in instruction:
                        instruction += f"\n路径：`{item}`"
                index = len(actions) + 1 + len(candidates)
                candidates[path] = _Action(
                    index=index,
                    source=trace.trace_id,
                    seq=step.seq,
                    kind="human",
                    payload={
                        "human": instruction,
                        "input": inputs,
                        "output": path,
                        "from": _source_id(trace, step),
                    },
                    reads=tuple(inputs),
                    writes=(path,),
                    ambiguity=(
                        f"人工节点 `{trace.trace_id}:{step.seq}` 的产出 "
                        f"`{path}` 是从用户交回内容中识别的"
                    ),
                )
    return candidates


def _all_actions(traces: Sequence[Trace]) -> tuple[list[_Action], list[str], list[str]]:
    actions: list[_Action] = []
    branches: list[str] = []
    ambiguities: list[str] = []
    index = 0
    for trace in traces:
        for step in trace.steps:
            if not step.success or step.rejected:
                continue
            source = _source_id(trace, step)
            if step.kind == KIND_SHELL:
                command = parse_command(step.command)
                if INLINE_MARK in step.command_shape:
                    branches.append(f"`{source}` 是内联代码，已排除；业务脚本应使用落盘文件")
                    continue
                if probe_command(command):
                    continue
                if _ephemeral_write(command, step.command):
                    branches.append(
                        f"`{source}` 写入的是临时/试跑产物并在同一命令中清理，已排除"
                    )
                    continue
                if not step.read_paths and not step.write_paths:
                    branches.append(f"`{source}` 没有可识别的输入或产物，已排除")
                    continue
                writes = tuple(dict.fromkeys((*step.write_paths, *step.reported_writes)))
                reads = tuple(
                    path
                    for path in step.read_paths
                    if path not in step.reported_writes
                )
                hidden = tuple(
                    path
                    for path in step.reported_writes
                    if path not in step.write_paths
                )
                ambiguity = None
                if hidden:
                    ambiguity = (
                        f"`{source}` 的成功结果声明了产物 "
                        + "、".join(f"`{path}`" for path in hidden)
                        + "，但命令没有显式接收输出路径；确认前必须把脚本改成"
                        "命令行传入输出路径，再重新提取"
                    )
                    ambiguities.append(ambiguity)
                actions.append(_Action(
                    index=index,
                    source=trace.trace_id,
                    seq=step.seq,
                    kind="run",
                    payload={"run": step.command, "from": source},
                    reads=reads,
                    writes=writes,
                    ambiguity=ambiguity,
                ))
                index += 1
                continue
            if step.kind == KIND_TOOL:
                if _MANAGEMENT_TOOL.search(step.tool_name):
                    branches.append(
                        f"`{source}` 是 AgentGenome 管理调用，不属于待沉淀业务链路"
                    )
                    continue
                args = dict(step.arguments or {})
                output = _output_arg(args)
                generated = output is None
                output = output or _receipt_path(trace, step)
                reads = tuple(dict.fromkeys(
                    path
                    for path in (*step.read_paths, *_paths_in_value(args))
                    if path != output
                ))
                side_effect = step.keep or _side_effect_tool(step.tool_name)
                payload: dict[str, Any] = {
                    "tool": step.tool_name,
                    "args": args,
                    "output": output,
                    "keep": True,
                    "from": source,
                }
                if reads:
                    payload["input"] = list(reads)
                ambiguity = None
                if generated:
                    reason = (
                        "该节点的名称表明它会修改外部状态"
                        if side_effect and not step.keep
                        else "该节点按有副作用步骤保留"
                    )
                    ambiguity = (
                        f"工具节点 `{source}` 未声明结果路径，候选计划使用回执路径 "
                        f"`{output}`；{reason}"
                    )
                elif side_effect and not step.keep:
                    ambiguity = (
                        f"工具节点 `{source}` 的名称表明它可能修改外部状态；"
                        "候选计划按有副作用步骤保留"
                    )
                actions.append(_Action(
                    index=index,
                    source=trace.trace_id,
                    seq=step.seq,
                    kind="tool",
                    payload=payload,
                    reads=reads,
                    writes=(output,),
                    keep=side_effect,
                    generated_output=generated,
                    ambiguity=ambiguity,
                ))
                index += 1
                continue
            if step.kind == KIND_LLM:
                ambiguities.append(
                    f"`{source}` 是 agent/LLM 步骤，当前没有可靠产物声明；"
                    "候选计划暂不把它伪装成固定脚本"
                )
                continue
            if step.kind == KIND_WRITE and step.write_paths:
                for path in step.write_paths:
                    if role(path, writing=True) == ROLE_SCRIPT:
                        continue
                    ambiguities.append(
                        f"`{source}` 用编辑工具写出了数据 `{path}`；该产物只能作为"
                        "输入边界，不能机械回放"
                    )
    return actions, branches, ambiguities


def _infer_final(
    actions: Sequence[_Action],
    humans: Mapping[str, _Action],
    explicit: str | None,
) -> tuple[str | None, str | None]:
    outputs = [
        (action.index, path)
        for action in actions
        for path in action.writes
        if not action.generated_output
        and role(path, writing=True) != ROLE_SCRIPT
    ]
    if not outputs:
        inferred = None
        inferred_note = "没有从 trace 识别出可执行的最终产物"
    else:
        inferred = max(outputs, key=lambda item: item[0])[1]
        inferred_note = (
            f"最终产物 `{inferred}` 未显式指定，候选计划按最后一个数据产物推断"
        )
    if explicit is None:
        return inferred, inferred_note
    key = placeholder_key(explicit)
    explicit_is_path = bool(key) or (
        explicit == explicit.strip()
        and not any(char.isspace() for char in explicit)
        and not any(char in '，。；：、（）[]<>"\'`' for char in explicit)
        and _path_like(explicit)
    )
    if not explicit_is_path:
        outputs = [
            output
            for action in actions
            for output in action.writes
        ] + list(humans)
        if recovered := recover_artifact_path(explicit, outputs):
            return recovered, (
                f"final_artifact 是描述文本，描述中唯一由 trace 产出的路径是 "
                f"`{recovered}`，已按它推断"
            )
        return inferred, (
            f"final_artifact `{explicit}` 不是单个产物路径，"
            f"已按链路最后一个数据产物推断为 `{inferred}`"
        )
    if _producer(explicit, actions, len(actions) + 1) is not None:
        return explicit, None
    if explicit in humans:
        return explicit, None
    return inferred, (
        f"final_artifact `{explicit}` 在 trace 中没有对应产出，"
        f"候选计划改用最后一个数据产物 `{inferred}`"
    )


def _closure(
    actions: Sequence[_Action],
    humans: Mapping[str, _Action],
    final_artifact: str | None,
) -> set[int]:
    selected: set[int] = set()

    def visit(index: int) -> None:
        if index in selected:
            return
        selected.add(index)
        action = actions[index]
        for path in action.reads:
            producer = _producer(path, actions, index)
            if producer is not None:
                visit(producer[0])
                continue
            for human in humans.values():
                if human.index not in selected and _same_path(path, human.writes[0]):
                    selected.add(human.index)
                    visit_human(human)
                    break

    def visit_human(human: _Action) -> None:
        for path in human.reads:
            producer = _producer(path, actions, len(actions))
            if producer is not None:
                visit(producer[0])

    if final_artifact is None:
        return selected
    root = _producer(final_artifact, actions, len(actions) + 1)
    if root is None:
        human = humans.get(final_artifact)
        if human is not None:
            selected.add(human.index)
            visit_human(human)
        return selected
    visit(root[0])
    return selected


def _include_side_effect_tools(
    actions: Sequence[_Action],
    selected: set[int],
    final_artifact: str | None,
) -> set[int]:
    mechanical = {index for index in selected if index < len(actions)}
    if not mechanical:
        return selected
    selected_outputs = {
        path for index in mechanical for path in actions[index].writes
    }
    selected_reads = {
        path for index in mechanical for path in actions[index].reads
    }
    for index, action in enumerate(actions):
        if index in selected or action.kind != "tool":
            continue
        if not action.keep and _READONLY_TOOL.search(
            str(action.payload.get("tool") or "")
        ):
            continue
        overlaps = any(
            _same_path(path, known)
            for path in action.reads
            for known in selected_outputs | selected_reads
        )
        if overlaps or action.keep:
            selected.add(index)
    if final_artifact:
        root = _producer(final_artifact, actions, len(actions) + 1)
        if root is not None:
            selected.add(root[0])
    return selected


_CD_TARGET = re.compile(r"(?:^|[;&|]\s*)cd\s+([^\s;&|]+)")


def _cd_targets(action: _Action) -> set[str]:
    """Directories a command only steps into.

    `cd <workspace>; python3 script.py in out` makes the parser report the
    workspace itself as a read, which then becomes a root input parameter — and a
    root input that is a directory nobody can supply. It is the execution context,
    not data, so it never enters the contract.
    """
    run = action.payload.get("run") if isinstance(action.payload, dict) else None
    if not isinstance(run, str):
        return set()
    return {
        match.group(1).strip("\"'") for match in _CD_TARGET.finditer(run)
    }


def _root_paths(
    selected: Sequence[_Action],
) -> tuple[str, ...]:
    produced = {
        path for action in selected for path in action.writes
    }
    roots: list[str] = []
    for action in selected:
        stepped_into = _cd_targets(action)
        for path in action.reads:
            if role(path, writing=False) == ROLE_SCRIPT:
                continue
            if any(_same_path(path, target) for target in stepped_into):
                continue
            if any(_same_path(path, output) for output in produced):
                continue
            if path not in roots:
                roots.append(path)
    return tuple(roots)


def _param_names(
    roots: Sequence[str],
    supplied: Mapping[str, Any],
) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for key, value in supplied.items():
        if isinstance(value, str):
            for path in roots:
                if _same_path(path, value):
                    names[path] = str(key)
                    used.add(str(key))
    for path in roots:
        if path in names:
            continue
        base = _key_base(path)
        candidate = base
        index = 1
        while candidate in used:
            index += 1
            candidate = f"{base}_{index}"
        used.add(candidate)
        names[path] = candidate
    return names


def build_candidate_plan(
    traces: Sequence[Trace],
    *,
    final_artifact: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> CandidatePlan:
    """Extract the smallest artifact-connected chain from one or more traces."""
    if not traces:
        raise ValueError("candidate extraction needs at least one trace")

    actions, branches, ambiguities = _all_actions(traces)
    humans = _human_candidates(traces, actions)
    final, final_note = _infer_final(actions, humans, final_artifact)
    if final_note:
        ambiguities.append(final_note)

    selected = _closure(actions, humans, final)
    selected = _include_side_effect_tools(actions, selected, final)
    ordered = [
        action for index, action in enumerate(actions) if index in selected
    ]
    ordered.extend(
        human for human in humans.values() if human.index in selected
    )
    trace_rank = {
        trace.trace_id: index for index, trace in enumerate(traces)
    }
    ordered.sort(key=lambda action: (trace_rank.get(action.source, 0), action.seq))

    for action in ordered:
        if action.ambiguity:
            ambiguities.append(action.ambiguity)

    roots = _root_paths(ordered)
    names = _param_names(roots, params or {})
    replacements = {
        path: "{" + name + "}"
        for path, name in names.items()
    }

    steps: list[dict[str, Any]] = []
    for action in ordered:
        step = _replace_tokens(action.payload, replacements)
        if not isinstance(step, dict):
            raise ValueError("candidate step must be an object")
        steps.append(step)

    selected_keys = {
        (action.source, action.seq) for action in ordered
    }
    for action in actions:
        if (action.source, action.seq) in selected_keys:
            continue
        if action.kind == "tool":
            continue
        label = action.payload.get("run") or action.payload.get("tool") or action.kind
        branches.append(
            f"`{action.source}:{action.seq}` `{str(label)[:160]}` "
            "不在最终产物的依赖闭包里，已作为旁支排除"
        )

    input_contract = {name: {"type": "path"} for name in names.values()}
    generated = tuple(
        action.writes[0] for action in ordered
        if action.generated_output
    )
    return CandidatePlan(
        steps=tuple(steps),
        final_artifact=final,
        params={names[path]: path for path in roots},
        input_contract=input_contract,
        branches=tuple(dict.fromkeys(branches)),
        ambiguities=tuple(dict.fromkeys(ambiguities)),
        generated_receipts=generated,
    )
