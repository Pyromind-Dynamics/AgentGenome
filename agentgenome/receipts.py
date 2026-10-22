"""Durable receipts for graph tool calls.

A tool call crosses an external side-effect boundary.  The receipt is written
before dispatch and updated only after the host proves a terminal outcome.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _args_digest(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(arguments).encode("utf-8")).hexdigest()


class ToolReceipts:
    TERMINAL = {"completed", "failed", "cancelled"}
    UNRESOLVED = {
        "accepted",
        "cancel_requested",
        "cancel_unconfirmed",
        "unknown",
    }

    def __init__(self, service: Any) -> None:
        self.service = service
        self._lock = threading.RLock()
        with service._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_receipts (
                    run_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    execution_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, execution_id)
                );
                CREATE INDEX IF NOT EXISTS tool_receipts_run_state
                    ON tool_receipts(run_id, state);
                """
            )

    def accepted(
        self,
        *,
        run_id: str,
        node: str,
        attempt: int,
        execution_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        now = _now()
        with self._lock, self.service._db() as db:
            db.execute(
                """
                INSERT INTO tool_receipts(
                    run_id, node, attempt, execution_id, tool_name, args_digest,
                    state, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, execution_id) DO UPDATE SET
                    state='accepted', error=NULL, updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    node,
                    attempt,
                    execution_id,
                    tool_name,
                    _args_digest(arguments),
                    "accepted",
                    now,
                    now,
                ),
            )

    def finish(
        self,
        *,
        run_id: str,
        execution_id: str,
        state: str,
        receipt: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if state not in self.TERMINAL | self.UNRESOLVED:
            raise ValueError(f"invalid tool receipt state: {state}")
        payload = _json(dict(receipt or {}))[:16384] if receipt is not None else None
        with self._lock, self.service._db() as db:
            db.execute(
                """
                UPDATE tool_receipts
                   SET state=?, receipt=?, error=?, updated_at=?
                 WHERE run_id=? AND execution_id=?
                """,
                (state, payload, error, _now(), run_id, execution_id),
            )

    def request_cancel(
        self,
        *,
        run_id: str,
        execution_id: str,
        error: str | None = None,
    ) -> None:
        with self._lock, self.service._db() as db:
            db.execute(
                """
                UPDATE tool_receipts
                   SET state='cancel_requested', error=?, updated_at=?
                 WHERE run_id=? AND execution_id=? AND state='accepted'
                """,
                (error, _now(), run_id, execution_id),
            )

    def cancel_result(
        self,
        *,
        run_id: str,
        execution_id: str,
        confirmed: bool,
        error: str | None = None,
    ) -> None:
        state = "cancelled" if confirmed else "cancel_unconfirmed"
        with self._lock, self.service._db() as db:
            db.execute(
                """
                UPDATE tool_receipts
                   SET state=?, error=?, updated_at=?
                 WHERE run_id=? AND execution_id=?
                   AND state IN ('accepted','cancel_requested','cancel_unconfirmed')
                """,
                (state, error, _now(), run_id, execution_id),
            )

    def recover(self) -> None:
        with self._lock, self.service._db() as db:
            db.execute(
                """
                UPDATE tool_receipts
                   SET state='unknown',
                       error=COALESCE(error, 'host restarted before a terminal receipt'),
                       updated_at=?
                 WHERE state IN ('accepted','cancel_requested','cancel_unconfirmed')
                """,
                (_now(),),
            )

    def unresolved(self, run_id: str) -> list[dict[str, Any]]:
        with self.service._db() as db:
            rows = db.execute(
                """
                SELECT execution_id, node, attempt, tool_name, state, error
                  FROM tool_receipts
                 WHERE run_id=? AND state IN
                       ('accepted','cancel_requested','cancel_unconfirmed','unknown')
                 ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "execution_id": row["execution_id"],
                "node": row["node"],
                "attempt": row["attempt"],
                "tool": row["tool_name"],
                "state": row["state"],
                "error": row["error"],
            }
            for row in rows
        ]
