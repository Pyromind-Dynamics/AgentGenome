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
from core.api import handshake, resume as resume_run, run
from core.ports import Ports, PortError
from .captures import Captures
from .manifest import validate as validate_manifest
from .manifest import view as manifest_view
from .parameters import PARAMETER_TYPES, ParameterError, normalize_params, parameter_spec
from .plans import DistillPlans, RunPlans
from .receipts import ToolReceipts
from .stages import StageRequests
from .revisions import Revisions, AWAIT_INSTRUCTION, RECOVERY_INSTRUCTION, WAITING_INSTRUCTION
from .regressions import Regressions, RegressionFailure


class RunCancelled(Exception):
    """The host confirmed that execution has stopped."""


class Host(Protocol):
    def prepare(self, run_id: str, package: Path, params: dict[str, Any],
                path_parameters: list[str], cancel: threading.Event,
                emit: Callable[[dict[str, Any]], None], **options: Any
                ) -> tuple[Ports, dict[str, Any]]:
        """Build the ports for one attempt.

        `options["resuming"]` names the human node a wakeup already answered, so a
        host that parks human nodes can approve exactly that node and park the rest.
        `options["human_artifact"]` is the file the person produced outside the run
        directory; a host that parks human nodes lands it on the declared artifact
        when it approves that node.
        """

    def read(self, path: str) -> bytes:
        """Read a workspace file: exported traces and captured scripts."""


