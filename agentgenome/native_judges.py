"""Connection-owned, bounded judge calls; separate from agent stage rounds."""
from __future__ import annotations

import asyncio
import json

from .judge import MAX_BYTES


class NativeJudges:
    def __init__(self, service, timeout=110):
        self.service, self.timeout = service, timeout
        self.pending = {}

    async def wait(self, scope, owner, request, deliverable):
        future = asyncio.get_running_loop().create_future()
        item = dict(scope=scope, owner=owner, request=request, future=future, claimed=False)
        self.pending[request['request_id']] = item
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            while True:
                signal = self.service._signals.get(request['run_id'])
                if signal is not None and signal.is_set():
                    return {'error': 'model judge cancelled'}
                if not deliverable():
                    return {'error': 'model judge owner disconnected'}
                if asyncio.get_running_loop().time() >= deadline:
                    return {'error': 'model judge timed out'}
                try:
                    return await asyncio.wait_for(asyncio.shield(future), .1)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.pending.pop(request['request_id'], None)
            if not future.done():
                future.cancel()

    def snapshot(self, scope, owner):
        return [item['request'] for item in self.pending.values()
                if item['scope'] == scope and item['owner'] == owner
                and not item['claimed'] and not item['future'].done()]

    def active(self, scope, owner, request_id):
        item = self.pending.get(request_id)
        if (not item or item['scope'] != scope or item['owner'] != owner
                or item['future'].done()):
            raise ValueError('judge request is not active on this connection')
        if self.service.status(scope, item['request']['run_id'])['status'] != 'running':
            raise ValueError('judge run is no longer active')
        return item

    def claim(self, scope, owner, request_id):
        item = self.active(scope, owner, request_id)
        if item['claimed']:
            return {'claimed': False}
        item['claimed'] = True
        return {'claimed': True, 'request': item['request']}

    def result(self, scope, owner, args):
        item = self.active(scope, owner, args['request_id'])
        if not item['claimed']:
            raise ValueError('judge request has not been claimed')
        response = args.get('response')
        verdict = response.get('verdict') if isinstance(response, dict) else None
        if (isinstance(response, dict) and isinstance(response.get('error'), str)
                and response['error'].strip()):
            response = {'error': response['error'][:2000]}
        elif (isinstance(verdict, dict) and type(verdict.get('passed')) is bool
              and isinstance(verdict.get('reason'), str) and verdict['reason'].strip()
              and len(json.dumps(verdict).encode()) < MAX_BYTES - 1024):
            response = {'verdict': {'passed': verdict['passed'], 'reason': verdict['reason']}}
        else:
            response = {'error': 'invalid model judge verdict'}
        item['future'].set_result(response)
        return {'accepted': True}
