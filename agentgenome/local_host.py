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
from typing import Callable

from core.artifacts import LocalArtifacts
from core.ports import Ports, ShellResult, ShellUnavailable
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


class LocalExecutionHost:
    def __init__(self, root: Path, cwd: Path) -> None:
        self.root, self.cwd = root, cwd

    def prepare(self, run_id, package, params, path_parameters, cancel, emit):
        directory = self.root / "runs" / run_id / "work"
        scripts = directory / "package"
        shutil.copytree(package, scripts)
        from builder import package_pin
        if package_pin(scripts) != package_pin(package):
            raise ValueError("package changed while copying scripts")
        values = dict(params)
        for key in path_parameters:
            path = Path(values[key]).expanduser()
            values[key] = str((self.cwd / path).resolve())
        return Ports(shell=StreamingShell(cancel, emit), cwd=scripts,
                     artifacts=NativeArtifacts(directory)), values
