"""Stage C-1: keep only the skeleton steps the final artifact depends on.

The slice is the reverse closure of the write/read edges: start at the step that
produces the declared final artifact, then walk back through every input that some
earlier step produced. Skeleton steps outside that closure are auxiliary: they ran,
but the artifact does not depend on them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .. import paths
from .align import ROLE_SKELETON, Alignment, Slot
from .trace import canonical_path


class SliceError(ValueError):
    pass


OUTPUT_CONTRACT_HINT = ("产出路径必须写成命令里的显式输出（`> 路径`、`--out 路径`、"
                        "`--output 路径` 之类）；`python3 脚本.py 输入 产出` 这种位置参数一律"
                        "按输入识别，脚本内部写出的文件先改写成显式声明产出的命令")
_LIST_LIMIT = 8


def _produced(producers: dict[str, int]) -> str:
    paths = sorted(producers)
    shown = "、".join(f"`{path}`" for path in paths[:_LIST_LIMIT])
    if len(paths) > _LIST_LIMIT:
        shown += f" 等 {len(paths)} 个"
    return shown


@dataclass(frozen=True)
class Slice:
    nodes: tuple[int, ...]
    auxiliary: tuple[int, ...]
    final_artifact: str | None
    producers: tuple[tuple[str, int], ...]
    kept: tuple[int, ...] = ()

    @property
    def full(self) -> bool:
        return self.final_artifact is None


def producer_map(slots: Sequence[Slot],
                 bindings: Mapping[str, str] | None = None) -> dict[str, int]:
    """Last skeleton writer wins: a later step overwrites the earlier product.

    Producers are keyed by the file they name, so a `{param}` token and the path it
    binds to are one product however a later step spells it.
    """
    given = dict(bindings or {})
    result: dict[str, int] = {}
    for order, slot in enumerate(slots):
        if slot.role != ROLE_SKELETON:
            continue
        for path in slot.write_paths:
            result[paths.display(canonical_path(path, given))] = order
    return result


def reverse_slice(alignment: Alignment, final_artifact: str | None = None,
                  bindings: Mapping[str, str] | None = None) -> Slice:
    slots = alignment.slots
    given = dict(bindings or {})
    producers = producer_map(slots, given)
    skeleton = tuple(order for order, slot in enumerate(slots) if slot.role == ROLE_SKELETON)
    if not skeleton:
        raise SliceError("没有骨架步骤，无法生成图")
    kept = tuple(order for order in skeleton if slots[order].keep)
    if final_artifact is None:
        return Slice(skeleton, (), None, tuple(sorted(producers.items())), kept)
    root = producers.get(paths.display(canonical_path(final_artifact, given)))
    if root is None:
        detail = (f"骨架步骤的产出：{_produced(producers)}" if producers
                  else "骨架步骤没有产出任何文件")
        raise SliceError(
            f"最终产物 {final_artifact!r} 不在任何骨架步骤的产物里；{detail}。"
            f"{OUTPUT_CONTRACT_HINT}")
    # A step whose effect matters without feeding the final artifact (creating a
    # Label Studio project, submitting a platform job) is kept on purpose; its own
    # inputs then pull their producers in like any other node.
    keep = {root, *kept}
    stack = list(keep)
    while stack:
        order = stack.pop()
        for path in slots[order].read_paths:
            source = producers.get(paths.display(canonical_path(path, given)))
            if source is not None and source < order and source not in keep:
                keep.add(source)
                stack.append(source)
    nodes = tuple(sorted(keep))
    return Slice(
        nodes=nodes,
        auxiliary=tuple(order for order in skeleton if order not in keep),
        final_artifact=final_artifact,
        producers=tuple(sorted(producers.items())),
        kept=kept,
    )


def slice_payload(slice_: Slice, slots: Sequence[Slot]) -> dict[str, object]:
    return {
        "final_artifact": slice_.final_artifact,
        "full": slice_.full,
        "kept": list(slice_.kept),
        "nodes": [
            {"order": order, "shape": slots[order].command_shape,
             "writes": list(slots[order].write_paths)}
            for order in slice_.nodes
        ],
        "auxiliary": [
            {"order": order, "shape": slots[order].command_shape,
             "writes": list(slots[order].write_paths)}
            for order in slice_.auxiliary
        ],
    }


def render_slice(slice_: Slice, slots: Sequence[Slot]) -> str:
    lines = ["### 反向切片", ""]
    if slice_.full:
        lines.append("- 未声明最终产物，使用全量骨架")
    else:
        lines.append(f"- 最终产物：`{slice_.final_artifact}`")
    lines.append(f"- 入图 {len(slice_.nodes)} 步 / 辅助 {len(slice_.auxiliary)} 步")
    if slice_.kept:
        lines.append("- 强制保留（副作用步骤，产出不参与依赖闭包）："
                     + "、".join(f"#{order}" for order in slice_.kept))
    if slice_.auxiliary:
        lines.extend(["", "| # | 辅助步骤 | 产出 |", "| --- | --- | --- |"])
        for order in slice_.auxiliary:
            slot = slots[order]
            shape = slot.command_shape.replace("|", "\\|") or "-"
            lines.append(f"| {order} | `{shape}` | {','.join(slot.write_paths) or '-'} |")
    return "\n".join(lines) + "\n"
