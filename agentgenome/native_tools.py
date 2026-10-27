"""One dispatch per execution ID, owned by the connection starting the run."""
from __future__ import annotations

import asyncio
import json
import time

from core.ports import PortError, ToolResult


class NativeTools:
    def __init__(self, service, timeout=300, cancel_timeout=5):
        self.service, self.timeout, self.cancel_timeout = service, timeout, cancel_timeout
        self.pending = {}

    async def call(self, scope, owner, request, deliverable):
        execution_id = request['execution_id']
        if execution_id in self.pending:
            raise PortError('tool execution already dispatched', kind='bad_output')
        future = asyncio.get_running_loop().create_future()
        item = dict(scope=scope, owner=owner, request=request, claimed=False,
                    future=future, cancel_at=None)
        self.pending[execution_id] = item
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                signal = self.service._signals.get(request['run_id'])
                if signal is not None and signal.is_set():
                    self.cancel(scope, owner, execution_id)
                if item['cancel_at'] is not None and not item['claimed']:
                    return ToolResult(True, {}, 'Tool cancelled before dispatch')
                if not deliverable():
                    raise PortError('tool owner disconnected; outcome unknown, do not retry automatically', kind='unavailable')
                if time.monotonic() >= deadline or (item['cancel_at'] is not None
                        and time.monotonic() - item['cancel_at'] >= self.cancel_timeout):
                    raise PortError('tool stop/result not confirmed; outcome unknown, do not retry automatically', kind='timeout')
                try:
                    return await asyncio.wait_for(asyncio.shield(future), .1)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.pending.pop(execution_id, None)
            if not future.done():
                future.cancel()

    def cancel(self, scope, owner, execution_id):
        item = self.pending.get(execution_id)
        if item and item['scope'] == scope and item['owner'] == owner and item['cancel_at'] is None:
            item['cancel_at'] = time.monotonic()

    def snapshot(self, scope, owner):
        return [{**item['request'], 'claimed': item['claimed'],
                 'cancel_requested': item['cancel_at'] is not None}
                for item in self.pending.values() if item['scope'] == scope
                and item['owner'] == owner and not item['future'].done()]

    def active(self, scope, owner, execution_id):
        item = self.pending.get(execution_id)
        if not item or item['scope'] != scope or item['owner'] != owner or item['future'].done():
            raise ValueError('tool execution is not active on this connection')
        return item

    def claim(self, scope, owner, execution_id):
        item = self.active(scope, owner, execution_id)
        if item['claimed'] or item['cancel_at'] is not None:
            return {'claimed': False}
        item['claimed'] = True
        return {'claimed': True, 'request': item['request']}

    def result(self, scope, owner, args):
        item = self.active(scope, owner, args['execution_id'])
        if not item['claimed']:
            raise ValueError('tool execution not claimed')
        response = args.get('response')
        if (not isinstance(response, dict) or type(response.get('is_error')) is not bool
                or not isinstance(response.get('result'), dict)
                or not isinstance(response.get('text'), str)
                or len(json.dumps(response).encode()) > 262144):
            item['future'].set_exception(PortError('invalid tool response; outcome unknown', kind='bad_output'))
        else:
            item['future'].set_result(ToolResult(response['is_error'], response['result'], response['text']))
        return {'accepted': True}
