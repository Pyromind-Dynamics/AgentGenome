"""Native Pi execution environment. SDK hosts never select this adapter."""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from uuid import uuid4
from pathlib import Path
from typing import Any, Callable

from adapters.terminal_human import TerminalHuman
from core.artifacts import LocalArtifacts
from core.ports import (
    Answer,
    AwaitResult,
    Ports,
    ShellResult,
    ShellUnavailable,
    ToolCancellation,
    ToolResult,
)
from .service import RunCancelled
from .judge import read_small


class StreamingShell:
    def __init__(self, cancel: threading.Event, emit: Callable,
                 timeout: float = 1800, judge=None, judge_cwd=None, judge_root=None) -> None:
        self.cancel, self.emit, self.timeout = cancel, emit, timeout
        self.judge, self.judge_cwd, self.judge_root = judge, judge_cwd, judge_root

    def execute(self, command: str, cwd: Path) -> ShellResult:
        started = time.monotonic()
        judge_dir = None
        env = dict(os.environ)
        env.pop('AGENTGENOME_JUDGE_DIR', None)
        if self.judge is not None and Path(cwd).resolve() == Path(self.judge_cwd).resolve():
            judge_dir = self.judge_root / uuid4().hex
            judge_dir.mkdir(parents=True)
            env['AGENTGENOME_JUDGE_DIR'] = str(judge_dir)
        # Each command owns a process group, including shell descendants.
        process = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=env)
        tails = {"stdout": "", "stderr": ""}
        decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in tails}
        try:
            with selectors.DefaultSelector() as selector:
                for name in tails:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    if self.cancel.is_set():
                        raise RunCancelled()
                    if time.monotonic() - started > self.timeout:
                        raise ShellUnavailable("Local command exceeded timeout", kind="timeout")
                    if (judge_dir is not None and (judge_dir / 'request.json').exists()
                            and not (judge_dir / 'response.ready').exists()):
                        self._serve_judge(judge_dir)
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fd, 8192)
                        text = decoders[key.data].decode(chunk, final=not chunk)
                        if text:
                            self.emit({"event": "output", "stream": key.data, "text": text})
                            tails[key.data] = (tails[key.data] + text)[-65536:]
                        if not chunk:
                            selector.unregister(key.fileobj)
            return ShellResult(command, process.wait(), tails["stdout"], tails["stderr"],
                               round((time.monotonic() - started) * 1000))
        finally:
            # Also stop detached-in-shell children that outlive the shell itself.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()

    def _serve_judge(self, directory):
        request_id = None
        try:
            request = json.loads(read_small(directory / 'request.json'))
            if (not isinstance(request, dict) or not isinstance(request.get('request_id'), str)
                    or not request['request_id'] or len(request['request_id']) > 128
                    or not isinstance(request.get('criterion'), str)
                    or not isinstance(request.get('evidence'), str)):
                raise ValueError('invalid judge request')
            request_id = request['request_id']
            response = self.judge({key: request[key] for key in ('request_id', 'criterion', 'evidence')})
        except Exception as exc:
            response = {'error': str(exc)[:2000]}
        response = {**response, 'request_id': request_id}
        temporary = directory / 'response.tmp'
        temporary.write_text(json.dumps(response), encoding='utf-8')
        temporary.replace(directory / 'response.json')
        (directory / 'response.ready').write_text('ready')


class NativeArtifacts(LocalArtifacts):
    def describe(self, name: str) -> dict:
        path = Path(self.path(name))
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return {"path": name, "bytes": size, "sha256": digest.hexdigest(), "execution_path": str(path)}

    def read_text(self, name: str) -> str:
        with Path(self.path(name)).open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError("Metric input exceeds 64 KiB; use script verification for large artifacts")
        return data.decode("utf-8")


class LocalTool:
    """Forward to the owning Pi connection; never synthesize a successful echo."""

    def __init__(self, call, cancel):
        self._call, self._cancel = call, cancel

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        execution_id: str,
    ) -> ToolResult:
        return self._call(name, arguments, execution_id)

    def cancel(self, execution_id: str) -> ToolCancellation:
        self._cancel(execution_id)
        return ToolCancellation(False, 'Waiting for the actual Pi tool to stop and return a result')


