"""Stage C-2: turn a sliced alignment into a compiled graph package.

Generation is mechanical. A captured command is never rewritten, only the exact
path tokens observed in the trace are replaced with graph slots, and a script file
is copied byte for byte. Anything that cannot be solidified (inline code, files
authored by an editor tool, agent tasks) is reported instead of guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import yaml

from builder import Graph, compile as compile_graph

from ..manifest import MATCH_KEYS, ManifestError, declared
from ..manifest import validate as validate_manifest
from .align import Alignment, Slot
from .slice import Slice
from .trace import (KIND_HUMAN, KIND_LLM, KIND_SHELL, KIND_TOOL, KIND_WRITE, ROLE_SCRIPT,
                    Step, Trace, canonical_path, parse_command, role)

DEFAULT_VERSION = "0.1.0"
_TEXT_SUFFIXES = {".csv", ".jsonl", ".txt", ".tsv", ".md", ".log", ".yaml", ".yml"}
_KEY = re.compile(r"[^a-z0-9]+")
_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_BOUNDARY = r"(?<![\w./-])"
_SCRIPT_MARK = re.compile(r"<script:([^>]+)>")
_SHAPE_HEAD = re.compile(r"([A-Za-z][A-Za-z0-9._+-]*)(?=\s|\Z)")
_SUFFIX = re.compile(r"[a-z0-9]{1,12}")
_RUNTIME_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.(?:py|sh)\b")
_RUNTIME_LIMIT = 32


class EmitError(ValueError):
    pass


@runtime_checkable
class Source(Protocol):
    """Where a captured script is read from: the local workspace, or a host API."""

    def read(self, path: str) -> bytes: ...


@runtime_checkable
class BatchSource(Protocol):
    """A host that answers many reads in one call (the SDK execution bridge).

    Freezing a chain reads every script and its runtime siblings, and the chain is
    built twice (draft and capture); a per-file read turns one slow sandbox into
    dozens of round trips. Hosts that cannot batch keep the per-file path.
    """

    def read_many(self, paths: Sequence[str]) -> Mapping[str, bytes | None]: ...


@dataclass(frozen=True)
class LocalSource:
    root: Path

    def _target(self, path: str) -> Path:
        target = Path(path)
        return target if target.is_absolute() else self.root / target

    def read(self, path: str) -> bytes:
        return self._target(path).read_bytes()

    def read_many(self, paths: Sequence[str]) -> dict[str, bytes | None]:
        answers: dict[str, bytes | None] = {}
        for path in paths:
            try:
                answers[path] = self.read(path)
            except Exception:  # noqa: BLE001 - a probe, not a required read
                answers[path] = None
        return answers

    def head(self, path: str, limit: int) -> bytes:
        with self._target(path).open("rb") as stream:
            return stream.read(limit)


def source_for(workspace: Source | str | Path) -> Source:
    if isinstance(workspace, (str, Path)):
        return LocalSource(Path(workspace).resolve())
    return workspace


def read_many(source: Source, paths: Sequence[str]) -> dict[str, bytes | None]:
    """Answer a batch of reads, collapsing a failed probe to ``None``."""
    wanted = list(dict.fromkeys(str(path) for path in paths))
    if isinstance(source, BatchSource):
        return {str(path): value for path, value in source.read_many(wanted).items()}
    answers: dict[str, bytes | None] = {}
    for path in wanted:
        try:
            answers[path] = source.read(path)
        except Exception:  # noqa: BLE001 - a probe, not a required read
            answers[path] = None
    return answers


def unresolved_literals(source: Source, command: str) -> tuple[tuple[str, str], ...]:
    """The flag values of one command that only resolve in the session's own cwd.

    A frozen command replays with the package as its working directory, so a value
    the parser could not classify (`--images-root storage`) silently points at
    nothing unless it is a graph slot. Only a token the workspace really has counts:
    `--pipeline llm-filter` is a scalar and stays a literal.
    """
    return tuple((flag, value) for flag, value in parse_command(command).literals
                 if _names_something(source, value))


def _names_something(source: Source, token: str) -> bool:
    try:
        source.read(token)
    except IsADirectoryError:
        return True
    except Exception as exc:  # noqa: BLE001 - a probe, not a required read
        # A remote host words the same condition as a request error.
        return "is a directory" in str(exc).lower()
    return True


@dataclass(frozen=True)
class EmittedNode:
    order: int
    id: str
    source_trace: str
    provenance: tuple[str, ...]
    inputs: tuple[tuple[str, str, str], ...]
    output: tuple[str, str] | None
    command: str
    extra_writes: tuple[str, ...]
    kind: str = "run"
    verify_command: str | None = None
    artifact_ext: str | None = None
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None

    @property
    def verify(self) -> str:
        if self.verify_command:
            return self.verify_command
        return f"test -s {{output.{self.output[0]}}}" if self.output else "true"

    @property
    def declaration(self) -> str:
        """How this node's execution reads in a report: its command, or its tool call."""
        if self.kind == "tool":
            return json.dumps({"tool": self.tool_name, "args": self.arguments},
                              ensure_ascii=False)
        return self.command


