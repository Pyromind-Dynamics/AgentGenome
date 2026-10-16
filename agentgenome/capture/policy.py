"""固化策略：一条链路按机械可执行性收口，还是按 agent 的判断收口。

`scripts-first` 是默认：会话里跑过的固定命令必须进图，llm 节点必须指出它来自哪一步，
插件据此拒绝「把固定命令伪装成 llm」。`adaptive-first` 显式信任 agent 的判断，
只保留「人工门禁/编辑工具产物不能变成 agent 任务」这类硬边界。
"""
from __future__ import annotations

from typing import Any

POLICY_SCRIPTS_FIRST = "scripts-first"
POLICY_ADAPTIVE_FIRST = "adaptive-first"
POLICIES = (POLICY_SCRIPTS_FIRST, POLICY_ADAPTIVE_FIRST)


def normalize_policy(value: Any) -> str:
    """Default to scripts-first; reject anything that is not a known policy."""
    if value is None or value == "":
        return POLICY_SCRIPTS_FIRST
    text = str(value)
    if text not in POLICIES:
        raise ValueError(f"policy 必须是 {' 或 '.join(POLICIES)}，收到 `{text}`")
    return text