class LocalAwait:
    """Resolve a receipt against a local filesystem: the CLI/debug counterpart.

    The SDK host probes the platform's Storage; a local run reads the same
    ``report.json`` contract from disk. Both answer in one shot and never wait.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def probe(self, receipt_path: str) -> AwaitResult:
        import json

        path = Path(receipt_path).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return AwaitResult("failed", error=f"invalid local task receipt: {exc}")
        if not isinstance(receipt, dict):
            return AwaitResult("failed", error="local task receipt must be an object")
        directory = receipt.get("output_dir")
        if not isinstance(directory, str) or not directory.strip():
            return AwaitResult("failed", error="local task receipt requires output_dir")
        target = Path(directory).expanduser()
        candidates = [target if target.is_absolute() else self.cwd / target]
        report = None
        found = None
        for candidate in candidates:
            try:
                report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found = candidate
            break
        if not isinstance(report, dict) or found is None:
            return AwaitResult("pending", detail={
                "output_dir": directory, "task_id": receipt.get("task_id")})
        status = str(report.get("status") or "").lower()
        if status in {"failed", "cancelled", "canceled", "error"}:
            return AwaitResult("failed", error=f"platform run {status}", detail=report)
        if status != "succeeded":
            return AwaitResult("pending", detail={
                "output_dir": str(found), "status": status,
                "task_id": receipt.get("task_id")})
        product = found / "processed.jsonl"
        if not product.is_file():
            return AwaitResult("pending", detail={
                "output_dir": str(found), "task_id": receipt.get("task_id"),
                "status": "awaiting_product"})
        return AwaitResult(
            "completed",
            product=str(product),
            detail={"output_dir": str(found), "status": status},
        )


class NativeHuman:
    """Pi supplies human interaction; only an explicitly resumed gate is approved."""

    def __init__(self, resuming: str | None = None, artifact: Path | None = None) -> None:
        self.resuming = resuming
        self.artifact = artifact

    def ask(self, question: str, evidence: dict) -> Answer:
        if self.resuming is not None and evidence.get("node") == self.resuming:
            if self.artifact is not None:
                target = Path(evidence["artifact"])
                target.parent.mkdir(parents=True, exist_ok=True)
                if self.artifact.resolve() != target.resolve():
                    shutil.copyfile(self.artifact, target)
            return Answer(approve=True)
        return Answer(pending=True)

    def capabilities(self) -> dict:
        return {"interactive": True}


class LocalExecutionHost:
    def __init__(self, root: Path, cwd: Path, human: Any = None, judge=None, tool=None) -> None:
        self.root, self.cwd = root, cwd
        self.human = human if human is not None else TerminalHuman()
        self.judge = judge
        self.tool = tool

    def _target(self, path: str) -> Path:
        target = Path(path).expanduser()
        return target if target.is_absolute() else self.cwd / target

    def read(self, path: str) -> bytes:
        return self._target(path).read_bytes()

    def head(self, path: str, limit: int) -> bytes:
        with self._target(path).open("rb") as stream:
            return stream.read(limit)

    def prepare(self, run_id, package, params, path_parameters, cancel, emit,
                verification_package=None, **options):
        # A local run asks its terminal human again on resume; `resuming` only tells
        # a headless host which parked node the wakeup answers.
        directory = self.root / "runs" / run_id / "work"
        scripts = directory / "package"
        emit({
            "event": "execution_prepared",
            "execution": {
                "execution_domain": "local",
                "execution_path": str(scripts),
                "workspace": str(self.cwd),
                "package": str(package),
                "artifacts_dir": str(directory),
            },
        })
        # Preserve generated working files across human gates. Only the packaged
        # files are immutable on resume; new runtime files are not package edits.
        from builder import package_pin
        if not scripts.exists():
            shutil.copytree(package, scripts)
            if package_pin(scripts) != package_pin(package):
                raise ValueError("package changed while copying scripts")
        else:
            for original in Path(package).rglob("*"):
                relative = original.relative_to(package)
                if ("__pycache__" in relative.parts or
                        original.name.endswith(".pyc") or original.name == ".DS_Store"):
                    continue
                target = scripts / relative
                if (target.is_symlink() or
                        (original.is_dir() and not target.is_dir()) or
                        (original.is_file() and
                         (not target.is_file() or target.read_bytes() != original.read_bytes()))):
                    raise ValueError(f"package changed while resuming: {relative}")
        values = dict(params)
        for key in path_parameters:
            path = Path(values[key]).expanduser()
            values[key] = str((self.cwd / path).resolve())
        exported = options.get("human_artifact")
        human = (NativeHuman(options.get("resuming"),
                             self._target(exported) if exported else None)
                 if isinstance(self.human, NativeHuman) else self.human)
        judge = self.judge if 'model_judge' in json.loads((package / 'manifest.json').read_text()).get('requires', []) else None
        verification_source = verification_package or (package if judge is not None else None)
        verification = None
        if verification_source is not None:
            verification = directory / 'verification'
            source_pin = package_pin(verification_source)
            # Verifiers may write scratch files. Rebuild on resume instead of
            # treating those files as mutations of the frozen verification source.
            if verification.exists():
                shutil.rmtree(verification)
            shutil.copytree(verification_source, verification)
            if package_pin(verification) != source_pin:
                raise ValueError('verification package changed while copying')
        return Ports(shell=StreamingShell(cancel, emit, judge=judge,
                                         judge_cwd=verification, judge_root=directory / '.judge'), cwd=scripts,
                     artifacts=NativeArtifacts(directory), tool=self.tool,
                     human=human,
                     # Use a separate copy of the original standards, never execute
                     # in the asset store or against editable business resources.
                     verification_cwd=verification,
                     await_port=LocalAwait(self.cwd)), values

    def prepare_revision(self, revision_id, package, allowed):
        """Copy the frozen package for the agent to edit in place.

        There is no OS-level protection: the full copy is context, the prompt
        carries the constraint, and submission still collects only the
        allowlisted business scripts.
        """
        directory = self.root / "revision-drafts" / revision_id
        if directory.exists():
            raise ValueError("revision draft already exists")
        shutil.copytree(package, directory)
        for name in allowed:
            file = directory / name
            if not file.is_file() or file.is_symlink():
                raise ValueError("editable script is not a regular package file")
        return {"editable_dir": str(directory), "verification_dir": str(directory),
                "verification_protection": "prompt"}

    def collect_revision(self, draft, allowed):
        directory = Path(draft["editable_dir"])
        files = {}
        for name in allowed:
            file = directory / name
            if not file.is_file() or file.is_symlink():
                raise ValueError("editable script is not a regular package file")
            files[name] = file.read_bytes()
        return files