@dataclass(frozen=True)
class Package:
    directory: Path
    asset_id: str
    version: str
    label: str
    owner: str
    root_id: str
    params: tuple[tuple[str, str], ...]
    nodes: tuple[EmittedNode, ...]
    scripts: tuple[tuple[str, str], ...]
    absorbed: tuple[tuple[int, str], ...]
    not_solidifiable: tuple[tuple[int, str], ...]
    auxiliary: tuple[int, ...]
    final_artifact: str | None
    applicability: dict[str, Any]
    graph: Graph

    @property
    def solidifiable(self) -> bool:
        return not self.not_solidifiable

    @property
    def pin(self) -> str:
        return self.graph.pin

    @property
    def outputs(self) -> tuple[tuple[str, str], ...]:
        return tuple(node.output for node in self.nodes if node.output is not None)

    @classmethod
    def load(cls, directory: str | Path) -> "Package":
        """Reload a package from disk, for work that outlives the capture call."""
        root = Path(directory)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        graph = compile_graph(root)
        capture = manifest.get("capture") or {}
        return cls(
            directory=root,
            asset_id=manifest["id"],
            version=manifest["version"],
            label=manifest["name"],
            owner=manifest.get("owner", "local"),
            root_id=graph.node.id,
            params=tuple((key, "") for key in sorted(graph.params)),
            nodes=(),
            scripts=(),
            absorbed=(),
            not_solidifiable=(),
            auxiliary=tuple(capture.get("auxiliary") or ()),
            final_artifact=capture.get("final_artifact"),
            applicability=declared(manifest),
            graph=graph,
        )


class _Keys:
    def __init__(self) -> None:
        self.taken: set[str] = set()

    def claim(self, base: str) -> str:
        candidate, index = base, 1
        while candidate in self.taken:
            index += 1
            candidate = f"{base}_{index}"
        self.taken.add(candidate)
        return candidate


def _key_base(path: str) -> str:
    text = _KEY.sub("_", PurePosixPath(path).stem.lower()).strip("_")
    if not text:
        return "value"
    return text if text[0].isalpha() else f"p_{text}"


def _slug(value: str, fallback: str, *, allow_digit: bool) -> str:
    text = _KEY.sub("-", value.strip().lower()).strip("-")
    if not text or (not allow_digit and not text[0].isalpha()):
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        return f"{fallback}-{text}-{digest}" if text else f"{fallback}-{digest}"
    return text[:64].rstrip("-")


def output_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    return "path"


def artifact_ext(path: str) -> str | None:
    """The suffix the node's artifact carries, so readers can pick a parser by name."""
    suffix = PurePosixPath(path).suffix.lstrip(".").lower()
    return f".{suffix}" if _SUFFIX.fullmatch(suffix) else None


def _replace_tokens(command: str, mapping: Mapping[str, str]) -> str:
    """Replace whole shell tokens, keeping the original quoting style intact."""
    text = command
    missing: list[str] = []
    for token in sorted(mapping, key=len, reverse=True):
        quoted = re.escape(token)
        pattern = re.compile(
            _BOUNDARY + r"(?:'" + quoted + r"'|\"" + quoted + r"\"|" + quoted
            + r")(?![\w./-])"
        )
        text, count = pattern.subn(mapping[token], text)
        if not count:
            missing.append(token)
    if missing:
        raise EmitError(f"命令里找不到这些路径：{', '.join(missing)}；命令：{command}")
    return text


