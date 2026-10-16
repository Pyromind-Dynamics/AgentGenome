"""Transport-independent operations shared by Pi and embedded hosts."""
from typing import Any

from .capture.align import DEFAULT_THRESHOLD
from .capture.distill import bindings, distill as distill_plan
from .capture.emit import Source
from .capture.pipeline import prepare_capture
from .capture.policy import POLICY_SCRIPTS_FIRST
from .capture.sample import compatibility, inspect
from .capture.trace import Trace, capture_trace
from .manifest import MATCH_KEYS
from .service import GenomeService


def invoke(service: GenomeService, scope: str, action: str,
           arguments: dict[str, Any], request_id: str, *,
           capabilities: set[str] | None = None, host: Any = None) -> dict[str, Any]:
    capabilities = capabilities or set()
    if action == "list":
        return {"assets": [{**asset,
                            "has_samples": service.captures.has_samples(
                                asset["id"], asset["version"])}
                           for asset in service.list()]}
    if action == "capture":
        return _capture(service, scope, arguments, host)
    if action == "distill":
        return _distill(service, scope, arguments, host)
    if action == "capture_status":
        return service.captures.status(str(arguments["capture_id"]))
    if action == "get":
        asset = service.get(str(arguments["asset_id"]), str(arguments["version"]))
        if not asset["published"]:
            raise ValueError("asset is not published")
        return {**asset,
                "revision_availability": service.revisions.availability(asset, capabilities),
                "input_samples": service.captures.sample_preview(
                    asset["id"], asset["version"])}
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
        asset_id, version = str(arguments["asset_id"]), str(arguments["version"])
        service.check_capabilities(asset_id, version, capabilities)
        report = _gate(service, host, asset_id, version, params,
                       allow=arguments.get("allow_mismatch") is True)
        state = service.submit(scope, request_id, asset_id, version, params)
        if report["problems"] or report["warnings"]:
            state["compatibility"] = report
        return state
    if action == "takeover":
        return service.revisions.takeover(scope, str(arguments["run_id"]), str(arguments["reason"]))
    if action == "step_result":
        return service.stages.receipt(scope, str(arguments["request_id"]),
                                      str(arguments["outcome"]), str(arguments.get("summary", "")),
                                      str(arguments.get("execution_id", "")))
    if action == "status":
        state = service.status(scope, str(arguments["run_id"]))
        return {**state, "recovery": service.revisions.recovery(state, capabilities)}
    if action == "resume":
        return service.resume(scope, str(arguments["run_id"]))
    if action == "cancel":
        return getattr(service, action)(scope, str(arguments["run_id"]))
    raise ValueError("unknown workflow action")


