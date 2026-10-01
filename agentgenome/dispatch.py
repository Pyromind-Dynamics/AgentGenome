"""Transport-independent operations shared by Pi and embedded hosts."""
from typing import Any

from .service import GenomeService


def invoke(service: GenomeService, scope: str, action: str,
           arguments: dict[str, Any], request_id: str, *,
           capabilities: set[str] | None = None, host: Any = None) -> dict[str, Any]:
    capabilities = capabilities or set()
    if action == "list":
        return {"assets": service.list()}
    if action == "get":
        asset = service.get(str(arguments["asset_id"]), str(arguments["version"]))
        if not asset["published"]:
            raise ValueError("asset is not published")
        return {**asset, "revision_availability": service.revisions.availability(asset, capabilities)}
    if action == "prepare_revision" or (action == "run" and "revision_id" in arguments):
        if host is None or "revision" not in capabilities:
            raise ValueError("host cannot prepare and collect revision files; revision disabled. "
                             "Keep verification unchanged; use genome_takeover after the run stops "
                             "to finish the remaining task with Skills.")
    if action == "prepare_revision":
        return service.revisions.prepare(scope, str(arguments["run_id"]), request_id, host, arguments.get("reason"), arguments.get("editable_scripts"), arguments.get("verification_resources"))
    if action == "run":
        params = arguments.get("params")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if "revision_id" in arguments:
            if "asset_id" in arguments:
                raise ValueError("original asset and revision are mutually exclusive")
            return service.revisions.submit(scope, request_id, str(arguments["revision_id"]),
                                            params, arguments.get("change_summary"), host,
                                            arguments.get("parameter_patch"), capabilities, arguments.get("baseline_cases"), arguments.get("regression_cases"), arguments.get("version"))
        if "change_summary" in arguments or "parameter_patch" in arguments:
            raise ValueError("revision fields require revision_id")
        service.check_capabilities(str(arguments["asset_id"]), str(arguments["version"]), capabilities)
        return service.submit(scope, request_id, str(arguments["asset_id"]),
                              str(arguments["version"]), params)
    if action == "takeover":
        return service.revisions.takeover(scope, str(arguments["run_id"]), str(arguments["reason"]))
    if action == "step_result":
        return service.stages.receipt(scope, str(arguments["request_id"]),
                                      str(arguments["outcome"]), str(arguments.get("summary", "")),
                                      str(arguments.get("execution_id", "")))
    if action == "status":
        state = service.status(scope, str(arguments["run_id"]))
        return {**state, "recovery": service.revisions.recovery(state, capabilities)}
    if action == "cancel":
        return getattr(service, action)(scope, str(arguments["run_id"]))
    raise ValueError("unknown workflow action")