def _replace_json_tokens(value: Any, mapping: Mapping[str, str]) -> Any:
    """Replace path tokens inside a tool call's args without quoting or retyping them.

    An arg that is exactly a declared path becomes the slot for it; an arg that merely
    contains the path keeps its surrounding text. JSON numbers and bools pass through,
    because a tool's args are values, not a command line.
    """
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        present = {token: target for token, target in mapping.items() if token in value}
        return _replace_tokens(value, present) if present else value
    if isinstance(value, dict):
        return {key: _replace_json_tokens(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_json_tokens(item, mapping) for item in value]
    return value


class _Plan:
    def __init__(self, alignment: Alignment, slice_: Slice, traces: Sequence[Trace],
                 label: str, owner: str, source: Source, version: str,
                 description: str | None, asset_id: str | None,
                 bindings: Mapping[str, str] | None = None) -> None:
        self.slots = alignment.slots
        self.slice = slice_
        self.traces = traces
        self.steps = {step.trace_id: {item.seq: item for item in trace.steps}
                      for trace in traces for step in trace.steps}
        self.label = label
        self.owner = owner
        self.source = source
        self.version = version
        self.description = description
        self.keys = _Keys()
        self.bindings = {str(key): str(value) for key, value in (bindings or {}).items()}
        self.root_id = _slug(label, "capture", allow_digit=False)
        if asset_id is not None and not _ASSET_ID.fullmatch(asset_id):
            raise EmitError(f"资产 id 只能是字母数字和 . _ -：{asset_id}")
        self.asset_id = asset_id or _slug(label, "capture", allow_digit=True)
        self.node_ids: dict[int, str] = {}
        self.outputs: dict[int, tuple[str, str, str]] = {}
        self.linkable: dict[str, tuple[int, str, str, str]] = {}
        self.params: dict[str, str] = {}
        self.scripts: dict[str, str] = {}
        self.absorbed: dict[int, str] = {}
        self.not_solidifiable: dict[int, str] = {}
        self.nodes: list[EmittedNode] = []

    def step(self, order: int) -> Step:
        slot = self.slots[order]
        trace_id, seq = slot.evidence[0]
        step = self.steps[trace_id][seq]
        assert step.signature == slot.signature
        return step

    def file_of(self, path: str) -> str:
        """The file a path token names, so both spellings of it pair up."""
        return canonical_path(path, self.bindings)

    @property
    def output_paths(self) -> dict[str, str]:
        return {key: primary for key, primary, _ in self.outputs.values()}

    def plan(self) -> None:
        self._classify()
        self._dedupe_node_ids()
        self._plan_scripts()
        self._plan_outputs()
        self._build_nodes()

    def _dedupe_node_ids(self) -> None:
        """Command shapes repeat, so sibling node ids must be made unique up front.

        Every node needs a stable id before outputs are planned, because a later node
        links to its inputs by node id.
        """
        taken = {self.root_id}
        for order in sorted(self.node_ids):
            base = self.node_ids[order]
            candidate, index = base, 2
            while candidate in taken:
                candidate = f"{base}-{index}"
                index += 1
            taken.add(candidate)
            self.node_ids[order] = candidate

    def _classify(self) -> None:
        for order in self.slice.nodes:
            step = self.step(order)
            script_writes = [path for path in step.write_paths
                             if role(path, writing=True) == ROLE_SCRIPT]
            if step.kind == KIND_SHELL and not step.inline_code and step.command:
                base = self._base_id(step, order)
                self.node_ids[order] = _slug(base, f"step-{order}", allow_digit=False)
                continue
            # A distilled llm step is a first-class leaf: it declares its inputs and a
            # single artifact, so emit can hand the agent a `do: {llm}` node to replay.
            # An opaque session `task` call has no declared artifact, so it stays auxiliary.
            if step.kind == KIND_LLM and step.command and step.write_paths:
                base = self._base_id(step, order)
                self.node_ids[order] = _slug(base, f"llm-{order}", allow_digit=False)
                continue
            # A distilled tool call is a first-class leaf too: it names the host tool,
            # its args and the artifact the call's JSON result is written to.
            if step.kind == KIND_TOOL and step.tool_name and step.write_paths:
                base = self._base_id(step, order)
                self.node_ids[order] = _slug(base, f"tool-{order}", allow_digit=False)
                continue
            # A distilled human step is a first-class leaf as well: it names the
            # question the person answers and the artifact their answer must land in,
            # so the graph parks there instead of treating the gate as an unmodelled step.
            if step.kind == KIND_HUMAN and step.command and step.write_paths:
                base = self._base_id(step, order)
                self.node_ids[order] = _slug(base, f"human-{order}", allow_digit=False)
                continue
            if script_writes and len(script_writes) == len(step.write_paths):
                self.absorbed[order] = "脚本创建步骤已由脚本搬运吸收"
                continue
            self.not_solidifiable[order] = self._reason(step)

    @staticmethod
    def _base_id(step: Step, order: int) -> str:
        """Name a node after the script it runs, else after the command itself."""
        if step.kind == KIND_TOOL:
            return step.tool_name or f"tool-{order}"
        if step.kind in (KIND_LLM, KIND_HUMAN) and step.write_paths:
            return PurePosixPath(step.write_paths[0]).stem
        match = _SCRIPT_MARK.search(step.command_shape)
        if match:
            return match.group(1)
        head = _SHAPE_HEAD.match(step.command_shape)
        if head:
            return head.group(1)
        return step.tool_name or f"step-{order}"

    @staticmethod
    def _reason(step: Step) -> str:
        if step.kind == KIND_WRITE:
            return "编辑工具写入的内容没有记录，无法机械固化"
        if step.kind == KIND_LLM:
            return "agent 任务无法机械固化"
        if step.kind == KIND_HUMAN:
            return ("人工门禁没有声明产出 artifact（用 genome_distill 声明 output），"
                    "无法确定人的答复落在哪里")
        if step.kind == KIND_TOOL:
            return ("工具调用没有声明产出 artifact（用 genome_distill 声明 output），"
                    "无法确定结果落在哪里")
        if step.inline_code:
            return "内联代码未固化"
        return f"{step.kind} 步骤没有可搬运的命令"

    def _plan_scripts(self) -> None:
        taken: dict[str, str] = {}
        queue: list[str] = []
        for order in self.node_ids:
            for path in self.step(order).read_paths:
                if role(path, writing=False) != ROLE_SCRIPT or path in self.scripts:
                    continue
                self._plan_script(path, taken)
                queue.append(path)
        self._plan_runtime_siblings(queue, taken)

    def _plan_script(self, path: str, taken: dict[str, str]) -> str:
        suffix = PurePosixPath(path).suffix
        stem = PurePosixPath(path).stem
        target, index = f"scripts/{stem}{suffix}", 2
        while target in taken:
            target = f"scripts/{stem}-{index}{suffix}"
            index += 1
        taken[target] = path
        self.scripts[path] = target
        return target

    def _plan_runtime_siblings(self, queue: list[str], taken: dict[str, str]) -> None:
        """A copied script keeps the runtime files it names next to it.

        A skill CLI such as `sandbox_sample.py` shells out to sibling helpers and
        puts its own directory on `PYTHONPATH` for the pipelines it runs. Freezing
        only the CLI would leave the package unrunnable, so named siblings are
        copied transitively under their original file names.
        """
        while queue and len(self.scripts) < _RUNTIME_LIMIT:
            wave, queue = queue, []
            contents = read_many(self.source, wave)
            candidates: list[tuple[str, str]] = []
            probed: set[str] = set()
            for origin in wave:
                content = contents.get(origin)
                if content is None:
                    continue
                directory = PurePosixPath(origin).parent
                names = set(_RUNTIME_REF.findall(content.decode("utf-8", "replace")))
                for name in sorted(names):
                    sibling = str(directory / name)
                    target = f"scripts/{name}"
                    if sibling in self.scripts or target in taken or sibling in probed:
                        continue
                    probed.add(sibling)
                    candidates.append((sibling, target))
            siblings = (
                read_many(self.source, [sibling for sibling, _ in candidates])
                if candidates
                else {}
            )
            for sibling, target in candidates:
                if len(self.scripts) >= _RUNTIME_LIMIT:
                    break
                if sibling in self.scripts or target in taken:
                    continue
                if siblings.get(sibling) is None:
                    continue
                taken[target] = sibling
                self.scripts[sibling] = target
                queue.append(sibling)

    def _plan_outputs(self) -> None:
        consumed: set[str] = set()
        for order in sorted(self.node_ids, reverse=True):
            step = self.step(order)
            primary = next((path for path in step.write_paths
                            if self.file_of(path) in consumed), None)
            if primary is None and step.write_paths:
                primary = step.write_paths[-1]
            if primary is not None:
                key = self.keys.claim(_key_base(primary))
                node_id = self.node_ids[order]
                self.outputs[order] = (key, primary, output_type(primary))
                self.linkable[self.file_of(primary)] = (
                    order, node_id, key, output_type(primary))
            consumed.update(self.file_of(path) for path in step.read_paths)

    def _input_for(self, path: str, order: int) -> tuple[str, str, str]:
        link = self.linkable.get(self.file_of(path))
        if link is not None and link[0] < order:
            _, node_id, key, type_name = link
            return key, type_name, f"{node_id}.output.{key}"
        key = self.params.get(path)
        if key is None:
            key = self.keys.claim(_key_base(path))
            self.params[path] = key
        return key, "path", f"{self.root_id}.input.{key}"

    def _build_nodes(self) -> None:
        for order in sorted(self.node_ids):
            step = self.step(order)
            written = {self.file_of(path) for path in step.write_paths}
            both = [path for path in step.read_paths if self.file_of(path) in written]
            if both:
                raise EmitError(
                    f"步骤 {order} 原地读写同一路径，v2 不支持：{', '.join(sorted(both))}")
            inputs: list[tuple[str, str, str]] = []
            mapping: dict[str, str] = {}
            for path in step.read_paths:
                if path in self.scripts:
                    mapping[path] = self.scripts[path]
                    continue
                key, type_name, source = self._input_for(path, order)
                if all(key != item[0] for item in inputs):
                    inputs.append((key, type_name, source))
                mapping[path] = "{input." + key + "}"
            output = self.outputs.get(order)
            extra: tuple[str, ...] = ()
            ext: str | None = None
            if output is not None:
                _, primary, _ = output
                # A node writes into its own artifact; {output.*} only resolves for
                # ancestors and siblings, which is what verify and compose use.
                mapping[primary] = "{artifact}"
                extra = tuple(path for path in step.write_paths if path != primary)
                ext = artifact_ext(primary)
            kind = {"llm": "llm", "human": "human"}.get(step.kind, "run")
            arguments: Mapping[str, Any] | None = None
            command = ""
            if step.kind == KIND_TOOL:
                kind = "tool"
                # Only declared reads are slots in the args: the output path names the
                # artifact the result is written to, it is not something the tool receives.
                arguments = _replace_json_tokens(
                    dict(step.arguments or {}),
                    {path: mapping[path] for path in step.read_paths if path in mapping},
                )
            else:
                command = _replace_tokens(step.command, mapping)
            self.nodes.append(EmittedNode(
                order=order,
                id=self.node_ids[order],
                source_trace=step.trace_id,
                provenance=tuple(f"{trace_id}:{seq}"
                                 for trace_id, seq in self.slots[order].evidence),
                inputs=tuple(inputs),
                output=None if output is None else (output[0], output[2]),
                command=command,
                extra_writes=extra,
                kind=kind,
                verify_command=step.verify or None,
                artifact_ext=ext,
                tool_name=step.tool_name if step.kind == KIND_TOOL else None,
                arguments=arguments,
            ))


def _node_yaml(node: EmittedNode) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": node.id,
        "provenance": list(node.provenance),
        "input": {key: {"type": type_name, "from": source}
                  for key, type_name, source in node.inputs},
        "do": (
            {"tool": {"name": node.tool_name, "args": dict(node.arguments or {})}}
            if node.kind == "tool"
            else {node.kind: node.command}
        ),
    }
    if node.output is not None:
        key, type_name = node.output
        data["output"] = {key: {"type": type_name}}
    if node.artifact_ext is not None:
        data["artifact"] = {"ext": node.artifact_ext.lstrip(".")}
    data["verify"] = [{"run": node.verify}]
    return data


