"""CORE 消费的端口协议；实现只存在于 adapters。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Protocol

from .artifacts import ArtifactPort


class PortError(RuntimeError):
    """端口不可用等硬错误；必须上抛，不能转换为图失败。"""

    valid_kinds: ClassVar[frozenset[str]] = frozenset(
        {"unavailable", "timeout", "refused", "bad_output"}
    )
    kind: ClassVar[str] = "unavailable"

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        selected_kind = kind or self.kind
        if selected_kind not in self.valid_kinds:
            raise ValueError(f"未知 PortError kind：{selected_kind}")
        self.kind = selected_kind
        super().__init__(message)


class ShellUnavailable(PortError):
    kind = "unavailable"


@dataclass(frozen=True)
class ShellResult:
    command: str
    rc: int
    stdout: str
    stderr: str
    duration_ms: int


class ShellPort(Protocol):
    def execute(self, command: str, cwd: Path) -> ShellResult: ...


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    reason: str


@dataclass(frozen=True)
class Answer:
    """A human's verdict on one question.

    `pending` is the pause: the person has not answered yet and the run parks at the
    node instead of holding the worker. Everything else is a decision.
    """

    approve: bool = False
    feedback: str | None = None
    batch_approval: bool = False
    batch_reused: bool = False
    pending: bool = False

    def __post_init__(self) -> None:
        if self.pending:
            if self.approve or self.batch_approval or self.batch_reused:
                raise ValueError("待唤醒的答复不能同时批准")
            return
        if not self.approve and not self.feedback:
            raise ValueError("否决必须提供非空 feedback")
        if self.batch_reused and not self.approve:
            raise ValueError("批量批准复用只能返回批准")


@dataclass(frozen=True)
class FailDecl:
    candidate: str
    stage: str
    reason: str
    evidence: list[dict[str, Any]]
    attempt: int


@dataclass(frozen=True)
class Candidate:
    id: str
    hint: str


class AIPort(Protocol):
    def work(self, prompt: str) -> str: ...

    def judge(self, criterion: str, context: dict[str, Any]) -> JudgeResult: ...

    def route(self, failures: list[FailDecl], candidates: list[Candidate]) -> str: ...

    def capabilities(self) -> dict[str, Any]: ...


class HumanPort(Protocol):
    def ask(self, question: str, evidence: dict[str, Any]) -> Answer: ...

    def capabilities(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ToolResult:
    """A host tool call's outcome; `result` is the JSON object the node persists."""

    is_error: bool
    result: Mapping[str, Any]
    text: str = ""


class ToolPort(Protocol):
    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


@dataclass(frozen=True)
class Ports:
    shell: ShellPort
    ai: AIPort | None = None
    human: HumanPort | None = None
    artifacts: ArtifactPort | None = None
    cwd: Path | None = None
    verification_cwd: Path | None = None
    check_cancel: Callable[[], None] | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None
    agent_task: Callable[[dict[str, Any]], str] | None = None
    tool: ToolPort | None = None