def _text(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _observed(host: Any, asset: dict[str, Any], params: dict[str, Any]
              ) -> list[tuple[str, dict[str, Any]]]:
    """Sample this run's path inputs, so the gate compares real files, not names."""
    observed = []
    for key, spec in (asset.get("parameters") or {}).items():
        value = params.get(key)
        if spec.get("type") != "path" or not isinstance(value, str) or not value:
            continue
        if value.startswith("fixture:"):
            continue
        observed.append((key, inspect(host, value)))
    return observed


def _gate(service: GenomeService, host: Any, asset_id: str, version: str,
          params: dict[str, Any], *, allow: bool) -> dict[str, Any]:
    """Mechanical reuse gate: this run's inputs must look like the captured ones.

    Format and required fields are hard stops only while the caller keeps the
    promise to reuse the experience as captured; ``allow_mismatch`` records the
    drift in the run instead of stopping it.
    """
    asset = service.get(asset_id, version)
    effective = service.validate_params(asset, params)
    report = compatibility(service.captures.sample_index(asset_id, version),
                           asset.get("input_contract"),
                           _observed(host, asset, effective))
    if report["problems"] and not allow:
        raise ValueError(
            "输入与这条经验的契约不符：" + "；".join(report["problems"])
            + "。确认要按现状强行复用就带 allow_mismatch=true；"
            "否则按本次输入重新沉淀一条经验。")
    report["forced"] = bool(allow and report["problems"])
    return report


def _declarations(arguments: dict[str, Any]) -> dict[str, Any]:
    """Applicability and contract fields shared by capture and distill."""
    return {key: arguments[key] for key in MATCH_KEYS
            if arguments.get(key) is not None}


def _traces(host: Any, refs: list[Any]) -> list[Trace]:
    traces = []
    for ref in refs:
        path = ref.get("path") if isinstance(ref, dict) else ref
        if not isinstance(path, str) or not path:
            raise ValueError("every trace_ref needs a path")
        try:
            text = host.read(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"trace is not UTF-8 text: {path}") from exc
        traces.append(capture_trace(text, source=path))
    return traces


def _capture(service: GenomeService, scope: str, arguments: dict[str, Any],
             host: Any) -> dict[str, Any]:
    if not isinstance(host, Source):
        raise ValueError("capture needs a host that can read the exported traces")
    refs = arguments.get("trace_refs")
    if not isinstance(refs, list) or not refs:
        raise ValueError("capture needs trace_refs exported by genome_trace")
    label = _text(arguments, "label")
    if label is None:
        raise ValueError("capture needs a label")
    new_params = arguments.get("new_params")
    if new_params is not None and not isinstance(new_params, dict):
        raise ValueError("new_params must be an object")
    plan = prepare_capture(
        service, traces=_traces(host, refs), label=label, workspace=host, scope=scope,
        final_artifact=_text(arguments, "final_artifact"),
        threshold=float(arguments.get("min_support") or DEFAULT_THRESHOLD),
        new_params=new_params, owner=str(arguments.get("owner") or "local"),
        description=_text(arguments, "description"), version=_text(arguments, "version"),
        asset_id=_text(arguments, "asset_id"), declarations=_declarations(arguments))
    return plan.payload()


def _distill(service: GenomeService, scope: str, arguments: dict[str, Any],
             host: Any) -> dict[str, Any]:
    if not isinstance(host, Source):
        raise ValueError("distill needs a host that can read the workspace scripts")
    label = _text(arguments, "label")
    if label is None:
        raise ValueError("distill needs a label")
    steps = arguments.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("distill needs steps: [{run, from?}] in chain order")
    sources = arguments.get("source_traces") or []
    if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
        raise ValueError("source_traces must be a list of trace ids")
    refs = arguments.get("trace_refs") or []
    if not isinstance(refs, list):
        raise ValueError("trace_refs must be a list of exported trace paths")
    traces = _traces(host, refs) if refs else []
    params = arguments.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    new_params = arguments.get("new_params")
    if new_params is not None and not isinstance(new_params, dict):
        raise ValueError("new_params must be an object")
    threshold = float(arguments.get("min_support") or DEFAULT_THRESHOLD)
    policy = _text(arguments, "policy") or POLICY_SCRIPTS_FIRST
    plan = distill_plan(
        service, label=label, steps=steps, workspace=host,
        final_artifact=_text(arguments, "final_artifact"),
        asset_id=_text(arguments, "asset_id"),
        description=_text(arguments, "description"),
        source_traces=sources, owner=str(arguments.get("owner") or "local"),
        version=_text(arguments, "version"),
        threshold=threshold, declarations=_declarations(arguments),
        traces=traces, params=params, policy=policy,
        review_confirmed=arguments.get("review_confirmed") is True,
        review_reason=_text(arguments, "review_reason"))
    capture = prepare_capture(
        service, traces=[plan.trace], label=label, workspace=host, scope=scope,
        # The distilled trace names its artifacts with the plan's own tokens, so the
        # capture stage slices on the token this plan resolved, not on the spelling
        # the agent used when it declared the final artifact.
        final_artifact=plan.package.final_artifact, threshold=threshold,
        new_params=new_params, owner=str(arguments.get("owner") or "local"),
        description=_text(arguments, "description"), version=_text(arguments, "version"),
        asset_id=_text(arguments, "asset_id"), declarations=_declarations(arguments),
        replay_params=dict(plan.bound), bindings=bindings(params))
    return {**capture.payload(), "distilled": plan.summary()}
