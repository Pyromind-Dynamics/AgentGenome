"""断点的原子读写及 M0 resume 校验。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class CheckpointError(RuntimeError):
    pass


class Checkpoint:
    def __init__(self, run_dir: Path, data: dict[str, Any]) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "checkpoint.json"
        self.data = data

    @classmethod
    def create(cls, run_dir: Path, pin: str, params: dict[str, Any]) -> "Checkpoint":
        try:
            json.dumps(params)
        except (TypeError, ValueError) as exc:
            raise CheckpointError(f"params 必须可 JSON 序列化：{exc}") from exc
        checkpoint = cls(run_dir, {"ckpt_v": 1, "pin": pin, "params": params, "nodes": {}})
        checkpoint.write()
        return checkpoint

    @classmethod
    def load(cls, run_dir: Path, pin: str, graph_paths: set[str]) -> "Checkpoint":
        path = run_dir / "checkpoint.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"断点损坏：{exc}") from exc
        if not isinstance(raw, dict) or raw.get("ckpt_v") != 1:
            raise CheckpointError("断点损坏：ckpt_v 必须为 1")
        if not isinstance(raw.get("pin"), str) or not isinstance(raw.get("params"), dict) or not isinstance(raw.get("nodes"), dict):
            raise CheckpointError("断点损坏：缺少 pin、params 或 nodes")
        if raw["pin"] != pin:
            raise CheckpointError("断点 pin 与当前 Graph 不一致，拒绝 resume")
        for node_path, entry in raw["nodes"].items():
            if not isinstance(node_path, str) or node_path not in graph_paths:
                raise CheckpointError("断点损坏：包含图中不存在的节点")
            if not isinstance(entry, dict) or not isinstance(entry.get("retries"), int):
                raise CheckpointError("断点损坏：节点 retries 无效")
            status = entry.get("status")
            if status is not None and status not in {"succeeded", "failed", "abandoned",
                                                     "waiting", "waiting_external"}:
                raise CheckpointError(
                    "断点损坏：节点状态必须是 succeeded/failed/abandoned/"
                    "waiting/waiting_external")
            if status == "succeeded" and not isinstance(entry.get("outputs"), dict):
                raise CheckpointError("断点损坏：成功节点缺少 outputs")
            if status in {"failed", "abandoned", "waiting", "waiting_external"} and "outputs" in entry:
                raise CheckpointError("断点损坏：未成功节点不可带 outputs")
            if status == "waiting_external" and not isinstance(entry.get("await"), dict):
                raise CheckpointError("断点损坏：外部等待节点缺少 await 记录")
        return cls(run_dir, raw)

    @property
    def params(self) -> dict[str, Any]:
        return self.data["params"]

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return self.data["nodes"]

    def entry(self, node_path: str) -> dict[str, Any] | None:
        return self.nodes.get(node_path)

    def mark_retry(self, node_path: str, retries: int) -> None:
        entry = self.nodes.setdefault(node_path, {})
        entry["retries"] = retries
        entry.pop("status", None)
        entry.pop("outputs", None)
        self.write()

    def mark_succeeded(self, node_path: str, retries: int, outputs: dict[str, Any]) -> None:
        self.nodes[node_path] = {"status": "succeeded", "retries": retries, "outputs": outputs}
        self.write()

    def mark_failed(self, node_path: str, retries: int) -> None:
        self.nodes[node_path] = {"status": "failed", "retries": retries}
        self.write()

    def mark_abandoned(self, node_path: str, retries: int) -> None:
        self.nodes[node_path] = {"status": "abandoned", "retries": retries}
        self.write()

    def mark_waiting(self, node_path: str, retries: int) -> None:
        """Park a node: not a verdict, so a resume re-enters it and asks again."""
        self.nodes[node_path] = {"status": "waiting", "retries": retries}
        self.write()

    def mark_waiting_external(self, node_path: str, retries: int, *,
                              receipt: str, product: str | None,
                              awaited_at: float, deadline: float | None,
                              task_id: str | None = None,
                              output_dir: str | None = None) -> None:
        """Park a node on an external task: the worker is released, not blocked.

        The receipt and the deadline live in the checkpoint so a resume after a
        crash still knows what it was waiting for and when to give up.
        """
        self.nodes[node_path] = {
            "status": "waiting_external",
            "retries": retries,
            "await": {
                "receipt": receipt,
                "product": product,
                "awaited_at": awaited_at,
                "deadline": deadline,
                "task_id": task_id,
                "output_dir": output_dir,
            },
        }
        self.write()

    def awaiting(self, node_path: str) -> dict[str, Any] | None:
        entry = self.nodes.get(node_path) or {}
        record = entry.get("await")
        return record if isinstance(record, dict) else None

    def reenter(self, node_path: str) -> None:
        entry = self.nodes[node_path]
        entry.pop("status", None)
        entry.pop("outputs", None)
        self.write()

    def clear_failed_descendants(self, node_path: str) -> None:
        prefix = node_path + "."
        for path in list(self.nodes):
            if path.startswith(prefix) and self.nodes[path].get("status") == "failed":
                del self.nodes[path]
        self.write()

    def write(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