# A parked run holds no worker: `waiting` is where a human gate leaves it,
# `waiting_external` is where an asynchronous task's receipt leaves it, and
# `resume` is what puts either back in the queue.
_ACTIVE = ("queued", "running", "cancelling")
_PARKED = ("waiting", "waiting_external")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class GenomeService:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._signals: dict[str, threading.Event] = {}
        self._tool_ports: dict[str, Any] = {}
        self._active_tools: dict[str, set[str]] = {}
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            if "handoff" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN handoff TEXT")

        self.stages = StageRequests(self)
        self.regressions = Regressions(self)
        self.revisions = Revisions(self)
        self.captures = Captures(self)
        self.tool_receipts = ToolReceipts(self)
        self.plans = RunPlans(self)
        self.distill_plans = DistillPlans(self)

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
        self.stages.recover()
        self.tool_receipts.recover()
        error = _json({"error": "Host restarted; execution effects may be incomplete. "
                               "No automatic replay."})
        with self._db() as db:
            stopped = [row["id"] for row in db.execute(
                "SELECT id FROM runs WHERE status IN ('queued','running','cancelling')").fetchall()]
            db.execute("UPDATE runs SET status='interrupted', result=? "
                       "WHERE status IN ('queued','running','cancelling')", (error,))
        for run_id in stopped:
            capture = self.captures.for_run(run_id)
            if capture:
                # A restart is a verdict too: the capture must not sit in "replaying" forever.
                self.captures.finish(capture["capture_id"], status="interrupted",
                                     result=json.loads(error), host=None, emit=None)

    def import_asset(self, source: str | Path) -> dict[str, Any]:
        source = Path(source).resolve()
        manifest = json.loads((source / "manifest.json").read_text())
        for key in ("id", "version"):
            if not isinstance(manifest.get(key), str) or not _NAME.fullmatch(manifest[key]):
                raise ValueError(f"invalid asset {key}")
        for key in ("name", "description"):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise ValueError(f"missing asset {key}")
        validate_manifest(manifest)
        graph = compile(source)
        self._supported_graph(graph.node)
        inputs = manifest.get("parameters", {})
        if set(inputs) != set(graph.params):
            raise ValueError("manifest parameters must match graph parameters")
        if any(p.get("type") not in PARAMETER_TYPES for p in inputs.values()):
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
    def _supported_graph(node: Any) -> None:
        if (node.needs - {"shell", "llm", "human", "tool", "await"} or node.mode == "try"
                or any(rule.kind not in {"run", "metric", "human", "llm"}
                       for rule in node.verify)):
            raise ValueError("supports fixed script sequences and agent tasks only")
        if "llm" in node.needs and node.do_kind != "llm" and not any("llm" in child.needs for child in node.children):
            raise ValueError("fixed script node declares unsupported llm requirement")
        for child in node.children:
            GenomeService._supported_graph(child)

    @staticmethod
    def _requires_agent(node: Any) -> bool:
        return node.do_kind == "llm" or any(GenomeService._requires_agent(child) for child in node.children)

    @staticmethod
    def _requires_tool(node: Any) -> bool:
        return "tool" in node.needs or any(
            GenomeService._requires_tool(child) for child in node.children
        )

    def check_capabilities(self, asset_id: str, version: str, capabilities: set[str]) -> None:
        graph = compile(self.root / "assets" / asset_id / version)
        asset = self.get(asset_id, version)
        required = set(asset.get("requires", []))
        if self._requires_agent(graph.node):
            required.add("agent_task")
        if self._requires_tool(graph.node):
            required.add("tool")
        if missing := required - capabilities:
            message = f"execution host lacks capabilities: {sorted(missing)}"
            if "tool" in missing:
                message += "; native tool nodes require a Pi tool context exposing " \
                    "ExtensionToolContext.executeTool (tested on Pi 1.1.0)"
            raise ValueError(message)

    def list(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT a.* FROM assets a WHERE published=1 AND publication="
                              "(SELECT MAX(publication) FROM assets b WHERE b.id=a.id AND b.published=1) ORDER BY id").fetchall()
        return [manifest_view(self._asset(row)) for row in rows]

    def versions(self, asset_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT version, pin, published FROM assets WHERE id=? ORDER BY rowid",
                              (asset_id,)).fetchall()
        return [{"version": row["version"], "pin": row["pin"], "published": bool(row["published"])}
                for row in rows]

    @staticmethod
    def _asset(row: sqlite3.Row) -> dict[str, Any]:
        return {**json.loads(row["manifest"]), "pin": row["pin"], "published": bool(row["published"])}

    def get(self, asset_id: str, version: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM assets WHERE id=? AND version=?", (asset_id, version)).fetchone()
        if row is None:
            raise ValueError("asset version not found")
        return self._asset(row)

    def publish(self, asset_id: str, version: str, *, evidence: str | None = None) -> dict[str, Any]:
        """Publish a frozen version; ``evidence`` is the run finalizing right now."""
        asset = self.get(asset_id, version)
        if package_pin(self.root / "assets" / asset_id / version) != asset["pin"]:
            raise ValueError("frozen package has changed")
        if asset["published"]:
            return asset
        with self._db() as db:
            for row in db.execute("SELECT promotion FROM revisions WHERE promotion IS NOT NULL"):
                candidate = json.loads(row["promotion"])
                if candidate["version"] == version and candidate.get("asset_id") == asset_id and not asset["published"]:
                    raise ValueError("revision candidates publish only after their regression suite passes")
            proof = db.execute(
                "SELECT 1 FROM runs WHERE asset_id=? AND version=? AND pin=? "
                "AND (status='succeeded' OR id=?)",
                (asset_id, version, asset["pin"], evidence)).fetchone()
            if proof is None:
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
            params = self.validate_params(asset, params)
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
    def validate_params(asset, params):
        specs = asset["parameters"]
        typed = {
            key: parameter_spec(
                key,
                type_name=spec["type"],
                source=str(spec.get("source", key)),
                default=spec.get("default"),
            )
            for key, spec in specs.items()
        }
        try:
            return normalize_params(typed, params)
        except ParameterError as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        state = {key: json.loads(row[key]) if key in {"params", "result"} and row[key] else row[key]
                 for key in ("id", "asset_id", "version", "pin", "params", "status", "node", "result")}
        result = state.get("result")
        if state["status"] in _ACTIVE and isinstance(result, dict) and result.get("waiting"):
            result.pop("waiting")
            result["instruction"] = {
                "queued": "Resume queued. End this turn and wait for the completion notification; do not poll or call resume again.",
                "running": "Resumed execution is running. Wait for the completion notification; do not call resume again.",
                "cancelling": "Cancellation requested. Wait for execution to stop; do not call resume again.",
            }[state["status"]]
        return state

    def status(self, scope: str, run_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND scope=?", (run_id, scope)).fetchone()
        if row is None:
            raise ValueError("run not found in this conversation")
        state = self._run(row)
        # Old receipts remain on disk, but must not instruct new agents to stop.
        if isinstance(state['result'], dict) and 'revision_remaining' in state['result']:
            state['result'].pop('revision_remaining')
            state['result']['instruction'] = RECOVERY_INSTRUCTION
        with self._db() as db:
            stage = db.execute("SELECT id FROM stage_requests WHERE run_id=? AND state='pending'", (run_id,)).fetchone()
        if stage and state['status'] in _ACTIVE:
            state.update(phase='waiting_agent', stage_request_id=stage['id'])
        if state['status'] in _PARKED:
            waiting = (state.get('result') or {}).get('waiting')
            external = isinstance(waiting, dict) and waiting.get('kind') == 'external'
            state.update(phase='waiting_external' if external else 'waiting_human',
                         waiting=waiting if isinstance(waiting, dict) else None)
        revision = self.revisions.for_run(run_id)
        if revision:
            state.update(parent_run_id=revision['parent'], revision_id=revision['id'])
            if revision['promotion']:
                state['candidate_version'] = json.loads(revision['promotion'])['version']
        state.update(self._execution_status(state, revision))
        return state

    def _execution_status(self, state: dict[str, Any],
                          revision: dict[str, Any] | None) -> dict[str, Any]:
        run_id = str(state["id"])
        run_dir = self.root / "runs" / run_id
        package = self.root / "assets" / state["asset_id"] / state["version"]
        if revision:
            package = Path(revision["package"])
        events: list[dict[str, Any]] = []
        ledger = run_dir / "ledger.jsonl"
        if ledger.is_file():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
        nodes = self._checkpoint_nodes(run_dir / "checkpoint.json")
        result = state.get("result") if isinstance(state.get("result"), dict) else {}
        waiting = result.get("waiting")
        current = (
            state.get("node")
            or (waiting.get("node") if isinstance(waiting, dict) else None)
            or self._checkpoint_parked(run_dir)
        )
        sequence: list[str] = []
        try:
            graph = compile(package)
            sequence = self._leaf_paths(graph.node)
            if current is None:
                for path in sequence:
                    detail = nodes.get(path)
                    if not isinstance(detail, dict) or detail.get("status") != "succeeded":
                        current = path
                        break
        except Exception:
            sequence = []
        execution = result.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        return {
            "current_node": current,
            "step_index": (
                sequence.index(current) + 1
                if current in sequence
                else None
            ),
            "total_steps": len(sequence) or None,
            "started_at": events[0].get("ts") if events else None,
            "last_event": events[-1] if events else None,
            "execution_domain": execution.get("execution_domain"),
            "execution_path": execution.get("execution_path", str(run_dir)),
            "artifacts_dir": execution.get(
                "artifacts_dir", str(run_dir / "artifacts")
            ),
        }

    @staticmethod
    def _leaf_paths(node: Any) -> list[str]:
        if not node.children:
            return [node.path]
        return [path for child in node.children for path in GenomeService._leaf_paths(child)]

    def cancel(self, scope: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.status(scope, run_id)
            if state["status"] in _ACTIVE:
                self._signals.setdefault(run_id, threading.Event()).set()
                with self._db() as db:
                    db.execute("UPDATE runs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END "
                               "WHERE id=? AND status IN ('queued','running')", (run_id,))
                self._cancel_tools(run_id)
            elif state["status"] in _PARKED:
                # Nothing is running: a parked run only has to be taken out of the queue
                # the resume would put it back into.
                with self._db() as db:
                    db.execute(
                        "UPDATE runs SET status='cancelled' "
                        "WHERE id=? AND status IN ('waiting','waiting_external')",
                        (run_id,),
                    )
        return self.status(scope, run_id)

    def _cancel_tools(self, run_id: str) -> None:
        receipts = self.tool_receipts.unresolved(run_id)
        port = self._tool_ports.get(run_id)
        for receipt in receipts:
            execution_id = str(receipt["execution_id"])
            if receipt["state"] == "unknown":
                continue
            self.tool_receipts.request_cancel(
                run_id=run_id,
                execution_id=execution_id,
                error="cancellation requested",
            )
            try:
                if port is None:
                    raise RuntimeError("no live tool port")
                result = port.cancel(execution_id)
                self.tool_receipts.cancel_result(
                    run_id=run_id,
                    execution_id=execution_id,
                    confirmed=result.confirmed,
                    error=None if result.confirmed else result.detail,
                )
            except Exception as exc:
                self.tool_receipts.cancel_result(
                    run_id=run_id,
                    execution_id=execution_id,
                    confirmed=False,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )

    def _cancellation_unconfirmed(self, run_id: str) -> list[dict[str, Any]]:
        return self.tool_receipts.unresolved(run_id)

    def resume(self, scope: str, run_id: str,
               artifact: str | None = None) -> dict[str, Any]:
        """Put a parked run back in the queue; the host's worker picks it up from here.

        A crash can interrupt a run whose human gate the person already answered.
        That checkpoint still holds the answer, so an interrupted run wakes when its
        checkpoint left a node parked. ``artifact`` is where the person put the file
        the gate asked for, for the host to land on the artifact the graph declared.
        """
        with self._lock:
            state = self.status(scope, run_id)
            resumable = (
                state["status"] in _PARKED
                or (
                    state["status"] == "interrupted"
                    and (
                        self.parked_checkpoint(run_id) is not None
                        or self.awaiting_checkpoint(run_id) is not None
                    )
                )
            )
            if not resumable:
                return state
            with self._db() as db:
                db.execute("UPDATE runs SET handoff=? WHERE id=?",
                           (_json({"artifact": artifact}) if artifact else None, run_id))
                db.execute("UPDATE runs SET status='queued' "
                           "WHERE id=? AND status IN "
                           "('waiting','waiting_external','interrupted')", (run_id,))
        return self.status(scope, run_id)

    def replay_requeue(self, scope: str, run_id: str,
                       artifact: str | None = None) -> dict[str, Any]:
        """Queue one more attempt after a replay died.

        A run whose checkpoint still holds a parked human node continues from that
        checkpoint, so work the person already did is not asked again; anything else
        queues the same frozen draft from the start. Only an undecided capture may
        requeue: a published asset belongs to the revision channel.
        """
        state = self.status(scope, run_id)
        unresolved = self.tool_receipts.unresolved(run_id)
        if unresolved:
            raise ValueError(
                "run has unresolved tool side effects; reconcile or cancel them "
                "before replay"
            )
        asset = self.get(state["asset_id"], state["version"])
        if asset["published"]:
            raise ValueError("asset is already published; revise it instead of replaying")
        if state["status"] in _ACTIVE:
            raise ValueError("run is still live; wait for it or cancel it first")
        if state["status"] in _PARKED:
            raise ValueError("run is parked; use genome_resume to continue it")
        if (self.parked_checkpoint(run_id) is not None
                or self.awaiting_checkpoint(run_id) is not None):
            return self.resume(scope, run_id, artifact)
        capture = self.captures.for_run(run_id)
        queued = self.submit(scope, uuid4().hex, state["asset_id"], state["version"],
                             state["params"], allow_draft=True)
        if capture:
            self.captures.relink(capture["capture_id"], run_id=queued["id"], scope=scope)
        return queued

    def _run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def parked_checkpoint(self, run_id: str) -> str | None:
        """The human node a run's checkpoint left parked, if it has one."""
        return self._checkpoint_waiting(self._run_dir(run_id), "waiting")

    def awaiting_checkpoint(self, run_id: str) -> str | None:
        """The node a run's checkpoint left waiting on an external task."""
        return self._checkpoint_waiting(self._run_dir(run_id), "waiting_external")

    @staticmethod
    def _checkpoint_nodes(path: Path) -> dict[str, Any]:
        try:
            nodes = json.loads(path.read_text(encoding="utf-8"))["nodes"]
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        return nodes if isinstance(nodes, dict) else {}

    @classmethod
    def _checkpoint_waiting(cls, run_dir: Path, status: str) -> str | None:
        """The parked node a checkpoint still holds: an answer a crash did not consume.

        A park marks the node and every ancestor waiting, so the parked gate is the
        deepest one; the shallowest would be the graph root.
        """
        waiting = [name for name, entry in
                   cls._checkpoint_nodes(run_dir / "checkpoint.json").items()
                   if isinstance(entry, dict) and entry.get("status") == status]
        return max(waiting, key=lambda name: name.count(".")) if waiting else None

    @classmethod
    def _checkpoint_parked(cls, run_dir: Path) -> str | None:
        """The deepest parked node of either kind, for progress reporting."""
        waiting = [name for name, entry in
                   cls._checkpoint_nodes(run_dir / "checkpoint.json").items()
                   if isinstance(entry, dict)
                   and entry.get("status") in {"waiting", "waiting_external"}]
        return max(waiting, key=lambda name: name.count(".")) if waiting else None

    def execute(self, scope: str, run_id: str, host: Host,
                emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            state = self.status(scope, run_id)
            if state["status"] not in ("queued", *_PARKED):
                return state
            # resume() queues the same run but preserves its waiting result. Use
            # that durable marker after queueing, including across worker handoff.
            # Public status hides stale waiting instructions once queued. Read
            # the persisted marker directly; rendering must not erase recovery.
            with self._db() as db:
                stored = db.execute("SELECT result FROM runs WHERE id=? AND scope=?", (run_id, scope)).fetchone()
            parked = (json.loads(stored["result"]) if stored["result"] else {}).get("waiting")
            resuming_node = (
                str(parked.get("node"))
                if isinstance(parked, dict) and parked.get("node")
                and parked.get("kind", "human") == "human"
                else None
            )
            # A queued run may already have consumed its park: the waiting receipt or
            # the checkpoint is then the only evidence that this is a continuation.
            resuming = (state["status"] in _PARKED or isinstance(parked, dict)
                        or (self.root / "runs" / run_id / "checkpoint.json").exists())
            cancel = self._signals.setdefault(run_id, threading.Event())
            with self._db() as db:
                changed = db.execute(
                    "UPDATE runs SET status='running' "
                    "WHERE id=? AND status IN ('queued','waiting','waiting_external')",
                    (run_id,)).rowcount
            if not changed:
                return self.status(scope, run_id)
            with self._db() as db:
                row = db.execute("SELECT handoff FROM runs WHERE id=?", (run_id,)).fetchone()
            handoff = json.loads(row["handoff"]) if row and row["handoff"] else None
            human_artifact = handoff.get("artifact") if isinstance(handoff, dict) else None
        execution_evidence = {"log_tail": ""}
        promotion = None
        revision = None
        evidence_dir = self.root / "runs" / run_id
        def check_cancel() -> None:
            if cancel.is_set():
                raise RunCancelled()
        def event(payload: dict[str, Any]) -> Any:
            if payload.get("event") == "tool_call_started":
                execution_id = payload.get("execution_id")
                if isinstance(execution_id, str):
                    self._active_tools.setdefault(run_id, set()).add(execution_id)
                    self.tool_receipts.accepted(
                        run_id=run_id,
                        node=str(payload.get("node", "")),
                        attempt=int(payload.get("attempt", 0)),
                        execution_id=execution_id,
                        tool_name=str(payload.get("tool", "")),
                        arguments=(
                            payload.get("arguments")
                            if isinstance(payload.get("arguments"), dict)
                            else {}
                        ),
                    )
            if payload.get("event") == "tool_call_finished":
                execution_id = payload.get("execution_id")
                if isinstance(execution_id, str):
                    self._active_tools.setdefault(run_id, set()).discard(execution_id)
                    self.tool_receipts.finish(
                        run_id=run_id,
                        execution_id=execution_id,
                        state=str(payload.get("state", "unknown")),
                        receipt=(
                            payload.get("receipt")
                            if isinstance(payload.get("receipt"), dict)
                            else None
                        ),
                        error=(
                            str(payload["error"])
                            if payload.get("error") is not None
                            else None
                        ),
                    )
            if isinstance(payload.get("execution"), dict):
                execution_evidence.update(payload["execution"])
            if payload.get("command"):
                execution_evidence.update(command=payload['command'], node=payload.get('node'))
            if payload.get("rc") is not None:
                execution_evidence['exit_code'] = payload['rc']
            if payload.get("event") == "output":
                execution_evidence['log_tail'] = (execution_evidence['log_tail'] + payload.get('text', ''))[-8192:]
                log = self.root / "runs" / run_id / "execution.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(payload.get("text", ""))
            if payload.get("validation_case"):
                execution_evidence["validation_case"] = payload["validation_case"]
                with self._db() as db:
                    db.execute("UPDATE runs SET result=? WHERE id=?", (_json({"validation_case": payload["validation_case"]}), run_id))
            if payload.get("node"):
                with self._db() as db:
                    db.execute("UPDATE runs SET node=? WHERE id=?", (payload["node"], run_id))
            return emit(payload)
        try:
            check_cancel()
            package = self.root / "assets" / state["asset_id"] / state["version"]
            revision = self.revisions.for_run(run_id)
            if revision:
                package = Path(revision['package'])
            graph = compile(package)
            if graph.pin != state["pin"]:
                raise ValueError("frozen package has changed")
            self._supported_graph(graph.node)
            if resuming_node is None:
                # A crash can interrupt a wakeup before the engine re-enters the gate:
                # the checkpoint still names the node the person answered.
                resuming_node = self.parked_checkpoint(run_id)
            asset = json.loads((package / "manifest.json").read_text())
            paths = [key for key, spec in asset["parameters"].items() if spec["type"] == "path"]
            options = {}
            if revision:
                baseline = self.revisions.context(state)[2]
                original = self.root / "assets" / state["asset_id"] / baseline["version"]
                if package_pin(original) != baseline['pin']:
                    raise ValueError("original verification package changed")
                options['verification_package'] = original
            ports, params = host.prepare(run_id, package, state["params"], paths, cancel,
                                         event, resuming=resuming_node,
                                         human_artifact=human_artifact, **options)
            if package_pin(package) != state["pin"]:
                raise ValueError("package changed while preparing execution")
            agent_task = None
            if self._requires_agent(graph.node):
                agent_task = lambda request: self.stages.execute(scope, run_id, request, event, check_cancel)
            ports = replace(ports, check_cancel=check_cancel, on_event=event, agent_task=agent_task)
            if ports.tool is not None:
                self._tool_ports[run_id] = ports.tool
            diagnostics = handshake(graph, ports)
            if diagnostics:
                reasons = "; ".join(
                    f"{item.code} [{item.node or 'graph'}]: {item.message}"
                    for item in diagnostics
                )
                raise ValueError(f"unsupported graph capabilities: {reasons}")
            result = (resume_run(graph, ports, evidence_dir) if resuming
                      else run(graph, params, ports, evidence_dir))
            check_cancel()
            status = {"succeeded": "succeeded", "waiting": "waiting"}.get(
                str(result.status), "failed")
            details = {"outputs": result.outputs, "failure": result.failure,
                       "abandonment": result.abandonment, "waiting": result.waiting}
            if status == "succeeded" and revision and revision["promotion"]:
                promotion = self.regressions.validate(revision, host, cancel, event, check_cancel, self.root / "runs" / run_id / "regressions")
        except RegressionFailure as exc:
            status, details = "failed", {"error": str(exc), "failed_case": execution_evidence.get("validation_case")}
        except PortError as exc:
            status, details = "failed", {"error": str(exc)}
        except RunCancelled:
            unresolved = self._cancellation_unconfirmed(run_id)
            if unresolved:
                status, details = "interrupted", {
                    "error": "cancellation could not be confirmed",
                    "cancellation_unconfirmed": unresolved,
                }
            else:
                status, details = "cancelled", {}
        except Exception as exc:
            status, details = "interrupted", {"error": str(exc)}
        if cancel.is_set() and status != "interrupted":
            unresolved = self._cancellation_unconfirmed(run_id)
            if unresolved:
                status = "interrupted"
                details.update(
                    error="cancellation could not be confirmed",
                    cancellation_unconfirmed=unresolved,
                )
        details["execution"] = execution_evidence
        checkpoint = evidence_dir / "checkpoint.json"
        if checkpoint.exists():
            nodes = json.loads(checkpoint.read_text()).get("nodes", {})
            details["completed"] = [{"node": key, "outputs": value.get("outputs", {})}
                                    for key, value in nodes.items() if value.get("status") == "succeeded"]
            def remaining(node):
                found = [] if nodes.get(node.path, {}).get("status") == "succeeded" else [node.path]
                return found + [path for child in node.children for path in remaining(child)]
            details["remaining"] = remaining(graph.node)
            details["evidence"] = {"checkpoint": str(checkpoint), "ledger": str(evidence_dir / "ledger.jsonl")}
        if status == "failed":
            details["instruction"] = RECOVERY_INSTRUCTION
        elif status == "waiting":
            waiting = details.get("waiting")
            details["instruction"] = (
                AWAIT_INSTRUCTION
                if isinstance(waiting, dict) and waiting.get("kind") == "external"
                else WAITING_INSTRUCTION
            )
        capture = self.captures.for_run(run_id)
        if capture:
            # The run stays "running" until its capture verdict is part of the same
            # row, so no observer sees a finished capture run without its decision.
            # Replay B runs here, in the run's worker thread, never inside a tool call.
            if cancel.is_set() and status != "interrupted":
                unresolved = self._cancellation_unconfirmed(run_id)
                if unresolved:
                    status = "interrupted"
                    details["cancellation_unconfirmed"] = unresolved
                else:
                    status = "cancelled"
            try:
                details["capture"] = self.captures.finish(
                    capture["capture_id"], status=status, result=details, host=host,
                    emit=emit, cancel=cancel)
            except Exception as exc:
                details["capture"] = {"capture_id": capture["capture_id"], "error": str(exc)}
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if cancel.is_set() and status != "interrupted":
                status = "cancelled"
            if status == "succeeded" and promotion:
                try:
                    details["publication"] = self.regressions.publish(db, revision, promotion)
                except ValueError as exc:
                    status = "failed"
                    details["error"] = str(exc)
            db.execute("UPDATE runs SET status=?, result=? WHERE id=?", (status, _json(details), run_id))
            self._signals.pop(run_id, None)
            self._tool_ports.pop(run_id, None)
            self._active_tools.pop(run_id, None)
        return self.status(scope, run_id)
