"""Stage B: align several traces into a skeleton, branches, garbage and parameters.

Alignment is progressive: the first trace seeds the columns, every later trace is
aligned against the current columns with Needleman-Wunsch. The result therefore
depends on trace order, which is deterministic and stated in the report.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from .trace import KIND_HUMAN, Step, Trace

DEFAULT_THRESHOLD = 0.6
ROLE_SKELETON = "skeleton"
ROLE_BRANCH = "branch"
ROLE_GARBAGE = "garbage"

Signature = tuple[str, str, tuple[str, ...], tuple[str, ...]]

_MATCH = 2
_MISMATCH = -5
_GAP = -1


@dataclass(frozen=True)
class Slot:
    signature: Signature
    role: str
    support: int
    attempts: int
    variants: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]
    evidence: tuple[tuple[str, int], ...]
    inline_code: bool
    keep: bool = False

    @property
    def kind(self) -> str:
        return self.signature[0]

    @property
    def command_shape(self) -> str:
        return self.signature[1]

    @property
    def reads(self) -> tuple[str, ...]:
        return self.signature[2]

    @property
    def writes(self) -> tuple[str, ...]:
        return self.signature[3]

    @property
    def trace_ids(self) -> tuple[str, ...]:
        return tuple(trace_id for trace_id, _, _ in self.variants)

    @property
    def read_paths(self) -> tuple[str, ...]:
        return tuple(sorted({path for _, reads, _ in self.variants for path in reads}))

    @property
    def write_paths(self) -> tuple[str, ...]:
        return tuple(sorted({path for _, _, writes in self.variants for path in writes}))

    @property
    def varies(self) -> bool:
        return len({(reads, writes) for _, reads, writes in self.variants}) > 1


@dataclass(frozen=True)
class Alignment:
    traces: tuple[str, ...]
    skipped: tuple[str, ...]
    threshold: float
    required_support: int
    slots: tuple[Slot, ...]

    @property
    def single_trace(self) -> bool:
        return self.support_total < 2

    @property
    def support_total(self) -> int:
        return len(self.traces) - len(self.skipped)

    @property
    def uniform_support(self) -> bool:
        skeleton = [slot for slot in self.slots if slot.role == ROLE_SKELETON]
        return bool(skeleton) and all(slot.support == self.support_total for slot in skeleton)

    def roles(self, role: str) -> tuple[int, ...]:
        return tuple(index for index, slot in enumerate(self.slots) if slot.role == role)


def required_support(trace_count: int, threshold: float = DEFAULT_THRESHOLD) -> int:
    if trace_count <= 2:
        return max(trace_count, 1)
    return max(math.ceil(threshold * trace_count), 1)


def alignable(trace: Trace) -> tuple[Step, ...]:
    """The steps a skeleton can be built from.

    A session's `user` message carries no work and no artifact, so it is not a column;
    a distilled human gate names the question and the artifact the person produces, so
    it is a step like any other.
    """
    return tuple(step for step in trace.steps
                 if step.kind != KIND_HUMAN or step.write_paths)


def align(left: Sequence[Signature], right: Sequence[Signature]) -> list[tuple[int | None, int | None]]:
    rows, columns = len(left) + 1, len(right) + 1
    score = [[0] * columns for _ in range(rows)]
    for row in range(1, rows):
        score[row][0] = score[row - 1][0] + _GAP
    for column in range(1, columns):
        score[0][column] = score[0][column - 1] + _GAP
    for row in range(1, rows):
        for column in range(1, columns):
            step = _MATCH if left[row - 1] == right[column - 1] else _MISMATCH
            score[row][column] = max(
                score[row - 1][column - 1] + step,
                score[row - 1][column] + _GAP,
                score[row][column - 1] + _GAP,
            )
    pairs: list[tuple[int | None, int | None]] = []
    row, column = len(left), len(right)
    while row > 0 or column > 0:
        if row > 0 and column > 0:
            step = _MATCH if left[row - 1] == right[column - 1] else _MISMATCH
            if score[row][column] == score[row - 1][column - 1] + step:
                pairs.append((row - 1, column - 1))
                row, column = row - 1, column - 1
                continue
        if row > 0 and score[row][column] == score[row - 1][column] + _GAP:
            pairs.append((row - 1, None))
            row -= 1
            continue
        pairs.append((None, column - 1))
        column -= 1
    pairs.reverse()
    return pairs


def _slot(step: Step, trace_id: str, role: str) -> Slot:
    successful = step.success and not step.rejected
    return Slot(
        signature=step.signature,
        role=role,
        support=1 if successful else 0,
        attempts=1,
        variants=((trace_id, step.read_paths, step.write_paths),) if successful else (),
        evidence=((trace_id, step.seq),) if successful else (),
        inline_code=step.inline_code,
        keep=step.keep,
    )


def _extend(slot: Slot, step: Step, trace_id: str) -> Slot:
    successful = step.success and not step.rejected
    if not successful:
        return replace(slot, attempts=slot.attempts + 1)
    return replace(
        slot,
        support=slot.support + 1,
        attempts=slot.attempts + 1,
        variants=slot.variants + ((trace_id, step.read_paths, step.write_paths),),
        evidence=slot.evidence + ((trace_id, step.seq),),
        keep=slot.keep or step.keep,
    )


def _merge(slots: list[Slot], steps: tuple[Step, ...], trace_id: str) -> list[Slot]:
    pairs = align([slot.signature for slot in slots], [step.signature for step in steps])
    merged: list[Slot] = []
    pending: list[Step] = []
    for index, position in pairs:
        if index is None:
            pending.append(steps[position])
            continue
        merged.extend(_slot(step, trace_id, ROLE_BRANCH) for step in pending)
        pending = []
        slot = slots[index]
        if position is not None and slot.signature == steps[position].signature:
            merged.append(_extend(slot, steps[position], trace_id))
        else:
            merged.append(slot)
            if position is not None:
                merged.append(_slot(steps[position], trace_id, ROLE_BRANCH))
    merged.extend(_slot(step, trace_id, ROLE_BRANCH) for step in pending)
    return merged


def classify(slots: Sequence[Slot], required: int) -> tuple[Slot, ...]:
    roles = []
    for slot in slots:
        if slot.support == 0:
            roles.append(ROLE_GARBAGE)
        elif slot.support >= required:
            roles.append(ROLE_SKELETON)
        else:
            roles.append(ROLE_BRANCH)
    return tuple(replace(slot, role=role) for slot, role in zip(slots, roles))


def align_traces(traces: Sequence[Trace], threshold: float = DEFAULT_THRESHOLD) -> Alignment:
    if not traces:
        raise ValueError("capture needs at least one trace")
    # The first trace seeds the columns and every later one is aligned against the
    # current slots, so the order traces arrive in changes the skeleton. Sorting by
    # id makes the result a property of the trace set, not of the caller's list.
    ordered = sorted(traces, key=lambda trace: trace.trace_id)
    sequences = [(trace.trace_id, alignable(trace)) for trace in ordered]
    contributing = [(trace_id, steps) for trace_id, steps in sequences if steps]
    if not contributing:
        raise ValueError("no tool steps to align")
    slots: list[Slot] = []
    for trace_id, steps in contributing:
        slots = _merge(slots, steps, trace_id) if slots else [
            _slot(step, trace_id, ROLE_BRANCH) for step in steps]
    required = required_support(len(contributing), threshold)
    return Alignment(
        traces=tuple(trace.trace_id for trace in ordered),
        skipped=tuple(trace_id for trace_id, steps in sequences if not steps),
        threshold=threshold,
        required_support=required,
        slots=classify(slots, required),
    )


def alignment_payload(alignment: Alignment) -> dict[str, Any]:
    slots = []
    for order, slot in enumerate(alignment.slots):
        slots.append({
            "order": order,
            "role": slot.role,
            "kind": slot.kind,
            "command_shape": slot.command_shape,
            "reads": list(slot.reads),
            "writes": list(slot.writes),
            "support": slot.support,
            "attempts": slot.attempts,
            "traces": list(slot.trace_ids),
            "evidence": [list(item) for item in slot.evidence],
            "read_paths": list(slot.read_paths),
            "write_paths": list(slot.write_paths),
            "inline_code": slot.inline_code,
            "varies": slot.varies,
            "keep": slot.keep,
        })
    return {
        "traces": list(alignment.traces),
        "skipped_traces": list(alignment.skipped),
        "trace_count": len(alignment.traces),
        "support_total": alignment.support_total,
        "threshold": alignment.threshold,
        "required_support": alignment.required_support,
        "single_trace": alignment.single_trace,
        "uniform_support": alignment.uniform_support,
        "skeleton": list(alignment.roles(ROLE_SKELETON)),
        "branches": list(alignment.roles(ROLE_BRANCH)),
        "garbage": list(alignment.roles(ROLE_GARBAGE)),
        "slots": slots,
    }


def write_alignment(directory: Path, alignment: Alignment) -> Path:
    target = directory / "alignment.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(alignment_payload(alignment), indent=2, ensure_ascii=False) + "\n")
    return target


def render_alignment(alignment: Alignment) -> str:
    payload = alignment_payload(alignment)
    lines = [
        "### 轨迹对齐", "",
        f"- 轨迹：{payload['trace_count']} 条（{', '.join(alignment.traces)}）",
        f"- 参与对齐：{alignment.support_total} 条",
        f"- 阈值：{alignment.threshold}，骨架要求 support >= {alignment.required_support}",
        f"- 骨架 {len(payload['skeleton'])} 步 / 分支候选 {len(payload['branches'])} 步 / "
        f"垃圾 {len(payload['garbage'])} 步",
    ]
    if alignment.skipped:
        lines.append(f"- 跳过（无工具步骤）：{', '.join(alignment.skipped)}")
    if alignment.single_trace:
        lines.append("- 单轨迹：不做骨架宣称，只能发布为窄口径版本")
    elif not alignment.uniform_support:
        lines.append("- 骨架并非每条轨迹都有，泛化性有限")
    lines.extend(["", "| # | 角色 | 形状 | support | 参数差异 |", "| --- | --- | --- | --- | --- |"])
    for order, slot in enumerate(alignment.slots):
        shape = slot.command_shape.replace("|", "\\|") or "-"
        varied = "是" if slot.varies else "否"
        lines.append(
            f"| {order} | {slot.role} | `{shape}` | {slot.support}/{alignment.support_total} | {varied} |")
    return "\n".join(lines) + "\n"
