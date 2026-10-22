"""Native Pi execution environment. SDK hosts never select this adapter."""
from __future__ import annotations

import codecs
import hashlib
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from adapters.terminal_human import TerminalHuman
from core.artifacts import LocalArtifacts
from core.ports import Answer, Ports, ShellResult, ShellUnavailable, ToolResult
from .service import RunCancelled


class StreamingShell:
    def __init__(self, cancel: threading.Event, emit: Callable,
                 timeout: float = 1800) -> None:
        self.cancel, self.emit, self.timeout = cancel, emit, timeout

    def execute(self, command: str, cwd: Path) -> ShellResult:
        started = time.monotonic()
        # Each command owns a process group, including shell descendants.
        process = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
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
    """A local host has no platform tools, so a tool node gets an echoed result.

    A local replay can only prove the node is fed the declared arguments and that the
    result lands in the declared artifact; the call itself needs the real port, which
    is why reuse runs on the host that owns the tool.
    """

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(False, {"tool": name, "arguments": arguments}, f"{name} (local)")


class NativeHuman:
    """Pi supplies human interaction; only an explicitly resumed gate is approved."""

    def __init__(self, resuming: str | None = None) -> None:
        self.resuming = resuming

    def ask(self, question: str, evidence: dict) -> Answer:
        if self.resuming is not None and evidence.get("node") == self.resuming:
            return Answer(approve=True)
        return Answer(pending=True)

    def capabilities(self) -> dict:
        return {"interactive": True}


class LocalExecutionHost:
    def __init__(self, root: Path, cwd: Path, human: Any = None) -> None:
        self.root, self.cwd = root, cwd
        self.human = human if human is not None else TerminalHuman()

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
        # A parked run keeps its work directory; resuming re-enters prepare and
        # must reuse the package copy the parked attempt already validated.
        if not scripts.exists():
            shutil.copytree(package, scripts)
        from builder import package_pin
        if package_pin(scripts) != package_pin(package):
            raise ValueError("package changed while copying scripts")
        values = dict(params)
        for key in path_parameters:
            path = Path(values[key]).expanduser()
            values[key] = str((self.cwd / path).resolve())
        human = (NativeHuman(options.get("resuming"))
                 if isinstance(self.human, NativeHuman) else self.human)
        return Ports(shell=StreamingShell(cancel, emit), cwd=scripts,
                     artifacts=NativeArtifacts(directory), tool=LocalTool(),
                     human=human,
                     # Verify rules must run the frozen originals, not the run copy.
                     verification_cwd=verification_package), values

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
