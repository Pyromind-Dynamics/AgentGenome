"""One local execution owner per registry; newline JSON over a private Unix socket.

The extension starts this service on demand. No Pi worker or SDK is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import importlib
import os
import shutil
import stat
import time
from pathlib import Path
from uuid import uuid4

from .dispatch import invoke
from .local_host import LocalExecutionHost, NativeHuman
from .native_stages import NativeStages
from .service import GenomeService

PROTOCOL = 1
VERSION = "0.6.48"
ACTIVE = {"queued", "running", "cancelling"}


def runtime_fingerprint() -> str:
    """Identify Python code, independent of install paths and wheel metadata."""
    digest = hashlib.sha256()
    for name in ("agentgenome", "adapters", "builder", "core"):
        root = Path(importlib.import_module(name).__file__).parent
        for path in sorted(root.rglob("*.py")):
            digest.update(f"{name}/{path.relative_to(root).as_posix()}\0".encode())
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def socket_path(root: Path) -> Path:
    # Unix socket paths have a small OS limit; do not place them under long workspaces.
    directory = Path(f"/tmp/agentgenome-{os.getuid()}")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("AgentGenome socket directory must be owned by you with mode 0700")
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
    return directory / f"{digest}.sock"


class LocalService:
    def __init__(self, root: Path, idle_seconds: float = 60) -> None:
        # Freeze the identity at process startup, not after files are replaced.
        self.runtime_fingerprint = runtime_fingerprint()
        self.service = GenomeService(root)
        self.stages = NativeStages(self.service)
        self.root = self.service.root
        self.idle_seconds = idle_seconds
        self.jobs: dict[str, asyncio.Task] = {}
        self.clients: set[asyncio.StreamWriter] = set()
        self.client_scopes: dict[str, str] = {}
        self.stage_clients: set[str] = set()
        self.last_used = time.monotonic()
        with self.service._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_runs (
                run_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                acknowledged INTEGER NOT NULL DEFAULT 0, claimant TEXT, lease_until REAL)''')

    def records(self, scope: str) -> list[dict]:
        with self.service._db() as db:
            rows = db.execute('''SELECT r.*, n.progress FROM runs r JOIN native_runs n ON r.id=n.run_id
                WHERE r.scope=? AND n.acknowledged=0 ORDER BY r.rowid''', (scope,)).fetchall()
        return [{**self.service._run(row), "progress": json.loads(row["progress"])} for row in rows]

    def reclaim_draft(self, revision_id: str) -> None:
        shutil.rmtree(self.root / "revision-drafts" / revision_id, ignore_errors=True)

    def reclaim_submitted_drafts(self) -> None:
        """Drop editable copies whose revision was submitted or never registered."""
        directory = self.root / "revision-drafts"
        if not directory.is_dir():
            return
        with self.service._db() as db:
            submitted = {row[0] for row in db.execute("SELECT id FROM revisions WHERE child IS NOT NULL")}
            known = {row[0] for row in db.execute("SELECT id FROM revisions")}
        for path in directory.iterdir():
            if path.name not in known or path.name in submitted:
                shutil.rmtree(path, ignore_errors=True)

    def progress(self, run_id: str, event: dict) -> None:
        with self.service._db() as db:
            row = db.execute("SELECT progress FROM native_runs WHERE run_id=?", (run_id,)).fetchone()
            state = json.loads(row[0])
            node = event.get("node", state.get("node"))
            if node != state.get("node"):
                state = {"node": node}
            state["event"] = event["event"]
            if "command" in event:
                state["command"] = event["command"]
            if event["event"] == "command_started":
                state.pop("verdict", None)
            if "verdict" in event:
                state["verdict"] = event["verdict"]
            if event["event"] == "output":
                state["tail"] = (state.get("tail", "") + event.get("text", ""))[-8000:]
            db.execute("UPDATE native_runs SET progress=? WHERE run_id=?",
                       (json.dumps(state), run_id))

    async def execute(self, scope: str, run_id: str, cwd: str, connection_id: str | None = None) -> None:
        loop = asyncio.get_running_loop()

        def deliverable():
            # Scope names survive reopening a session; connection IDs do not.
            return (connection_id in self.stage_clients
                    and self.client_scopes.get(connection_id) == scope)

        async def handoff(request):
            if not deliverable():
                return {"status": "interrupted"}
            return await self.stages.wait(scope, request, deliverable=deliverable)

        def emit(event):
            self.progress(run_id, event)
            if event["event"] == "waiting_agent":
                return asyncio.run_coroutine_threadsafe(
                    handoff(event["request"]), loop).result()

        try:
            await asyncio.to_thread(self.service.execute, scope, run_id,
                                    LocalExecutionHost(self.root, Path(cwd), human=NativeHuman()),
                                    emit)
        finally:
            self.jobs.pop(run_id, None)
            self.last_used = time.monotonic()

    async def dispatch(self, scope: str, cwd: str, message: dict, connection_id: str | None = None) -> dict:
        action, args = message["action"], message.get("arguments", {})
        # The local host can always stage and collect revision files on this machine.
        capabilities = {"revision"}
        if connection_id in self.stage_clients:
            capabilities.add("agent_task")
        request_id = message.get("request_id", message["id"])
        if action in {"run", "validate", "capture", "distill", "prepare_revision",
                      "prepare_run", "confirm_run", "confirm_distill"}:
            host = LocalExecutionHost(self.root, Path(cwd))
            if action == "validate":
                self.service.check_capabilities(args["asset_id"], args["version"], capabilities)
                result = self.service.submit(scope, request_id, args["asset_id"], args["version"],
                                             args["params"], allow_draft=True)
            else:
                result = invoke(self.service, scope, action, args, request_id,
                                host=host, capabilities=capabilities)
                if action == "run" and "revision_id" in args:
                    # Submission collected the allowlisted files; reclaim the copy.
                    self.reclaim_draft(args["revision_id"])
            # A capture or distill reuses an existing version without queueing a run;
            # a confirmed plan answers with the run it just queued.
            if action in {"capture", "distill", "confirm_distill"}:
                run_id = result.get("run_id")
            elif action == "confirm_run":
                run_id = (result.get("run") or {}).get("id")
            else:
                run_id = result.get("id")
            if run_id:
                with self.service._db() as db:
                    db.execute("INSERT OR IGNORE INTO native_runs(run_id,cwd) VALUES(?,?)",
                               (run_id, cwd))
            return result
        if action == "start":
            run_id = args["run_id"]
            state = self.service.status(scope, run_id)
            if state["status"] == "queued" and run_id not in self.jobs:
                with self.service._db() as db:
                    row = db.execute("SELECT cwd FROM native_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise ValueError("not a native run")
                self.jobs[run_id] = asyncio.create_task(self.execute(scope, run_id, row[0], connection_id))
            return state
        if action == "snapshot":
            return {"runs": self.records(scope), "stages": self.stages.snapshot(scope)}
        if action in {"stage_claim", "stage_begin", "stage_finish", "step_result"}:
            if "agent_task" not in capabilities:
                raise ValueError("native stages require a stage-capable Pi client")
            if action == "stage_claim":
                return self.stages.claim(scope, args["request_id"], connection_id)
            method = {"stage_begin": "begin", "stage_finish": "finish", "step_result": "receipt"}[action]
            return getattr(self.stages, method)(scope, args, connection_id)
        if action in {"claim", "ack", "renew", "release"}:
            run_id = args["run_id"]
            state = self.service.status(scope, run_id)
            if state["status"] in ACTIVE:
                raise ValueError("run has not finished")
            with self.service._db() as db:
                if action == "release":
                    db.execute("UPDATE native_runs SET claimant=NULL,lease_until=NULL WHERE run_id=? AND claimant=?",
                               (run_id, args.get("token")))
                    return {"released": True}
                if action == "renew":
                    db.execute("UPDATE native_runs SET lease_until=? WHERE run_id=? AND claimant=? AND acknowledged=0",
                               (time.time() + 60, run_id, args.get("token")))
                    return {"renewed": True}
                if action == "claim":
                    token = uuid4().hex
                    changed = db.execute('''UPDATE native_runs SET claimant=?,lease_until=?
                        WHERE run_id=? AND acknowledged=0 AND (lease_until IS NULL OR lease_until<?)''',
                        (token, time.time() + 60, run_id, time.time())).rowcount
                    return {"token": token if changed else None}
                # A persisted Pi receipt is sufficient after a crash between delivery and ack.
                if args.get("receipt") is True:
                    db.execute("UPDATE native_runs SET acknowledged=1 WHERE run_id=?", (run_id,))
                else:
                    db.execute("UPDATE native_runs SET acknowledged=1 WHERE run_id=? AND claimant=?",
                               (run_id, args.get("token")))
            return {"acknowledged": True}
        if action == "import":
            return self.service.import_asset(Path(cwd) / args["source"])
        if action == "publish":
            return self.service.publish(args["asset_id"], args["version"])
        return invoke(self.service, scope, action, args, request_id, capabilities=capabilities)

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        connection_id = uuid4().hex
        scope = cwd = None
        try:
            while line := await reader.readline():
                self.last_used = time.monotonic()
                message = {}
                try:
                    message = json.loads(line)
                    if message.get("action") == "hello":
                        args = message["arguments"]
                        if args.get("protocol") != PROTOCOL or args.get("version") != VERSION:
                            raise ValueError("AgentGenome service version mismatch; finish existing runs before upgrading")
                        if args.get("runtime_fingerprint", self.runtime_fingerprint) != self.runtime_fingerprint:
                            raise ValueError("AgentGenome service build mismatch; finish existing runs and close all Pi clients before upgrading")
                        if scope is not None:
                            raise ValueError("connection already belongs to a session")
                        if not isinstance(args.get("scope"), str) or not args["scope"] or not Path(args["cwd"]).is_absolute():
                            raise ValueError("hello requires session scope and absolute cwd")
                        scope, cwd = args["scope"], args["cwd"]
                        self.client_scopes[connection_id] = scope
                        if args.get("stage_protocol") == 1:
                            self.stage_clients.add(connection_id)
                        result = {"protocol": PROTOCOL, "version": VERSION,
                                  "runtime_fingerprint": self.runtime_fingerprint}
                    elif scope is None:
                        raise ValueError("hello required")
                    else:
                        result = await self.dispatch(scope, cwd, message, connection_id)
                    response = {"id": message["id"], "result": result}
                except Exception as exc:
                    response = {"id": message.get("id"), "error": str(exc)}
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ConnectionError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(writer)
            self.client_scopes.pop(connection_id, None)
            self.stage_clients.discard(connection_id)
            remaining = [s for key, s in self.client_scopes.items() if key in self.stage_clients]
            abandoned = scope if scope not in remaining else None
            self.stages.disconnect(connection_id, abandoned_scope=abandoned)
            self.last_used = time.monotonic()
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def serve(self) -> None:
        # Hold the existing execution lock *before* removing stale sockets or recovering runs.
        self.service.claim_execution_owner()
        path = socket_path(self.root)
        try:
            path.unlink(missing_ok=True)
            self.service.recover()
            self.reclaim_submitted_drafts()
            server = await asyncio.start_unix_server(self.client, path=path, limit=1024 * 1024)
            path.chmod(0o600)
            async with server:
                while self.clients or self.jobs or time.monotonic() - self.last_used < self.idle_seconds:
                    await asyncio.sleep(min(1, self.idle_seconds))
        finally:
            path.unlink(missing_ok=True)
            self.service.release_execution_owner()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=60)
    args = parser.parse_args()
    if args.idle_seconds <= 0:
        parser.error("idle-seconds must be positive")
    asyncio.run(LocalService(args.home, args.idle_seconds).serve())


if __name__ == "__main__":
    main()
