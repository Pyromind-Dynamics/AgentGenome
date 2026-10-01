"""Persistent receipts are distinct from transport delivery and round completion."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from core.ports import PortError


class StageRequests:
    def __init__(self, service):
        self.service = service
        with service._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS stage_requests (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope TEXT NOT NULL,
                request TEXT NOT NULL, receipt TEXT, result TEXT,
                state TEXT NOT NULL DEFAULT 'pending')''')

    def recover(self):
        with self.service._db() as db:
            db.execute("UPDATE stage_requests SET state='interrupted' WHERE state='pending'")

    def receipt(self, scope: str, request_id: str, outcome: str, summary: str,
                execution_id: str) -> dict[str, Any]:
        if outcome not in {'completed', 'failed'} or not execution_id:
            raise ValueError('invalid stage receipt')
        receipt = json.dumps(dict(outcome=outcome, summary=summary, execution_id=execution_id), sort_keys=True)
        with self.service._lock, self.service._db() as db:
            row = db.execute('SELECT * FROM stage_requests WHERE id=? AND scope=?', (request_id, scope)).fetchone()
            if row is None:
                raise ValueError('stage request not found in this conversation')
            run = db.execute('SELECT status FROM runs WHERE id=?', (row['run_id'],)).fetchone()
            if row['state'] != 'pending' or run['status'] != 'running':
                raise ValueError('stage request is no longer active')
            if row['receipt'] and row['receipt'] != receipt:
                raise ValueError('conflicting stage receipt')
            db.execute('UPDATE stage_requests SET receipt=? WHERE id=?', (receipt, request_id))
        return {'accepted': True, 'request_id': request_id, 'awaiting_verification': True}

    def execute(self, scope, run_id, request, emit, check_cancel):
        request = {**request, 'request_id': uuid4().hex, 'run_id': run_id}
        with self.service._db() as db:
            db.execute('INSERT INTO stage_requests(id,run_id,scope,request) VALUES(?,?,?,?)',
                       (request['request_id'], run_id, scope, json.dumps(request)))
        result = None
        try:
            result = emit({'event': 'waiting_agent', 'node': request['node_id'], 'request': request})
            check_cancel()
            with self.service._db() as db:
                row = db.execute('SELECT receipt FROM stage_requests WHERE id=?', (request['request_id'],)).fetchone()
            receipt = json.loads(row['receipt']) if row['receipt'] else None
            if (not isinstance(result, dict) or result.get('status') != 'completed'
                    or not receipt or receipt['execution_id'] != result.get('execution_id')):
                raise PortError('Agent stage ended without a matching successful round and receipt', kind='bad_output')
            if receipt['outcome'] != 'completed':
                raise PortError(receipt['summary'] or 'Agent could not complete this stage', kind='bad_output')
            return receipt['summary']
        finally:
            with self.service._db() as db:
                db.execute("UPDATE stage_requests SET state='finished',result=? WHERE id=?",
                           (json.dumps(result), request['request_id']))
