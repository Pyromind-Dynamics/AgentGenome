"""Durable execution plans.

Replaying a graph is an external action.  A plan records the exact asset,
parameters, analysis and scope the user was shown before that action starts.
The plan is intentionally separate from a run: a run is created only when the
plan is confirmed, and the plan's digest is checked again at confirmation time.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from builder import compile, package_pin

from .analysis import analyze_graph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_key(value: str) -> str:
    """A plan id as stored: bare hex.

    A caller that copies the id by hand tends to write it as a dashed UUID
    (`4a529fca-07f0-…`), and the lookup then answers "plan not found" for a plan
    that is right there. Both spellings name the same row.
    """
    return str(value).strip().lower().replace("-", "")


def _encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class RunPlans:
    """CRUD for the user-confirmed execution plan boundary."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self._lock = threading.RLock()
        with service._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_plans (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    pin TEXT NOT NULL,
                    params TEXT NOT NULL,
                    analysis TEXT NOT NULL,
                    capture_id TEXT,
                    revision_id TEXT,
                    run_id TEXT,
                    allow_mismatch INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, request_id)
                );
                CREATE INDEX IF NOT EXISTS run_plans_scope_status
                    ON run_plans(scope, status);
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(run_plans)")
            }
            if "allow_mismatch" not in columns:
                db.execute(
                    "ALTER TABLE run_plans "
                    "ADD COLUMN allow_mismatch INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _payload(row: Any) -> dict[str, Any]:
        analysis = json.loads(row["analysis"])
        execution_scope = analysis.get("execution_scope")
        state = {
            "plan_id": row["id"],
            "scope": row["scope"],
            "request_id": row["request_id"],
            "kind": row["kind"],
            "asset_id": row["asset_id"],
            "version": row["version"],
            "pin": row["pin"],
            "params": json.loads(row["params"]),
            "analysis": analysis,
            "capture_id": row["capture_id"],
            "revision_id": row["revision_id"],
            "run_id": row["run_id"],
            "allow_mismatch": bool(row["allow_mismatch"]),
            "status": row["status"],
            "digest": row["digest"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "requires_confirmation": row["status"] == "planned",
            "requires_scope_confirmation": bool(
                isinstance(execution_scope, dict)
                and execution_scope.get("mode") == "unspecified"
            ),
        }
        return state

    @staticmethod
    def digest_for(*, kind: str, asset_id: str, version: str, pin: str,
                   params: dict[str, Any], capture_id: str | None,
                   revision_id: str | None, allow_mismatch: bool,
                   analysis: dict[str, Any]) -> str:
        data = {
            "kind": kind,
            "asset_id": asset_id,
            "version": version,
            "pin": pin,
            "params": params,
            "capture_id": capture_id,
            "revision_id": revision_id,
            "allow_mismatch": bool(allow_mismatch),
            "analysis": analysis,
        }
        return hashlib.sha256(_encoded(data).encode("utf-8")).hexdigest()

    def create(self, *, scope: str, request_id: str, kind: str, asset_id: str,
               version: str, pin: str, params: dict[str, Any],
               analysis: dict[str, Any], capture_id: str | None = None,
               revision_id: str | None = None,
               allow_mismatch: bool = False) -> dict[str, Any]:
        if not scope or not request_id:
            raise ValueError("scope and request id are required")
        digest = self.digest_for(
            kind=kind,
            asset_id=asset_id,
            version=version,
            pin=pin,
            params=params,
            capture_id=capture_id,
            revision_id=revision_id,
            allow_mismatch=allow_mismatch,
            analysis=analysis,
        )
        canonical = {
            "kind": kind,
            "asset_id": asset_id,
            "version": version,
            "pin": pin,
            "params": params,
            "analysis": analysis,
            "capture_id": capture_id,
            "revision_id": revision_id,
            "allow_mismatch": bool(allow_mismatch),
            "digest": digest,
        }
        with self._lock, self.service._db() as db:
            previous = db.execute(
                "SELECT * FROM run_plans WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
            if previous is not None:
                current = self._payload(previous)
                if current["digest"] != digest:
                    raise ValueError("request id reused with a different execution plan")
                return current
            plan_id = uuid4().hex
            now = _now()
            db.execute(
                """
                INSERT INTO run_plans(
                    id, scope, request_id, kind, asset_id, version, pin, params,
                    analysis, capture_id, revision_id, allow_mismatch, status,
                    digest, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan_id,
                    scope,
                    request_id,
                    kind,
                    asset_id,
                    version,
                    pin,
                    _encoded(params),
                    _encoded(analysis),
                    capture_id,
                    revision_id,
                    int(allow_mismatch),
                    "planned",
                    digest,
                    now,
                    now,
                ),
            )
        return self.get(scope, plan_id)

    def get(self, scope: str, plan_id: str) -> dict[str, Any]:
        with self.service._db() as db:
            row = db.execute(
                "SELECT * FROM run_plans WHERE scope=? AND id=?",
                (scope, plan_id),
            ).fetchone()
        if row is None:
            raise ValueError("execution plan not found in this conversation")
        return self._payload(row)

    def mark_confirmed(self, scope: str, plan_id: str, run_id: str) -> dict[str, Any]:
        with self._lock, self.service._db() as db:
            changed = db.execute(
                """
                UPDATE run_plans
                   SET run_id=?, status='confirmed', updated_at=?
                 WHERE scope=? AND id=? AND status='planned'
                """,
                (run_id, _now(), scope, plan_id),
            ).rowcount
            if not changed:
                row = db.execute(
                    "SELECT * FROM run_plans WHERE scope=? AND id=?",
                    (scope, plan_id),
                ).fetchone()
                if row is None:
                    raise ValueError("execution plan not found in this conversation")
                if row["run_id"] != run_id:
                    raise ValueError("execution plan was already confirmed with another run")
        return self.get(scope, plan_id)

    def _analysis(
        self,
        *,
        asset: dict[str, Any],
        params: dict[str, Any],
        source: Any,
        requested_scope: dict[str, Any] | None,
        compatibility: dict[str, Any] | None,
        capture_id: str | None,
    ) -> dict[str, Any]:
        package = self.service.root / "assets" / asset["id"] / asset["version"]
        if package_pin(package) != asset["pin"]:
            raise ValueError("frozen package has changed")
        graph = compile(package)
        return analyze_graph(
            graph,
            asset,
            params,
            source,
            requested_scope=requested_scope,
            compatibility=compatibility,
            capture_id=capture_id,
        )

    def prepare(
        self,
        *,
        scope: str,
        request_id: str,
        kind: str,
        asset_id: str,
        version: str,
        params: dict[str, Any],
        source: Any,
        requested_scope: dict[str, Any] | None = None,
        compatibility: dict[str, Any] | None = None,
        capture_id: str | None = None,
        revision_id: str | None = None,
        allow_mismatch: bool = False,
    ) -> dict[str, Any]:
        if kind not in {"asset", "capture", "revision"}:
            raise ValueError("unsupported execution plan kind")
        asset = self.service.get(asset_id, version)
        if not asset["published"] and kind != "capture":
            raise ValueError("asset is not published")
        effective = self.service.validate_params(asset, params)
        if capture_id is not None:
            capture = self.service.captures.state(capture_id)
            if (
                capture.get("asset_id") != asset_id
                or capture.get("version") != version
                or capture.get("pin") != asset["pin"]
            ):
                raise ValueError("capture does not belong to this frozen asset")
        analysis = self._analysis(
            asset=asset,
            params=effective,
            source=source,
            requested_scope=requested_scope,
            compatibility=compatibility,
            capture_id=capture_id,
        )
        return self.create(
            scope=scope,
            request_id=request_id,
            kind=kind,
            asset_id=asset_id,
            version=version,
            pin=asset["pin"],
            params=effective,
            analysis=analysis,
            capture_id=capture_id,
            revision_id=revision_id,
            allow_mismatch=allow_mismatch,
        )

    def confirm(
        self,
        *,
        scope: str,
        plan_id: str,
        source: Any,
        digest: str | None = None,
        compatibility: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            plan = self.get(scope, plan_id)
            if digest is not None and digest != plan["digest"]:
                raise ValueError("execution plan digest changed; prepare the plan again")
            execution_scope = plan["analysis"].get("execution_scope")
            if (
                isinstance(execution_scope, dict)
                and execution_scope.get("mode") == "unspecified"
            ):
                raise ValueError(
                    "execution scope is not confirmed; choose full, sample, or subset "
                    "and prepare the plan again"
                )
            if plan["status"] == "confirmed" and plan["run_id"]:
                return {
                    "plan": plan,
                    "run": self.service.status(scope, plan["run_id"]),
                }
            asset = self.service.get(plan["asset_id"], plan["version"])
            if asset["pin"] != plan["pin"]:
                raise ValueError("asset pin changed; prepare the execution plan again")
            effective = self.service.validate_params(asset, plan["params"])
            analysis = self._analysis(
                asset=asset,
                params=effective,
                source=source,
                requested_scope=plan["analysis"].get("execution_scope"),
                compatibility=compatibility,
                capture_id=plan["capture_id"],
            )
            current = self.digest_for(
                kind=plan["kind"],
                asset_id=asset["id"],
                version=asset["version"],
                pin=asset["pin"],
                params=effective,
                capture_id=plan["capture_id"],
                revision_id=plan["revision_id"],
                allow_mismatch=plan["allow_mismatch"],
                analysis=analysis,
            )
            if current != plan["digest"]:
                raise ValueError(
                    "inputs or graph changed after the plan was prepared; "
                    "show the new analysis and ask the user to confirm again"
                )
            state = self.service.submit(
                scope,
                f"plan:{plan_id}",
                plan["asset_id"],
                plan["version"],
                effective,
                allow_draft=plan["kind"] == "capture",
            )
            if plan["capture_id"]:
                self.service.captures.relink(
                    plan["capture_id"],
                    run_id=state["id"],
                    scope=scope,
                )
            confirmed = self.mark_confirmed(scope, plan_id, state["id"])
            return {"plan": confirmed, "run": state}


class DistillPlans:
    """Durable chain previews that must be confirmed before a capture is created."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self._lock = threading.RLock()
        self._confirmation_events: dict[str, threading.Event] = {}
        with service._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS distill_plans (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    analysis TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    capture_id TEXT,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, request_id)
                );
                CREATE INDEX IF NOT EXISTS distill_plans_scope_status
                    ON distill_plans(scope, status);
                CREATE TABLE IF NOT EXISTS distill_plan_requests (
                    scope TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope, request_id)
                );
                """
            )
            columns = {row['name'] for row in db.execute('PRAGMA table_info(distill_plans)')}
            if 'revision' not in columns:
                db.execute('ALTER TABLE distill_plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')

    @staticmethod
    def _payload(row: Any) -> dict[str, Any]:
        result = json.loads(row["result"]) if row["result"] else None
        return {
            "plan_id": row["id"],
            "scope": row["scope"],
            "request_id": row["request_id"],
            "arguments": json.loads(row["arguments"]),
            "analysis": json.loads(row["analysis"]),
            "digest": row["digest"],
            "status": row["status"],
            "result": result,
            "capture_id": row["capture_id"],
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "requires_confirmation": row["status"] == "planned",
            "revision": row["revision"],
        }

    @staticmethod
    def digest_for(
        *, arguments: dict[str, Any], analysis: dict[str, Any]
    ) -> str:
        return hashlib.sha256(
            _encoded({"arguments": arguments, "analysis": analysis}).encode("utf-8")
        ).hexdigest()

    def create(
        self,
        *,
        scope: str,
        request_id: str,
        arguments: dict[str, Any],
        analysis: dict[str, Any],
        plan_id: str | None = None,
        base_digest: str | None = None,
    ) -> dict[str, Any]:
        if not scope or not request_id:
            raise ValueError("scope and request id are required")
        digest = self.digest_for(arguments=arguments, analysis=analysis)
        fingerprint = self.digest_for(arguments={"plan_id": plan_id, "base_digest": base_digest},
                                      analysis={"digest": digest})
        issues = tuple(
            (analysis.get("review") or {}).get("blocking_issues") or ()
        )
        with self._lock, self.service._db() as db:
            receipt = db.execute(
                'SELECT fingerprint,payload FROM distill_plan_requests WHERE scope=? AND request_id=?',
                (scope, request_id)).fetchone()
            if receipt is not None:
                if receipt['fingerprint'] != fingerprint:
                    raise ValueError('request id reused with a different distillation plan')
                return {**json.loads(receipt['payload']), 'repeated': True}
            previous = db.execute(
                "SELECT * FROM distill_plans WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
            if previous is not None:
                if plan_id is not None:
                    # The id already names a plan's creation — another plan's, or
                    # this same plan's. Either way it may not also name a revision:
                    # one request id stands for exactly one create/revise operation.
                    raise ValueError(
                        'request id already used by another distillation plan'
                        if previous['id'] != _plan_key(plan_id)
                        else 'request id already used to create this distillation plan; '
                             'revise with a fresh request id')
                current = self._payload(previous)
                if current["digest"] != digest:
                    raise ValueError(
                        "request id reused with a different distillation plan")
                # A creation recorded before request receipts existed:
                # register it so the same id cannot be reused elsewhere.
                db.execute('INSERT OR IGNORE INTO distill_plan_requests VALUES(?,?,?,?)',
                           (scope, request_id, fingerprint, _encoded(current)))
                return current
            if plan_id is not None:
                row = db.execute('SELECT * FROM distill_plans WHERE scope=? AND id=?',
                                 (scope, _plan_key(plan_id))).fetchone()
                if row is None:
                    raise ValueError('distillation plan not found in this conversation')
                current = self._payload(row)
                if current['status'] != 'planned':
                    raise ValueError('only an unconfirmed distillation plan can be revised')
                if not base_digest or base_digest != current['digest']:
                    raise ValueError('distillation plan digest changed; query the current plan before revising')
                initial_fingerprint = self.digest_for(
                    arguments={"plan_id": None, "base_digest": None},
                    analysis={"digest": current['digest']})
                db.execute('INSERT OR IGNORE INTO distill_plan_requests VALUES(?,?,?,?)',
                           (scope, current['request_id'], initial_fingerprint, _encoded(current)))
                if digest != current['digest']:
                    db.execute('UPDATE distill_plans SET arguments=?,analysis=?,digest=?, '
                               'revision=revision+1,updated_at=? WHERE id=? AND scope=?',
                               (_encoded(arguments), _encoded(analysis), digest, _now(),
                                _plan_key(plan_id), scope))
                row = db.execute('SELECT * FROM distill_plans WHERE scope=? AND id=?',
                                 (scope, _plan_key(plan_id))).fetchone()
                state = self._payload(row)
                db.execute('INSERT INTO distill_plan_requests VALUES(?,?,?,?)',
                           (scope, request_id, fingerprint, _encoded(state)))
                return state
            # Re-submitting without changing anything used to open a new plan that
            # reported the same problems, so a fix round looked like progress when
            # nothing had moved. Answer with the plan already on the table.
            if issues:
                latest = db.execute(
                    "SELECT * FROM distill_plans WHERE scope=? AND status='planned' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (scope,),
                ).fetchone()
                if latest is not None:
                    stored = self._payload(latest)
                    stored_issues = tuple(
                        (stored["analysis"].get("review") or {}).get("blocking_issues")
                        or ()
                    )
                    if (stored_issues == issues and stored['arguments'] == arguments
                            and stored['analysis'].get('review', {}).get('trace_evidence')
                            == analysis.get('review', {}).get('trace_evidence')):
                        db.execute(
                            'INSERT OR IGNORE INTO distill_plan_requests VALUES(?,?,?,?)',
                            (scope, request_id, fingerprint, _encoded(stored)))
                        return {**stored, "repeated": True}
            plan_id = uuid4().hex
            now = _now()
            db.execute(
                """
                INSERT INTO distill_plans(
                    id, scope, request_id, arguments, analysis, digest, status,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan_id,
                    scope,
                    request_id,
                    _encoded(arguments),
                    _encoded(analysis),
                    digest,
                    "planned",
                    now,
                    now,
                ),
            )
            # Register the creation request itself: the same id may not later name
            # a revision of another plan.
            row = db.execute('SELECT * FROM distill_plans WHERE scope=? AND id=?',
                             (scope, plan_id)).fetchone()
            db.execute('INSERT INTO distill_plan_requests VALUES(?,?,?,?)',
                       (scope, request_id, fingerprint, _encoded(self._payload(row))))
        return self.get(scope, plan_id)

    def get(self, scope: str, plan_id: str) -> dict[str, Any]:
        with self.service._db() as db:
            row = db.execute(
                "SELECT * FROM distill_plans WHERE scope=? AND id=?",
                (scope, _plan_key(plan_id)),
            ).fetchone()
        if row is None:
            raise ValueError("distillation plan not found in this conversation")
        return self._payload(row)

    def previews(self, scope: str) -> list[dict[str, Any]]:
        with self.service._db() as db:
            rows = db.execute('SELECT * FROM distill_plans WHERE scope=? '
                              'ORDER BY updated_at DESC LIMIT 20', (scope,)).fetchall()
        return [{key: state[key] for key in ('plan_id', 'digest', 'revision', 'status',
                                             'capture_id', 'run_id', 'updated_at')}
                | {'label': state['arguments'].get('label'),
                   'ready': state['analysis'].get('review', {}).get('validation', {}).get('ready', False)}
                for state in map(self._payload, rows)]

    def begin_confirmation(
        self,
        scope: str,
        plan_id: str,
        *,
        digest: str | None = None,
        stale_after_seconds: int = 600,
    ) -> dict[str, Any]:
        plan_id = _plan_key(plan_id)
        if not digest or not digest.strip():
            # A plan id is stable across revisions, so the id alone no longer
            # proves the user saw the current content: confirmation is bound to
            # the digest of the exact version that was shown.
            raise ValueError(
                "confirming a distillation plan requires its current digest; "
                "query the plan with genome_distill, show that version to the "
                "user, and confirm with the digest you showed"
            )
        digest = digest.strip()
        stale_before = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - stale_after_seconds,
            timezone.utc,
        ).isoformat()
        while True:
            wait_event: threading.Event | None = None
            stale_event: threading.Event | None = None
            acquired = False
            with self._lock, self.service._db() as db:
                row = db.execute(
                    "SELECT * FROM distill_plans WHERE scope=? AND id=?",
                    (scope, plan_id),
                ).fetchone()
                if row is None:
                    raise ValueError("distillation plan not found in this conversation")
                state = self._payload(row)
                if digest != state["digest"]:
                    raise ValueError(
                        "distillation plan digest changed; "
                        "show the plan again and ask the user"
                    )
                if state["status"] == "confirmed":
                    return state
                if (
                    state["status"] == "confirming"
                    and state["updated_at"] >= stale_before
                ):
                    wait_event = self._confirmation_events.get(plan_id)
                    if wait_event is None:
                        raise ValueError(
                            "this distillation plan is already being confirmed; "
                            "retry without resubmitting the plan"
                        )
                else:
                    stale_event = self._confirmation_events.pop(plan_id, None)
                    acquired = bool(
                        db.execute(
                            """
                            UPDATE distill_plans
                               SET status='confirming', updated_at=?
                             WHERE scope=? AND id=?
                               AND (
                                   status='planned'
                                   OR (status='confirming' AND updated_at<?)
                               )
                            """,
                            (_now(), scope, plan_id, stale_before),
                        ).rowcount
                    )
                    if acquired:
                        self._confirmation_events[plan_id] = threading.Event()
            if stale_event is not None:
                stale_event.set()
            if wait_event is not None:
                if not wait_event.wait(timeout=stale_after_seconds):
                    raise ValueError(
                        "timed out waiting for the in-flight distillation confirmation; "
                        "retry without resubmitting the plan"
                    )
                latest = self.get(scope, plan_id)
                if latest["status"] == "confirmed":
                    return latest
                if latest["status"] == "planned":
                    raise ValueError(
                        "the in-flight distillation confirmation failed; "
                        "inspect the error and confirm the same plan again"
                    )
                continue
            if acquired:
                return self.get(scope, plan_id)

    def release_confirmation(self, scope: str, plan_id: str) -> None:
        event: threading.Event | None = None
        with self._lock, self.service._db() as db:
            changed = db.execute(
                """
                UPDATE distill_plans
                   SET status='planned', updated_at=?
                 WHERE scope=? AND id=? AND status='confirming'
                """,
                (_now(), scope, plan_id),
            ).rowcount
            if changed:
                event = self._confirmation_events.pop(plan_id, None)
        if event is not None:
            event.set()

    def mark_confirmed(
        self,
        scope: str,
        plan_id: str,
        *,
        result: dict[str, Any],
        capture_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        plan_id = _plan_key(plan_id)
        event: threading.Event | None = None
        with self._lock, self.service._db() as db:
            row = db.execute(
                "SELECT * FROM distill_plans WHERE scope=? AND id=?",
                (scope, plan_id),
            ).fetchone()
            if row is None:
                raise ValueError("distillation plan not found in this conversation")
            if row["status"] == "confirmed":
                event = self._confirmation_events.pop(plan_id, None)
            else:
                changed = db.execute(
                    """
                    UPDATE distill_plans
                       SET status='confirmed', result=?, capture_id=?, run_id=?,
                           updated_at=?
                     WHERE scope=? AND id=? AND status IN ('planned','confirming')
                    """,
                    (
                        _encoded(result),
                        capture_id,
                        run_id,
                        _now(),
                        scope,
                        plan_id,
                    ),
                ).rowcount
                if not changed:
                    raise ValueError("distillation plan could not be confirmed")
                event = self._confirmation_events.pop(plan_id, None)
        if event is not None:
            event.set()
        return self.get(scope, plan_id)
