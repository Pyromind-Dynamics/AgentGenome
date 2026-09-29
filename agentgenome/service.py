from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from builder import compile, package_pin
from core.api import handshake, run
from core.ports import Ports


class RunCancelled(Exception):
    """The host confirmed that execution has stopped."""


class Host(Protocol):
    def prepare(self, run_id: str, package: Path, params: dict[str, Any],
                path_parameters: list[str], cancel: threading.Event,
                emit: Callable[[dict[str, Any]], None]) -> tuple[Ports, dict[str, Any]]: ...


_ACTIVE = ("queued", "running", "cancelling")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class GenomeService:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._signals: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._owner = None
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT, version TEXT, pin TEXT NOT NULL, manifest TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0,
                    publication INTEGER,
                    PRIMARY KEY(id, version));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_id TEXT NOT NULL,
                    request TEXT NOT NULL, asset_id TEXT NOT NULL, version TEXT NOT NULL,
                    pin TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL,
                    node TEXT, result TEXT, UNIQUE(scope, request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS active_scope ON runs(scope)
                    WHERE status IN ('queued','running','cancelling');
            ''')

    def claim_execution_owner(self) -> None:
        if os.name != "posix":
            raise RuntimeError("v1 execution hosting requires Linux or macOS")
        import fcntl

        owner = (self.root / ".execution-owner").open("a")
        try:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner.close()
            raise RuntimeError("This asset registry already has an execution host") from None
        self._owner = owner

    def release_execution_owner(self) -> None:
        if self._owner:
            self._owner.close()
            self._owner = None

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.root / "state.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        """Single owner startup: never replay commands whose effects are unknown."""
        with self._db() as db:
            db.execute("UPDATE runs SET status='interrupted', result=? "
                       "WHERE status IN ('queued','running','cancelling')",
                       (_json({"error": "Host restarted; execution effects may be incomplete. No automatic replay."}),))

    def import_asset(self, source: str | Path) -> dict[str, Any]:
        source = Path(source).resolve()
        manifest = json.loads((source / "manifest.json").read_text())
        for key in ("id", "version"):
            if not isinstance(manifest.get(key), str) or not _NAME.fullmatch(manifest[key]):
                raise ValueError(f"invalid asset {key}")
        for key in ("name", "description"):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise ValueError(f"missing asset {key}")
        graph = compile(source)
        self._fixed_graph(graph.node)
        inputs = manifest.get("parameters", {})
        if set(inputs) != set(graph.params):
            raise ValueError("manifest parameters must match graph parameters")
        if any(p.get("type") not in {"path", "string", "number", "boolean"} for p in inputs.values()):
            raise ValueError("unsupported parameter type")
        destination = self.root / "assets" / manifest["id"] / manifest["version"]
        pin = package_pin(source)
        with self._lock:
            if destination.exists():
                if package_pin(destination) != pin:
                    raise ValueError("asset version already contains different bytes")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging = destination.with_name(f".import-{uuid4().hex}")
                try:
                    shutil.copytree(source, staging)
                    if package_pin(staging) != pin:
                        raise ValueError("asset changed during import")
                    staging.rename(destination)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            with self._db() as db:
                db.execute("INSERT OR IGNORE INTO assets(id,version,pin,manifest) VALUES(?,?,?,?)",
                           (manifest["id"], manifest["version"], pin, _json(manifest)))
        return self.get(manifest["id"], manifest["version"])

    @staticmethod
    def _fixed_graph(node: Any) -> None:
        if node.needs - {"shell"} or node.mode == "try":
            raise ValueError("v1 supports fixed script sequences only")
        for child in node.children:
            GenomeService._fixed_graph(child)

    def list(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT a.* FROM assets a WHERE published=1 AND publication="
                              "(SELECT MAX(publication) FROM assets b WHERE b.id=a.id AND b.published=1) ORDER BY id").fetchall()
        return [{k: self._asset(row)[k] for k in ("id", "version", "name", "description")} for row in rows]

    @staticmethod
    def _asset(row: sqlite3.Row) -> dict[str, Any]:
        return {**json.loads(row["manifest"]), "pin": row["pin"], "published": bool(row["published"])}

    def get(self, asset_id: str, version: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM assets WHERE id=? AND version=?", (asset_id, version)).fetchone()
        if row is None:
            raise ValueError("asset version not found")
        return self._asset(row)

    def publish(self, asset_id: str, version: str) -> dict[str, Any]:
        asset = self.get(asset_id, version)
        if package_pin(self.root / "assets" / asset_id / version) != asset["pin"]:
            raise ValueError("frozen package has changed")
        with self._db() as db:
            evidence = db.execute("SELECT 1 FROM runs WHERE asset_id=? AND version=? AND pin=? AND status='succeeded'",
                                  (asset_id, version, asset["pin"])).fetchone()
            if evidence is None:
                raise ValueError("publication requires a successful validation run")
            db.execute("UPDATE assets SET published=1, publication=(SELECT COALESCE(MAX(publication),0)+1 FROM assets) "
                       "WHERE id=? AND version=? AND published=0", (asset_id, version))
        return self.get(asset_id, version)

    def submit(self, scope: str, request_id: str, asset_id: str, version: str,
               params: dict[str, Any], *, allow_draft: bool = False) -> dict[str, Any]:
        if not scope or not request_id:
            raise ValueError("scope and request id are required")
        request = _json({"asset_id": asset_id, "version": version, "params": params})
        with self._lock, self._db() as db:
            previous = db.execute("SELECT * FROM runs WHERE scope=? AND request_id=?", (scope, request_id)).fetchone()
            if previous:
                if previous["request"] != request:
                    raise ValueError("request id reused with different parameters")
                return self._run(previous)
            asset = self.get(asset_id, version)
            if not asset["published"] and not allow_draft:
                raise ValueError("asset is not published")
            specs = asset["parameters"]
            if set(params) != set(specs):
                raise ValueError("parameters do not match the asset inputs")
            for key, spec in specs.items():
                value = params[key]
                valid = {"path": isinstance(value, str) and bool(value),
                         "string": isinstance(value, str),
                         "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                         "boolean": isinstance(value, bool)}[spec["type"]]
                if not valid:
                    raise ValueError(f"invalid parameter: {key}")
            if package_pin(self.root / "assets" / asset_id / version) != asset["pin"]:
                raise ValueError("frozen package has changed")
            run_id = uuid4().hex
            try:
                db.execute("INSERT INTO runs(id,scope,request_id,request,asset_id,version,pin,params,status) VALUES(?,?,?,?,?,?,?,?,?)",
                           (run_id, scope, request_id, request, asset_id, version, asset["pin"], _json(params), "queued"))
            except sqlite3.IntegrityError as exc:
                raise ValueError("this conversation already has an active graph") from exc
        return self.status(scope, run_id)

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return {key: json.loads(row[key]) if key in {"params", "result"} and row[key] else row[key]
                for key in ("id", "asset_id", "version", "pin", "params", "status", "node", "result")}

    def status(self, scope: str, run_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND scope=?", (run_id, scope)).fetchone()
        if row is None:
            raise ValueError("run not found in this conversation")
        return self._run(row)

    def cancel(self, scope: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.status(scope, run_id)
            if state["status"] in _ACTIVE:
                self._signals.setdefault(run_id, threading.Event()).set()
                with self._db() as db:
                    db.execute("UPDATE runs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END "
                               "WHERE id=? AND status IN ('queued','running')", (run_id,))
        return self.status(scope, run_id)

    def execute(self, scope: str, run_id: str, host: Host,
                emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            state = self.status(scope, run_id)
            if state["status"] != "queued":
                return state
            cancel = self._signals.setdefault(run_id, threading.Event())
            with self._db() as db:
                changed = db.execute("UPDATE runs SET status='running' WHERE id=? AND status='queued'", (run_id,)).rowcount
            if not changed:
                return self.status(scope, run_id)
        def check_cancel() -> None:
            if cancel.is_set():
                raise RunCancelled()
        def event(payload: dict[str, Any]) -> None:
            if payload.get("event") == "output":
                log = self.root / "runs" / run_id / "execution.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(payload.get("text", ""))
            if payload.get("node"):
                with self._db() as db:
                    db.execute("UPDATE runs SET node=? WHERE id=?", (payload["node"], run_id))
            emit(payload)
        try:
            check_cancel()
            package = self.root / "assets" / state["asset_id"] / state["version"]
            graph = compile(package)
            if graph.pin != state["pin"]:
                raise ValueError("frozen package has changed")
            self._fixed_graph(graph.node)
            asset = self.get(state["asset_id"], state["version"])
            paths = [key for key, spec in asset["parameters"].items() if spec["type"] == "path"]
            ports, params = host.prepare(run_id, package, state["params"], paths, cancel, event)
            if package_pin(package) != state["pin"]:
                raise ValueError("package changed while preparing execution")
            ports = replace(ports, check_cancel=check_cancel, on_event=event)
            if handshake(graph, ports):
                raise ValueError("unsupported graph capabilities")
            result = run(graph, params, ports, self.root / "runs" / run_id)
            check_cancel()
            status = "succeeded" if result.status == "succeeded" else "failed"
            details = {"outputs": result.outputs, "failure": result.failure, "abandonment": result.abandonment}
        except RunCancelled:
            status, details = "cancelled", {}
        except Exception as exc:
            status, details = "interrupted", {"error": str(exc)}
        with self._lock, self._db() as db:
            db.execute("UPDATE runs SET status=?, result=? WHERE id=?", (status, _json(details), run_id))
            self._signals.pop(run_id, None)
        return self.status(scope, run_id)