def _composed(plan: _Plan) -> tuple[str, str, str] | None:
    composed = [(node.id, node.output[0], node.output[1])
                for node in plan.nodes if node.output is not None]
    return composed[-1] if composed else None


def _root_yaml(plan: _Plan) -> dict[str, Any]:
    root: dict[str, Any] = {
        "id": plan.root_id,
        "mode": "sequence",
        "provenance": sorted({seq for node in plan.nodes for seq in node.provenance}),
        "input": {key: {"type": "path", "from": f"params.{key}"}
                  for _, key in sorted(plan.params.items(), key=lambda item: item[1])},
        "children": [_node_yaml(node) for node in plan.nodes],
    }
    composed = _composed(plan)
    if composed:
        node_id, key, type_name = composed
        root["output"] = {key: {"type": type_name, "compose": [f"{node_id}.output.{key}"]}}
        root["verify"] = [{"run": f"test -s {{output.{key}}}"}]
    else:
        root["verify"] = [{"run": "true"}]
    return root


def _determinism(nodes: Sequence[EmittedNode]) -> str:
    """What replaying this graph needs: a person, the agent, or only fixed calls."""
    kinds = {node.kind for node in nodes}
    if "human" in kinds:
        return "human-gated"
    if "llm" in kinds:
        return "agent-assisted"
    return "script"


