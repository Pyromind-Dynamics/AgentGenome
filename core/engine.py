"""M0 sequence 引擎：enter → exit(验收) → retry/report。"""

from __future__ import annotations

import ast
import json
import re
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from typing import Protocol

    class _Graph(Protocol):
        node: Any
        package_dir: Path

    Graph = _Graph
    Node = Any
    VerifyRule = Any

from .artifacts import LocalArtifacts
from .checkpoint import Checkpoint
from .ledger import Ledger
from .ports import (Answer, AwaitResult, Candidate, FailDecl, JudgeResult, PortError, Ports,
                    ShellResult, ToolResult)

_SLOT = re.compile(r"\{(artifact|tried\.[^{}]+|(?:input|output)\.[^{}]+)\}")
_EXACT_SLOT = re.compile(r"\{(artifact|tried\.[^{}]+|(?:input|output)\.[^{}]+)\}\Z")


@dataclass(frozen=True)
class Failure:
    node: str
    stage: str
    cause_seq: int
    summary: str


class NodeFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        self.failure = failure
        super().__init__(failure.summary)


class NodeAbandoned(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class NodeWaiting(Exception):
    """A human node parked: the run stops here and continues when the answer arrives."""

    def __init__(self, node: str, question: str, artifact: str) -> None:
        self.node = node
        self.question = question
        self.artifact = artifact
        super().__init__(question)


class NodeAwaiting(Exception):
    """An await node parked on an external task: no worker and no shell stay busy."""

    def __init__(self, node: str, receipt: str, awaited_at: float,
                 deadline: float | None, task_id: str | None = None,
                 output_dir: str | None = None) -> None:
        self.node = node
        self.receipt = receipt
        self.awaited_at = awaited_at
        self.deadline = deadline
        self.task_id = task_id
        self.output_dir = output_dir
        super().__init__(f"{node} is waiting for the task behind {receipt}")


class Engine:
    def __init__(self, graph: Graph, ports: Ports, run_dir: Path, checkpoint: Checkpoint, ledger: Ledger) -> None:
        self.graph = graph
        self.ports = ports
        self.artifacts = ports.artifacts or LocalArtifacts(run_dir)
        self.run_dir = run_dir
        self.checkpoint = checkpoint
        self.ledger = ledger
        self.refs: dict[str, dict[str, dict[str, Any]]] = {}
        self.tried: dict[str, FailDecl] = {}
        self.nodes = self._index(graph.node)

    def _index(self, node: Node) -> dict[str, Node]:
        result = {node.path: node}
        for child in node.children:
            result.update(self._index(child))
        return result

    def execute(self) -> dict[str, dict[str, Any]]:
        return self.node(self.graph.node)

    def node(self, node: Node) -> dict[str, dict[str, Any]]:
        if self.ports.check_cancel:
            self.ports.check_cancel()
        entry = self.checkpoint.entry(node.path)
        if entry and entry.get("status") == "succeeded":
            refs = entry["outputs"]
            self.refs[node.path] = refs
            return refs
        if entry and entry.get("status") == "failed":
            raise NodeFailed(Failure(node.path, "do", 0, "该节点已在断点中失败"))
        if entry and entry.get("status") in {"abandoned", "waiting", "waiting_external"}:
            self.checkpoint.reenter(node.path)
            entry = self.checkpoint.entry(node.path)

        retries = entry.get("retries", 0) if entry else 0
        attempt = retries + 1
        while True:
            self.ledger.append("node_enter", node=node.path, attempt=attempt)
            try:
                refs = self._attempt(node, attempt)
            except NodeWaiting as waiting:
                self.checkpoint.mark_waiting(node.path, retries)
                self.ledger.append(
                    "node_waiting", node=node.path, attempt=attempt,
                    question=self.ledger.log_ref("question", waiting.question),
                )
                raise
            except NodeAwaiting as awaiting:
                self.checkpoint.mark_waiting_external(
                    node.path, retries,
                    receipt=awaiting.receipt,
                    product=None,
                    awaited_at=awaiting.awaited_at,
                    deadline=awaiting.deadline,
                    task_id=awaiting.task_id,
                    output_dir=awaiting.output_dir,
                )
                self.ledger.append(
                    "node_awaiting", node=node.path, attempt=attempt,
                    receipt=awaiting.receipt, deadline=awaiting.deadline,
                    task_id=awaiting.task_id,
                )
                raise
            except NodeAbandoned as abandoned:
                self.checkpoint.mark_abandoned(node.path, retries)
                self.ledger.append(
                    "node_abandoned", node=node.path, attempt=attempt, reason=abandoned.reason
                )
                raise
            except NodeFailed as failed:
                failure = failed.failure
                if node.on_fail == "abort":
                    raise NodeAbandoned(f"节点声明放弃：{failure.summary}") from failed
                if node.on_fail == "ask":
                    if self._ask_on_fail(node, attempt, failure):
                        attempt += 1
                        continue
                    raise NodeAbandoned("人否决失败处置") from failed
                if node.retry is not None and attempt < node.retry:
                    self.checkpoint.mark_retry(node.path, attempt)
                    self.ledger.append(
                        "node_retry", node=node.path, attempt=attempt,
                        attempts=attempt, max=node.retry,
                    )
                    self.checkpoint.clear_failed_descendants(node.path)
                    retries = attempt
                    attempt = retries + 1
                    continue
                self.checkpoint.mark_failed(node.path, retries)
                self.ledger.append(
                    "node_failed", node=node.path, attempt=attempt,
                    stage=failure.stage, attempts=attempt, cause_seq=failure.cause_seq,
                    summary=failure.summary,
                )
                raise NodeFailed(Failure(node.path, failure.stage, failure.cause_seq, failure.summary)) from failed
            self.checkpoint.mark_succeeded(node.path, retries, refs)
            self.refs[node.path] = refs
            self.ledger.append("node_succeeded", node=node.path, attempt=attempt, outputs=refs)
            return refs

    def _ask_on_fail(self, node: Node, attempt: int, failure: Failure) -> bool:
        human = self.ports.human
        if human is None:
            self._verify_port_error("human", node, attempt, "未注入 human 端口")
        assert human is not None
        declaration = self._failure_declaration(node, failure, attempt)
        detail = {**declaration.__dict__, "cause_seq": failure.cause_seq}
        question = (
            f"节点 {node.path} 在 {failure.stage} 阶段第 {declaration.attempt} 次尝试失败："
            f"{failure.summary}。是否重试？"
        )
        evidence = {"node": node.path, "failure": detail}
        started = time.monotonic()
        try:
            answer = human.ask(question, evidence)
            if (not isinstance(answer, Answer) or not isinstance(answer.approve, bool)
                    or (not answer.approve and not answer.feedback and not answer.pending)):
                raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("human", node, attempt, str(exc), exc.kind)
        self.ledger.append(
            "call_human", node=node.path, attempt=attempt,
            question=self.ledger.log_ref("question", question), evidence=evidence,
            approve=answer.approve, feedback=answer.feedback, pending=answer.pending,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        if answer.pending:
            # A failure decision is a question like any other: park the run so the
            # status shows the failed node instead of blocking a background replay
            # on an answer that no conversation is going to deliver.
            artifact = self._artifact_path(node)
            raise NodeWaiting(node.path, question, str(artifact) if artifact else "")
        if not answer.approve:
            raise NodeAbandoned(answer.feedback or "人否决失败处置")
        return True

    def _failure_declaration(
        self, node: Node, failure: Failure, attempt: int | None = None,
    ) -> FailDecl:
        """以同一条 FailDecl 取证链服务 try 选路与 on_fail: ask。"""
        evidence: list[dict[str, Any]] = []
        artifact = self._artifact_path(node)
        try:
            evidence.append(self.artifacts.describe(str(artifact)))
        except FileNotFoundError:
            pass
        event = self.ledger.event(failure.cause_seq)
        if event:
            evidence.extend(
                ref for key in ("stdout", "stderr") if isinstance((ref := event.get(key)), dict)
            )
        if attempt is None:
            entry = self.checkpoint.entry(node.path) or {}
            attempt = int(entry.get("retries", 0)) + 1
        return FailDecl(node.id, failure.stage, failure.summary, evidence, attempt)

    def _attempt(self, node: Node, attempt: int) -> dict[str, dict[str, Any]]:
        if node.danger:
            self._danger_gate(node, attempt)
        if node.do is not None or node.tool is not None or node.await_call is not None:
            artifact = self._artifact_path(node)
            if node.tool is not None:
                seq = self._call_tool(node, artifact, attempt)
            elif node.await_call is not None:
                seq = self._await_product(node, artifact, attempt)
            elif node.do_kind == "human":
                seq = self._human_work(node, artifact, attempt)
            elif node.do_kind == "llm":
                prompt = self._fill(node.do, node, artifact, quote=False)
                seq = self._ai_work(prompt, node, attempt)
            else:
                command = self._fill(node.do, node, artifact)
                result, seq = self._shell(command, node, attempt, "call_shell")
                if result.rc != 0:
                    raise NodeFailed(Failure(node.path, "do", seq, f"do 命令退出码 {result.rc}"))
            try:
                refs = {key: self.artifacts.describe(str(artifact)) for key in node.output}
            except OSError:
                raise NodeFailed(Failure(node.path, "do", seq, "do 未物化声明的 artifact")) from None
        else:
            refs = self._try_children(node) if node.mode == "try" else self._sequence_children(node)

        for index, rule in enumerate(node.verify):
            leaf = (node.do is not None or node.tool is not None
                or node.await_call is not None)
            failure = self._verify(rule, index, node, attempt, refs, artifact if leaf else None)
            if failure:
                raise NodeFailed(failure)
        return refs

    def _sequence_children(self, node: Node) -> dict[str, dict[str, Any]]:
        for child in node.children:
            self.node(child)
        return self._compose(node)

    def _try_children(self, node: Node) -> dict[str, dict[str, Any]]:
        remaining = list(node.children)
        failures: list[FailDecl] = []
        self.tried = {}
        while remaining:
            if node.route == "llm":
                selected = self._route(node, remaining, failures)
                candidate = next(child for child in remaining if child.id == selected)
            else:
                candidate = remaining[0]
            remaining.remove(candidate)
            try:
                return self.node(candidate)
            except NodeFailed as failed:
                declaration = self._failure_declaration(candidate, failed.failure)
                failures.append(declaration)
                self.tried[candidate.id] = declaration
        raise NodeFailed(Failure(node.path, "do", 0, "try 候选池已耗尽"))

    def _route(self, node: Node, remaining: list[Node], failures: list[FailDecl]) -> str:
        ai = self.ports.ai
        if ai is None:
            self._verify_port_error("ai", node, 1, "未注入 ai 端口")
        assert ai is not None
        candidates = [Candidate(child.id, child.route_hint) for child in remaining]
        prompt = "请选择候选 id：" + json.dumps(
            {"failures": [item.__dict__ for item in failures],
             "candidates": [item.__dict__ for item in candidates]},
            ensure_ascii=False,
        )
        started = time.monotonic()
        try:
            chosen = ai.route(failures, candidates)
            if not isinstance(chosen, str) or chosen not in {item.id for item in candidates}:
                raise PortError("AIPort.route 返回池外候选 id", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("ai", node, 1, str(exc), exc.kind)
        self.ledger.append(
            "route", node=node.path, attempt=1,
            failures=[item.__dict__ for item in failures],
            candidates=[{"id": item.id, "hint": item.hint} for item in candidates],
            chosen=chosen, prompt=self.ledger.log_ref("route", prompt),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return chosen

    def _declaration(self, node: Node, artifact: Path | None) -> Any:
        """What a leaf is about to run, for the danger gate's evidence."""
        if node.tool is not None:
            return {"tool": node.tool.name,
                    "args": self._fill_json(dict(node.tool.args), node, artifact)}
        if node.do_kind == "llm":
            return self._fill(node.do, node, artifact, quote=False)
        return self._fill(node.do, node, artifact)

    def _danger_gate(self, node: Node, attempt: int) -> None:
        human = self.ports.human
        if human is None:
            self._verify_port_error("human", node, attempt, "未注入 human 端口")
        assert human is not None
        leaf = (node.do is not None or node.tool is not None
                or node.await_call is not None)
        artifact = self._artifact_path(node) if leaf else None
        execution = self._declaration(node, artifact) if leaf else None
        evidence = {
            "node": node.path,
            "execution": execution,
            "inputs": {key: self._input_slot(node, key) for key in node.input},
        }
        question = f"节点 {node.path} 声明 danger，即将执行以下动作，是否批准？"
        started = time.monotonic()
        try:
            answer = human.ask(question, evidence)
            if (not isinstance(answer, Answer) or not isinstance(answer.approve, bool)
                    or (not answer.approve and not answer.feedback and not answer.pending)):
                raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("human", node, attempt, str(exc), exc.kind)
        if not answer.batch_reused:
            self.ledger.append(
                "call_human", node=node.path, attempt=attempt,
                question=self.ledger.log_ref("question", question), evidence=evidence,
                approve=answer.approve, feedback=answer.feedback, pending=answer.pending,
                **({"batch_approval": True} if answer.batch_approval else {}),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        if answer.pending:
            # The approval is a question too: park instead of blocking the replay.
            raise NodeWaiting(node.path, question, str(artifact) if artifact else "")
        if not answer.approve:
            raise NodeAbandoned(answer.feedback or "人拒绝 danger gate")

    def _ai_work(self, prompt: str, node: Node, attempt: int) -> int:
        ai = self.ports.ai
        if ai is None and self.ports.agent_task is None:
            error = PortError("未注入 ai 端口")
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="ai", kind=error.kind, message=str(error),
            )
            raise error
        started = time.monotonic()
        try:
            if self.ports.agent_task is not None:
                response = self.ports.agent_task({
                    "node_id": node.path, "attempt": attempt, "prompt": prompt,
                    "inputs": {key: self._input_slot(node, key) for key in node.input},
                    "resource_dir": str(self.ports.cwd or self.graph.package_dir),
                    "verification_dir": str(self.ports.verification_cwd) if self.ports.verification_cwd else None,
                    "expected_outputs": [self.artifacts.path(str(self._artifact_path(node)))],
                    "constraints": "Write declared outputs, then call genome_step_result and end this turn. Do not modify verification resources or start another graph.",
                })
            else:
                assert ai is not None
                response = ai.work(prompt)
        except PortError as exc:
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="ai", kind=exc.kind, message=str(exc),
            )
            raise
        return self.ledger.append(
            "call_ai",
            node=node.path,
            attempt=attempt,
            prompt=self.ledger.log_ref("prompt", prompt),
            response=self.ledger.log_ref("response", response),
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _compose(self, node: Node) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, spec in node.output.items():
            # compile 已保证 compose 完备；M0 每键的源引用直接复制。
            source = spec.compose[0]
            try:
                result[key] = self.refs[source.node_path][source.key]
            except KeyError as exc:  # 防御性协议错误，图在运行期不会重新解释 YAML。
                raise RuntimeError(f"compose 来源不存在：{source.node_path}.{source.key}") from exc
        return result

    def _verify(
        self, rule: VerifyRule, index: int, node: Node, attempt: int, refs: dict[str, dict[str, Any]],
        artifact: Path | None,
    ) -> Failure | None:
        if rule.kind == "run":
            command = self._fill(rule.value, node, artifact, refs)
            result, seq = self._shell(command, node, attempt, "check", rule_index=index, rule_type="run")
            if result.rc:
                return Failure(node.path, "verify", seq, f"verify run 规则 {index} 退出码 {result.rc}")
            return None
        if rule.kind == "llm":
            criterion = self._fill(rule.value, node, artifact, refs, quote=False)
            context = self._verify_context(node, refs, artifact)
            ai = self.ports.ai
            if ai is None:
                return self._verify_port_error("ai", node, attempt, "未注入 ai 端口")
            started = time.monotonic()
            try:
                judgment = ai.judge(criterion, context)
                if (not isinstance(judgment, JudgeResult) or not isinstance(judgment.passed, bool)
                        or not isinstance(judgment.reason, str) or not judgment.reason):
                    raise PortError("AIPort.judge 返回不符合协议", kind="bad_output")
            except PortError as exc:
                return self._verify_port_error("ai", node, attempt, str(exc), exc.kind)
            seq = self.ledger.append(
                "check", node=node.path, attempt=attempt, rule_index=index, rule_type="llm",
                verdict="pass" if judgment.passed else "fail",
                criterion=self.ledger.log_ref("criterion", criterion), context=context,
                reason=judgment.reason, duration_ms=round((time.monotonic() - started) * 1000),
            )
            return None if judgment.passed else Failure(node.path, "verify", seq, judgment.reason)
        if rule.kind == "human":
            question = self._fill(rule.value, node, artifact, refs, quote=False)
            evidence = self._verify_context(node, refs, artifact)
            human = self.ports.human
            if human is None:
                return self._verify_port_error("human", node, attempt, "未注入 human 端口")
            started = time.monotonic()
            try:
                answer = human.ask(question, evidence)
                if (not isinstance(answer, Answer) or not isinstance(answer.approve, bool)
                        or (not answer.approve and not answer.feedback)):
                    raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
            except PortError as exc:
                return self._verify_port_error("human", node, attempt, str(exc), exc.kind)
            seq = self.ledger.append(
                "check", node=node.path, attempt=attempt, rule_index=index, rule_type="human",
                verdict="pass" if answer.approve else "fail",
                question=self.ledger.log_ref("question", question), evidence=evidence,
                feedback=answer.feedback, duration_ms=round((time.monotonic() - started) * 1000),
            )
            reason = answer.feedback or f"verify human 规则 {index} 被否决"
            return None if answer.approve else Failure(node.path, "verify", seq, reason)
        try:
            bindings = {key: self._typed_value(node.output[key].type, ref) for key, ref in refs.items()}
            result = bool(_evaluate(rule.expression.body, bindings))  # type: ignore[union-attr]
            payload: dict[str, Any] = {
                "rule_index": index, "rule_type": "metric", "verdict": "pass" if result else "fail",
                "expression": rule.value, "bindings": bindings, "result": result,
            }
            seq = self.ledger.append("check", node=node.path, attempt=attempt, **payload)
            if not result:
                return Failure(node.path, "verify", seq, f"verify metric 规则 {index} 为假")
            return None
        except Exception as exc:
            seq = self.ledger.append(
                "check", node=node.path, attempt=attempt, rule_index=index, rule_type="metric",
                verdict="fail", expression=rule.value, bindings={}, result=False, error=str(exc),
            )
            return Failure(node.path, "verify", seq, f"verify metric 规则 {index} 无法求值：{exc}")

    def _verify_context(
        self, node: Node, refs: dict[str, dict[str, Any]], artifact: Path | None
    ) -> dict[str, Any]:
        return {
            "artifact": self.artifacts.describe(str(artifact)) if artifact is not None else None,
            "outputs": {
                key: self._typed_value(node.output[key].type, ref) for key, ref in refs.items()
            },
        }

    def _verify_port_error(
        self, port: str, node: Node, attempt: int, message: str, kind: str = "unavailable"
    ) -> Failure:
        seq = self.ledger.append(
            "port_error", node=node.path, attempt=attempt, port=port, kind=kind, message=message
        )
        raise PortError(message, kind=kind)

    def _shell(
        self, command: str, node: Node, attempt: int, event: str, **extra: Any
    ) -> tuple[ShellResult, int]:
        try:
            if self.ports.check_cancel:
                self.ports.check_cancel()
            if self.ports.on_event:
                self.ports.on_event({"event": "command_started" if event == "call_shell" else "verification_started", "node": node.path, "command": command})
            cwd = self.ports.verification_cwd if event != "call_shell" else None
            result = self.ports.shell.execute(command, cwd or self.ports.cwd or self.graph.package_dir)
            if self.ports.check_cancel:
                self.ports.check_cancel()
        except PortError as exc:
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="shell", kind=exc.kind, message=str(exc),
            )
            raise
        stdout = self.ledger.log_ref("stdout", result.stdout)
        stderr = self.ledger.log_ref("stderr", result.stderr)
        fields = {
            "command": result.command, "rc": result.rc, "stdout": stdout,
            "stderr": stderr, "duration_ms": result.duration_ms, **extra,
        }
        if event == "check":
            fields["verdict"] = "pass" if result.rc == 0 else "fail"
        seq = self.ledger.append(event, node=node.path, attempt=attempt, **fields)
        return result, seq

    def _artifact_path(self, node: Node) -> Path:
        if node.artifact_at:
            return Path(self._fill(node.artifact_at, node, None, quote=False))
        return Path("artifacts") / (
            f"{node.path.replace('.', '--')}{node.artifact_ext or '.out'}"
        )

    def _call_tool(self, node: Node, artifact: Path, attempt: int) -> int:
        """Run a fixed host tool call and materialize its result as the node artifact."""
        port = self.ports.tool
        if port is None:
            self._verify_port_error("tool", node, attempt, "未注入 tool 端口")
        assert port is not None and node.tool is not None
        arguments = self._fill_json(dict(node.tool.args), node, artifact)
        execution_id = f"{node.path}:{attempt}:{uuid4().hex}"
        started = time.monotonic()
        if self.ports.on_event:
            self.ports.on_event({
                "event": "tool_call_started",
                "node": node.path,
                "attempt": attempt,
                "execution_id": execution_id,
                "tool": node.tool.name,
                "arguments": arguments,
            })
        try:
            if self.ports.check_cancel:
                self.ports.check_cancel()
            result = port.call(
                node.tool.name,
                arguments,
                execution_id=execution_id,
            )
            if not isinstance(result, ToolResult):
                raise PortError("ToolPort.call 返回不符合协议", kind="bad_output")
        except PortError as exc:
            if self.ports.on_event:
                self.ports.on_event({
                    "event": "tool_call_finished",
                    "node": node.path,
                    "attempt": attempt,
                    "execution_id": execution_id,
                    "tool": node.tool.name,
                    "state": "unknown",
                    "error": str(exc),
                })
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="tool", kind=exc.kind, message=str(exc),
            )
            raise
        except Exception as exc:
            if self.ports.on_event:
                self.ports.on_event({
                    "event": "tool_call_finished",
                    "node": node.path,
                    "attempt": attempt,
                    "execution_id": execution_id,
                    "tool": node.tool.name,
                    "state": "unknown",
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                })
            raise
        if self.ports.on_event:
            self.ports.on_event({
                "event": "tool_call_finished",
                "node": node.path,
                "attempt": attempt,
                "execution_id": execution_id,
                "tool": node.tool.name,
                "state": "failed" if result.is_error else "completed",
                "receipt": {
                    "is_error": result.is_error,
                    "result": dict(result.result),
                    "text": result.text,
                },
            })
        payload = dict(result.result) or {"text": result.text}
        self.artifacts.write_text(
            str(artifact), json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )
        seq = self.ledger.append(
            "call_tool", node=node.path, attempt=attempt, tool=node.tool.name,
            arguments=arguments, is_error=result.is_error, message=result.text,
            artifact=self.artifacts.describe(str(artifact)),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        # Preserve the proven terminal outcome before cancellation stops the graph.
        # Checking inside the dispatch try would turn a returned result into unknown.
        if self.ports.check_cancel:
            self.ports.check_cancel()
        if result.is_error:
            raise NodeFailed(
                Failure(node.path, "do", seq,
                        result.text or f"工具 {node.tool.name} 返回错误")
            )
        return seq

    def _await_product(self, node: Node, artifact: Path, attempt: int) -> int:
        """Resolve a receipt to the product; park the node while the task runs.

        One probe, never a wait: a pending answer parks the run and the host wakes
        it from the task's own callback. The product lands on the declared artifact,
        so every later node keeps reading one path exactly as with a run leaf.
        """
        port = self.ports.await_port
        if port is None:
            self._verify_port_error("await", node, attempt, "未注入 await 端口")
        assert port is not None and node.await_call is not None
        receipt = self._fill(node.await_call.receipt, node, artifact, quote=False)
        record = self.checkpoint.awaiting(node.path) or {}
        awaited_at = record.get("awaited_at")
        if not isinstance(awaited_at, (int, float)):
            awaited_at = time.time()
        timeout = node.await_call.timeout
        deadline = float(awaited_at) + timeout if timeout else None
        if deadline is not None and time.time() >= deadline:
            raise NodeFailed(
                Failure(node.path, "do", 0, f"等待外部任务超过 {timeout:.0f}s")
            )
        started = time.monotonic()
        try:
            if self.ports.check_cancel:
                self.ports.check_cancel()
            result = port.probe(receipt)
            if not isinstance(result, AwaitResult):
                raise PortError("AwaitPort.probe 返回不符合协议", kind="bad_output")
        except PortError as exc:
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="await", kind=exc.kind, message=str(exc),
            )
            raise
        if result.status == "pending":
            detail = result.detail if isinstance(result.detail, Mapping) else {}
            task_id = detail.get("task_id")
            output_dir = detail.get("output_dir")
            raise NodeAwaiting(
                node.path, receipt, float(awaited_at), deadline,
                task_id=task_id if isinstance(task_id, str) and task_id else None,
                output_dir=(output_dir if isinstance(output_dir, str) else None),
            )
        if result.status == "failed":
            seq = self.ledger.append(
                "call_await", node=node.path, attempt=attempt, status="failed",
                receipt=receipt, error=result.error,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise NodeFailed(
                Failure(node.path, "do", seq, result.error or "外部任务失败")
            )
        if result.product:
            self.artifacts.link(str(artifact), result.product)
        else:
            self.artifacts.write_text(
                str(artifact),
                json.dumps(dict(result.detail), ensure_ascii=False, indent=2) + "\n",
            )
        return self.ledger.append(
            "call_await", node=node.path, attempt=attempt, status="completed",
            receipt=receipt, product=result.product,
            artifact=self.artifacts.describe(str(artifact)),
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _human_work(self, node: Node, artifact: Path, attempt: int) -> int:
        """A human step: the question is the work order, the artifact is their output.

        A pending answer parks the run instead of failing it, so the person can take
        as long as they need; the node is re-entered on resume and asks again.
        """
        human = self.ports.human
        if human is None:
            self._verify_port_error("human", node, attempt, "未注入 human 端口")
        assert human is not None
        question = self._fill(node.do, node, artifact, quote=False)
        evidence = {
            "node": node.path,
            "artifact": self.artifacts.path(str(artifact)),
            "inputs": {key: self._input_slot(node, key) for key in node.input},
            "outputs": sorted(node.output),
        }
        started = time.monotonic()
        try:
            answer = human.ask(question, evidence)
            if (not isinstance(answer, Answer) or not isinstance(answer.approve, bool)
                    or (not answer.approve and not answer.feedback and not answer.pending)):
                raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("human", node, attempt, str(exc), exc.kind)
        seq = self.ledger.append(
            "call_human", node=node.path, attempt=attempt,
            question=self.ledger.log_ref("question", question), evidence=evidence,
            approve=answer.approve, feedback=answer.feedback, pending=answer.pending,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        if answer.pending:
            raise NodeWaiting(node.path, question, evidence["artifact"])
        if not answer.approve:
            raise NodeAbandoned(answer.feedback or "人拒绝人工节点")
        return seq

    def _slot_value(
        self, token: str, node: Node, artifact: Path | None,
        refs: dict[str, dict[str, Any]],
    ) -> Any:
        if token == "artifact":
            if artifact is None:
                raise RuntimeError("{artifact} 仅可用于有产物的叶子")
            return self.artifacts.path(str(artifact))
        scope, key = token.split(".", 1)
        if scope == "tried":
            candidate, field = key.split(".", 1)
            return getattr(self.tried[candidate], field)
        if scope == "input":
            key, _, sub = key.partition(".")
            value = self._input_slot(node, key)
            if not sub:
                return value
            if sub == "dir":
                return str(Path(str(value)).parent)
            raise RuntimeError(f"无效槽位 {token}")
        if scope == "output":
            return self.artifacts.path(refs[key]["path"])
        raise RuntimeError(f"无效槽位 {token}")

    def _fill(
        self, template: str, node: Node, artifact: Path | None,
        refs: dict[str, dict[str, Any]] | None = None, *, quote: bool = True,
    ) -> str:
        refs = refs or self.refs.get(node.path, {})

        def replacement(match: Any) -> str:
            value = self._slot_value(match.group(1), node, artifact, refs)
            return shlex.quote(str(value)) if quote else str(value)

        return _SLOT.sub(replacement, template)

    def _fill_json(self, value: Any, node: Node, artifact: Path | None) -> Any:
        """Resolve a tool call's args: an exact slot keeps its JSON type, the rest interpolate."""
        if isinstance(value, str):
            exact = _EXACT_SLOT.fullmatch(value)
            if exact is not None:
                return self._slot_value(
                    exact.group(1), node, artifact, self.refs.get(node.path, {})
                )
            return self._fill(value, node, artifact, quote=False)
        if isinstance(value, Mapping):
            return {str(key): self._fill_json(item, node, artifact)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._fill_json(item, node, artifact) for item in value]
        return value

    def _input_slot(self, node: Node, key: str) -> Any:
        source = node.input[key].source
        if source.kind == "params":
            return self.checkpoint.params[source.key]
        if source.kind == "ancestor_input":
            return self._input_slot(self.nodes[source.node_path], source.key)
        return self.artifacts.path(self.refs[source.node_path][source.key]["path"])

    def _typed_value(self, type_name: str, ref: dict[str, Any]) -> Any:
        if type_name == "path":
            return self.artifacts.path(ref["path"])
        text = self.artifacts.read_text(ref["path"])
        if type_name == "text":
            return text
        value = json.loads(text)
        if type_name == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("产物不是 number")
        if type_name == "bool" and not isinstance(value, bool):
            raise ValueError("产物不是 bool")
        return value


def _evaluate(node: ast.AST, bindings: dict[str, Any]) -> Any:
    """执行已由 builder 白名单钉死的 AST，避免运行期 eval。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bindings[node.id]
    if isinstance(node, ast.Attribute):
        base = _evaluate(node.value, bindings)
        if isinstance(base, dict):
            return base[node.attr]
        return getattr(base, node.attr)
    if isinstance(node, ast.Subscript):
        return _evaluate(node.value, bindings)[_evaluate(node.slice, bindings)]
    if isinstance(node, ast.Call):
        return len(_evaluate(node.args[0], bindings))
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(value, bindings) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, bindings)
        if isinstance(node.op, ast.Not):
            return not value
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, bindings), _evaluate(node.right, bindings)
        operators = {
            ast.Add: lambda: left + right, ast.Sub: lambda: left - right, ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right, ast.FloorDiv: lambda: left // right, ast.Mod: lambda: left % right,
        }
        return operators[type(node.op)]()
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, bindings)
        comparisons = {
            ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
            ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
            ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
            ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
        }
        for op, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, bindings)
            if not comparisons[type(op)](left, right):
                return False
            left = right
        return True
    raise ValueError(f"未支持的预编译表达式节点：{type(node).__name__}")
