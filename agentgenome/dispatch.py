"""Transport-independent operations shared by Pi and embedded hosts."""
from typing import Any

from .service import GenomeService


def invoke(service: GenomeService, scope: str, action: str,
           arguments: dict[str, Any], request_id: str) -> dict[str, Any]:
    if action == "list":
        return {"assets": service.list()}
    if action == "get":
        asset = service.get(str(arguments["asset_id"]), str(arguments["version"]))
        if not asset["published"]:
            raise ValueError("asset is not published")
        return asset
    if action == "run":
        params = arguments.get("params")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        return service.submit(scope, request_id, str(arguments["asset_id"]),
                              str(arguments["version"]), params)
    if action in {"status", "cancel"}:
        return getattr(service, action)(scope, str(arguments["run_id"]))
    raise ValueError("unknown workflow action")