def _manifest(plan: _Plan, alignment: Alignment, slice_: Slice,
              declarations: Mapping[str, Any] | None = None) -> dict[str, Any]:
    trace_ids = list(dict.fromkeys(trace.trace_id for trace in plan.traces))
    composed = _composed(plan)
    manifest: dict[str, Any] = {
        "id": plan.asset_id,
        "version": plan.version,
        "name": plan.label,
        "description": plan.description or (
            f"由 {len(trace_ids)} 条 Pi 轨迹沉淀：{plan.label}"),
        "owner": plan.owner,
        "parameters": {key: {"type": "path", "description": f"根输入：{path}"}
                       for path, key in sorted(plan.params.items(), key=lambda item: item[1])},
        "requires": [],
        "expected_outputs": {} if composed is None else {
            composed[1]: f"由节点 {composed[0]} 产出，原轨迹路径 {plan.output_paths[composed[1]]}"},
        "revision": {
            "safe_to_rerun": True,
            "editable_scripts": sorted(plan.scripts.values()),
            "verification_resources": [],
        },
        "capture": {
            "traces": trace_ids,
            "skeleton": list(slice_.nodes),
            "auxiliary": list(slice_.auxiliary),
            "final_artifact": slice_.final_artifact,
            "threshold": alignment.threshold,
            "required_support": alignment.required_support,
            "single_trace": alignment.single_trace,
            "not_solidifiable": [{"order": order, "reason": reason}
                                 for order, reason in sorted(plan.not_solidifiable.items())],
            "absorbed": [{"order": order, "reason": reason}
                         for order, reason in sorted(plan.absorbed.items())],
        },
    }
    supported = {key: value for key, value in (declarations or {}).items()
                 if key in MATCH_KEYS}
    manifest["determinism"] = supported.get(
        "determinism", _determinism(plan.nodes))
    manifest.update({key: value for key, value in supported.items()
                     if key != "determinism"})
    return manifest


