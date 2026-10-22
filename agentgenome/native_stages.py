"""Native connection-owned stage handoff. Recovery never re-delivers old work."""
from __future__ import annotations

import asyncio
from uuid import uuid4


class NativeStages:
    def __init__(self, service, timeout: float = 1800):
        self.service, self.timeout = service, timeout
        self.pending = {}

    async def wait(self, scope, request, deliverable=None):
        request_id = request["request_id"]
        stage = {"scope": scope, "request": request, "owner": None,
                 "execution_id": uuid4().hex, "begun": False,
                 "deliverable": deliverable if deliverable is not None else (lambda: True),
                 "future": asyncio.get_running_loop().create_future()}
        self.pending[request_id] = stage
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            while True:
                signal = self.service._signals.get(request["run_id"])
                if signal is not None and signal.is_set():
                    return {"status": "cancelled", "execution_id": stage["execution_id"]}
                if not stage["deliverable"]():
                    return {"status": "interrupted", "execution_id": stage["execution_id"]}
                if asyncio.get_running_loop().time() >= deadline:
                    return {"status": "failed", "execution_id": stage["execution_id"]}
                try:
                    result = await asyncio.wait_for(asyncio.shield(stage["future"]), .1)
                    # Cancellation can arrive while disconnect wakes the future.
                    if signal is not None and signal.is_set():
                        return {"status": "cancelled", "execution_id": stage["execution_id"]}
                    return result
                except asyncio.TimeoutError:
                    pass
        finally:
            self.pending.pop(request_id, None)
            if not stage["future"].done():
                stage["future"].cancel()

    def snapshot(self, scope):
        return [{"request": stage["request"], "claimed": stage["owner"] is not None}
                for stage in self.pending.values() if stage["scope"] == scope
                and stage["deliverable"]() and not stage["future"].done()]

    def claim(self, scope, request_id, owner):
        stage = self._active(scope, request_id)
        if stage["future"].done():
            return {"claimed": False}
        if stage["owner"] not in (None, owner):
            return {"claimed": False}
        stage["owner"] = owner
        return {"claimed": True, "execution_id": stage["execution_id"], "request": stage["request"]}

    def owned(self, scope, args, owner):
        stage = self._active(scope, args["request_id"])
        if stage["owner"] != owner or stage["execution_id"] != args.get("execution_id"):
            raise ValueError("stage belongs to another connection or execution round")
        return stage

    def begin(self, scope, args, owner):
        self.owned(scope, args, owner)["begun"] = True
        return {"accepted": True}

    def receipt(self, scope, args, owner):
        stage = self.owned(scope, args, owner)
        if stage["future"].done():
            raise ValueError("stage round has already ended")
        if not stage["begun"]:
            raise ValueError("stage round has not started")
        return self.service.stages.receipt(scope, args["request_id"], args["outcome"],
                                           args.get("summary", ""), args["execution_id"])

    def finish(self, scope, args, owner):
        stage = self.owned(scope, args, owner)
        if not stage["begun"] or args.get("status") not in {"completed", "failed", "aborted"}:
            raise ValueError("invalid stage round completion")
        result = {"status": args["status"], "execution_id": stage["execution_id"]}
        if stage["future"].done():
            if stage["future"].result() != result:
                raise ValueError("conflicting stage round completion")
        else:
            stage["future"].set_result(result)
        return {"accepted": True}

    def disconnect(self, owner, abandoned_scope=None):
        for stage in self.pending.values():
            unclaimed_in_closed_session = stage["owner"] is None and stage["scope"] == abandoned_scope
            if (stage["owner"] == owner or unclaimed_in_closed_session) and not stage["future"].done():
                stage["future"].set_result({"status": "interrupted", "execution_id": stage["execution_id"]})

    def _active(self, scope, request_id):
        stage = self.pending.get(request_id)
        if stage is None or stage["scope"] != scope or not stage["deliverable"]():
            raise ValueError("stage request is not active in this session")
        state = self.service.status(scope, stage["request"]["run_id"])
        if state["status"] != "running":
            raise ValueError("stage run is no longer active")
        return stage