def _verify_provenance(nodes: Sequence[EmittedNode], expected: Sequence[int],
                       slots: Sequence[Slot]) -> None:
    wanted = {f"{trace_id}:{seq}" for order in expected
              for trace_id, seq in slots[order].evidence}
    covered = {item for node in nodes for item in node.provenance}
    uncovered, invented = sorted(wanted - covered), sorted(covered - wanted)
    if uncovered or invented:
        raise EmitError(
            f"provenance 校验失败：uncovered_steps={uncovered} invented_steps={invented}")


def build_package(directory: str | Path, *, alignment: Alignment, slice_: Slice,
                  traces: Sequence[Trace], label: str, workspace: Source | str | Path,
                  owner: str = "local", version: str = DEFAULT_VERSION,
                  description: str | None = None,
                  asset_id: str | None = None,
                  declarations: Mapping[str, Any] | None = None,
                  bindings: Mapping[str, str] | None = None
                  ) -> Package:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    plan = _Plan(alignment, slice_, traces, label, owner, source_for(workspace),
                 version, description, asset_id, bindings)
    plan.plan()
    expected = [order for order in slice_.nodes
                if order in plan.node_ids]
    _verify_provenance(plan.nodes, expected, alignment.slots)
    scripts: list[tuple[str, str]] = []
    planned_scripts = sorted(plan.scripts.items())
    contents = (
        read_many(plan.source, [origin for origin, _ in planned_scripts])
        if planned_scripts
        else {}
    )
    for origin, target in planned_scripts:
        content = contents.get(origin)
        if content is None:
            raise EmitError(f"脚本不存在，无法搬运：{origin}")
        destination = root / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        scripts.append((origin, target))
    (root / "graph.yaml").write_text(
        yaml.safe_dump({"version": "gt/1.0", "node": _root_yaml(plan)},
                       allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8")
    manifest = _manifest(plan, alignment, slice_, declarations)
    try:
        validate_manifest(manifest)
    except ManifestError as exc:
        raise EmitError(str(exc)) from exc
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    graph = compile_graph(root)
    return Package(
        directory=root,
        asset_id=plan.asset_id,
        version=version,
        label=label,
        owner=owner,
        root_id=plan.root_id,
        params=tuple((key, path) for path, key in sorted(plan.params.items(),
                                                         key=lambda item: item[1])),
        nodes=tuple(plan.nodes),
        scripts=tuple(scripts),
        absorbed=tuple(sorted(plan.absorbed.items())),
        not_solidifiable=tuple(sorted(plan.not_solidifiable.items())),
        auxiliary=slice_.auxiliary,
        final_artifact=slice_.final_artifact,
        applicability=declared(manifest),
        graph=graph,
    )


def package_payload(package: Package) -> dict[str, Any]:
    return {
        "asset_id": package.asset_id,
        "version": package.version,
        "owner": package.owner,
        "pin": package.graph.pin,
        "solidifiable": package.solidifiable,
        "params": [{"key": key, "source": path} for key, path in package.params],
        "scripts": [{"source": source, "package": target} for source, target in package.scripts],
        "nodes": [
            {
                "order": node.order,
                "id": node.id,
                "kind": node.kind,
                "command": node.command,
                "provenance": list(node.provenance),
                "source_trace": node.source_trace,
                "inputs": [{"key": key, "type": type_name, "from": source}
                           for key, type_name, source in node.inputs],
                "output": None if node.output is None else
                {"key": node.output[0], "type": node.output[1]},
                "extra_writes": list(node.extra_writes),
            }
            for node in package.nodes
        ],
        "absorbed": [{"order": order, "reason": reason} for order, reason in package.absorbed],
        "not_solidifiable": [{"order": order, "reason": reason}
                             for order, reason in package.not_solidifiable],
        "auxiliary": list(package.auxiliary),
        "final_artifact": package.final_artifact,
        **package.applicability,
    }


def render_emit(package: Package) -> str:
    lines = [
        "### 生成", "",
        f"- 资产：`{package.asset_id}` v{package.version}（owner {package.owner}）",
        f"- 根输入参数：{'、'.join(f'`{key}`' for key, _ in package.params) or '无'}",
        f"- 脚本搬运：{'、'.join(f'`{source}` → `{target}`' for source, target in package.scripts) or '无'}",
        f"- 编译：通过（pin `{package.graph.pin[:12]}`）",
        "",
        "| # | 节点 | 命令 | provenance |",
        "| --- | --- | --- | --- |",
    ]
    for node in package.nodes:
        command = node.declaration.replace("|", "\\|")
        provenance = ",".join(str(seq) for seq in node.provenance)
        lines.append(f"| {node.order} | `{node.id}` | `{command}` | {provenance} |")
    if package.absorbed:
        lines.extend(["", "脚本创建步骤（已由脚本搬运吸收）：" + "；".join(
            f"#{order} {reason}" for order, reason in package.absorbed)])
    if package.not_solidifiable:
        lines.extend(["", "**未固化步骤**：" + "；".join(
            f"#{order} {reason}" for order, reason in package.not_solidifiable)])
    if package.auxiliary:
        lines.extend(["", "辅助步骤（不在切片内，未入图）："
                      + "、".join(f"#{order}" for order in package.auxiliary)])
    if package.applicability:
        lines.extend(["", "复用声明：" + "；".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in package.applicability.items())])
    return "\n".join(lines) + "\n"
