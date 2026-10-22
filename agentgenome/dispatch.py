"""Transport-independent operations shared by Pi and embedded hosts."""
import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .capture.align import DEFAULT_THRESHOLD
from .capture.distill import (
    DistillError,
    bindings,
    distill as distill_plan,
    draft_distill,
    preflight_matches_draft,
)
from .capture.emit import Source
from .capture.pipeline import prepare_capture
from .capture.plan import build_candidate_plan
from .capture.policy import POLICY_SCRIPTS_FIRST
from .capture.sample import compatibility, inspect
from .capture.trace import Trace, capture_trace
from .manifest import MATCH_KEYS
from .service import GenomeService


class _Clock:
    """Per-phase wall-clock marks for one tool call.

    Planning itself is milliseconds of CPU, so when a distill call takes minutes
    the time is outside this module: reading the exported trace, materializing
    scripts, or waiting on the host bridge. Marks are relative to the previous
    mark, so the dict reads as a breakdown and the slow phase is the large one.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()
        self.marks: dict[str, int] = {}

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.marks[name] = int(round((now - self._last) * 1000))
        self._last = now

    def payload(self) -> dict[str, Any]:
        return {**self.marks, "total_ms": sum(self.marks.values())}


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
        return _capture(service, scope, arguments, host, request_id)
    if action == "prepare_distill":
        return _prepare_distill(service, scope, arguments, host, request_id)
    if action == "distill":
        return _prepare_distill(service, scope, arguments, host, request_id)
    if action == "confirm_distill":
        return _confirm_distill(service, scope, arguments, host)
    if action == "capture_status":
        return service.captures.status(str(arguments["capture_id"]))
    if action == "prepare_run":
        return _prepare_run(
            service,
            scope,
            request_id,
            arguments,
            host,
            capabilities,
        )
    if action == "confirm_run":
        return _confirm_run(service, scope, arguments, host, capabilities)
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
        if "plan_id" in arguments:
            return _confirm_run(service, scope, arguments, host, capabilities)
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
        return _prepare_run(
            service,
            scope,
            request_id,
            arguments,
            host,
            capabilities,
        )
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
        return service.resume(scope, str(arguments["run_id"]),
                              _artifact(arguments.get("artifact")))
    if action == "replay_requeue":
        return service.replay_requeue(scope, str(arguments["run_id"]),
                                      _artifact(arguments.get("artifact")))
    if action == "cancel":
        return getattr(service, action)(scope, str(arguments["run_id"]))
    raise ValueError("unknown workflow action")


def _artifact(value: Any) -> str | None:
    """The path a person exported to, handed over so the host can land it."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("artifact must be a non-empty path string")
    return value


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


def _prepare_run(
    service: GenomeService,
    scope: str,
    request_id: str,
    arguments: dict[str, Any],
    host: Any,
    capabilities: set[str],
) -> dict[str, Any]:
    params = arguments.get("params")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    asset_id, version = str(arguments["asset_id"]), str(arguments["version"])
    asset = service.get(asset_id, version)
    capture = None
    if not asset["published"]:
        capture = service.captures.for_asset(asset_id, version)
        if capture is None:
            raise ValueError("asset is not published and has no draft capture to replay")
    if asset["published"]:
        service.check_capabilities(asset_id, version, capabilities)
    report = _gate(
        service,
        host,
        asset_id,
        version,
        params,
        allow=arguments.get("allow_mismatch") is True,
    )
    scope_request = arguments.get("execution_scope")
    if scope_request is not None and not isinstance(scope_request, dict):
        raise ValueError("execution_scope must be an object")
    plan = service.plans.prepare(
        scope=scope,
        request_id=request_id,
        kind="asset" if asset["published"] else "capture",
        asset_id=asset_id,
        version=version,
        params=params,
        source=host,
        requested_scope=scope_request,
        compatibility=report,
        capture_id=capture["capture_id"] if capture else None,
        allow_mismatch=arguments.get("allow_mismatch") is True,
    )
    return {
        **plan,
        "status": "planned",
        "requires_confirmation": True,
        "instruction": (
            "Show the analysed inputs, graph nodes, side effects, and exact "
            "execution scope to the user. Submit with genome_confirm_run only "
            "after the user explicitly confirms this plan."
        ),
    }


def _confirm_run(
    service: GenomeService,
    scope: str,
    arguments: dict[str, Any],
    host: Any,
    capabilities: set[str],
) -> dict[str, Any]:
    plan_id = str(arguments.get("plan_id") or "")
    if not plan_id:
        raise ValueError("plan_id is required for execution")
    plan = service.plans.get(scope, plan_id)
    if plan["status"] != "confirmed":
        asset = service.get(plan["asset_id"], plan["version"])
        if asset["published"]:
            service.check_capabilities(plan["asset_id"], plan["version"], capabilities)
        report = _gate(
            service,
            host,
            plan["asset_id"],
            plan["version"],
            plan["params"],
            allow=plan["allow_mismatch"],
        )
    else:
        report = None
    return service.plans.confirm(
        scope=scope,
        plan_id=plan_id,
        source=host,
        digest=_text(arguments, "digest"),
        compatibility=report,
    )


def _traces(host: Any, refs: list[Any]) -> list[Trace]:
    return _traces_with_evidence(host, refs)[0]


def _traces_with_evidence(
    host: Any,
    refs: list[Any],
) -> tuple[list[Trace], list[tuple[str, str]]]:
    traces = []
    evidence: list[tuple[str, str]] = []
    for ref in refs:
        path = ref.get("path") if isinstance(ref, dict) else ref
        if not isinstance(path, str) or not path:
            raise ValueError("every trace_ref needs a path")
        try:
            content = host.read(path)
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"trace is not UTF-8 text: {path}") from exc
        traces.append(capture_trace(text, source=path))
        evidence.append((path, hashlib.sha256(content).hexdigest()))
    return traces, evidence


def _capture(service: GenomeService, scope: str, arguments: dict[str, Any],
             host: Any, request_id: str) -> dict[str, Any]:
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
    scope_request = arguments.get("execution_scope")
    if scope_request is not None and not isinstance(scope_request, dict):
        raise ValueError("execution_scope must be an object")
    plan = prepare_capture(
        service, traces=_traces(host, refs), label=label, workspace=host, scope=scope,
        final_artifact=_text(arguments, "final_artifact"),
        threshold=float(arguments.get("min_support") or DEFAULT_THRESHOLD),
        new_params=new_params, owner=str(arguments.get("owner") or "local"),
        description=_text(arguments, "description"), version=_text(arguments, "version"),
        asset_id=_text(arguments, "asset_id"), declarations=_declarations(arguments),
        request_id=request_id, execution_scope=scope_request)
    return plan.payload()


@dataclass(frozen=True)
class _DistillInputs:
    label: str
    steps: list[Any] | None
    source_traces: list[str]
    trace_refs: list[Any]
    params: Mapping[str, Any] | None
    new_params: Mapping[str, Any] | None
    validation_params: Mapping[str, Any] | None
    execution_scope: Mapping[str, Any] | None
    threshold: float
    policy: str
    arguments: dict[str, Any]

    @property
    def minimal_validation(self) -> bool:
        return (
            self.validation_params is not None
            and isinstance(self.execution_scope, dict)
            and self.execution_scope.get("mode") in {"sample", "subset"}
        )


@dataclass(frozen=True)
class _DistillArguments:
    plan: Any
    inputs: _DistillInputs

    @property
    def label(self) -> str:
        return self.inputs.label

    @property
    def params(self) -> Mapping[str, Any] | None:
        return self.inputs.params

    @property
    def new_params(self) -> Mapping[str, Any] | None:
        return self.inputs.new_params

    @property
    def validation_params(self) -> Mapping[str, Any] | None:
        return self.inputs.validation_params

    @property
    def execution_scope(self) -> Mapping[str, Any] | None:
        return self.inputs.execution_scope

    @property
    def threshold(self) -> float:
        return self.inputs.threshold

    @property
    def policy(self) -> str:
        return self.inputs.policy

    @property
    def arguments(self) -> dict[str, Any]:
        return self.inputs.arguments

    @property
    def minimal_validation(self) -> bool:
        return self.inputs.minimal_validation


def _distill_inputs(arguments: dict[str, Any]) -> _DistillInputs:
    label = _text(arguments, "label")
    if label is None:
        raise ValueError("distill needs a label")
    steps = arguments.get("steps")
    if steps is not None and not isinstance(steps, list):
        raise ValueError("distill steps must be a list when provided")
    if isinstance(steps, list) and not steps:
        steps = None
    sources = arguments.get("source_traces") or []
    if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
        raise ValueError("source_traces must be a list of trace ids")
    refs = arguments.get("trace_refs") or []
    if not isinstance(refs, list):
        raise ValueError("trace_refs must be a list of exported trace paths")
    params = arguments.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    new_params = arguments.get("new_params")
    if new_params is not None and not isinstance(new_params, dict):
        raise ValueError("new_params must be an object")
    validation_params = arguments.get("validation_params")
    if validation_params is not None and not isinstance(validation_params, dict):
        raise ValueError("validation_params must be an object")
    scope_request = arguments.get("execution_scope")
    if scope_request is not None and not isinstance(scope_request, dict):
        raise ValueError("execution_scope must be an object")
    # Validation replay is the main-trajectory check: run the chain again on the
    # inputs it was captured from. Making the caller restate those inputs bought
    # nothing but a blocking round, so an omitted `validation_params` defaults to
    # `params` and the scope defaults to a bounded sample of them.
    if not validation_params and isinstance(params, dict) and params:
        validation_params = dict(params)
        if scope_request is None:
            scope_request = {
                "mode": "sample",
                "description": "用原参数回放原链路（原始规模）",
            }
    threshold = float(arguments.get("min_support") or DEFAULT_THRESHOLD)
    policy = _text(arguments, "policy") or POLICY_SCRIPTS_FIRST
    return _DistillInputs(
        label=label,
        steps=steps,
        source_traces=sources,
        trace_refs=refs,
        params=params,
        new_params=new_params,
        validation_params=validation_params,
        execution_scope=scope_request,
        threshold=threshold,
        policy=policy,
        arguments=dict(arguments),
    )


def _distill_plan_from_inputs(
    service: GenomeService,
    inputs: _DistillInputs,
    host: Any,
    traces: list[Trace],
) -> _DistillArguments:
    if not isinstance(host, Source):
        raise ValueError("distill needs a host that can read the workspace scripts")
    if inputs.steps is None:
        raise ValueError("distill needs a resolved candidate or explicit steps")
    plan = distill_plan(
        service, label=inputs.label, steps=inputs.steps, workspace=host,
        final_artifact=_text(inputs.arguments, "final_artifact"),
        asset_id=_text(inputs.arguments, "asset_id"),
        description=_text(inputs.arguments, "description"),
        source_traces=inputs.source_traces,
        owner=str(inputs.arguments.get("owner") or "local"),
        version=_text(inputs.arguments, "version"),
        threshold=inputs.threshold, declarations=_declarations(inputs.arguments),
        traces=traces, params=inputs.params, policy=inputs.policy,
        review_confirmed=inputs.arguments.get("review_confirmed") is True,
        review_reason=_text(inputs.arguments, "review_reason"))
    return _DistillArguments(plan=plan, inputs=inputs)


def _load_distill_traces(
    inputs: _DistillInputs,
    host: Any,
) -> tuple[list[Trace], list[tuple[str, str]]]:
    return (
        _traces_with_evidence(host, inputs.trace_refs)
        if inputs.trace_refs
        else ([], [])
    )


def _distill_draft_from_inputs(
    inputs: _DistillInputs,
    traces: list[Trace],
    evidence: list[tuple[str, str]],
    host: Any,
) -> Any:
    if inputs.steps is None:
        raise ValueError("distill needs a resolved candidate or explicit steps")
    draft = draft_distill(
        label=inputs.label,
        steps=inputs.steps,
        source_traces=inputs.source_traces,
        traces=traces,
        final_artifact=_text(inputs.arguments, "final_artifact"),
        params=inputs.params,
        policy=inputs.policy,
        trace_evidence=evidence,
        input_contract=_declarations(inputs.arguments).get("input_contract"),
        workspace=host,
    )
    return draft


def _replay_params(
    plan: Any, validation_params: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Replay A's parameters: the full bound set, with validation values overriding.

    `validation_params` is an override, not a replacement. An agent that only wants
    to shrink `records` for the probe still needs every other root input bound;
    treating it as a replacement dropped the rest, so replay reported "needs 3,
    received ['records']" while the agent's own `params` looked complete.
    """
    bound = dict(plan.bound)
    if validation_params is None:
        return bound
    bound.update(
        {key: value for key, value in validation_params.items() if key in bound}
    )
    return bound


def _capture_distilled(
    service: GenomeService,
    scope: str,
    parsed: _DistillArguments,
    host: Any,
    *,
    request_id: str,
    capture_id: str | None = None,
) -> tuple[Any, Any]:
    plan = parsed.plan
    capture = prepare_capture(
        service, traces=[plan.trace], label=parsed.label, workspace=host, scope=scope,
        capture_id=capture_id,
        # The distilled trace names its artifacts with the plan's own tokens, so the
        # capture stage slices on the token this plan resolved, not on the spelling
        # the agent used when it declared the final artifact.
        final_artifact=plan.package.final_artifact, threshold=parsed.threshold,
        new_params=parsed.new_params,
        owner=str(parsed.arguments.get("owner") or "local"),
        description=_text(parsed.arguments, "description"),
        version=_text(parsed.arguments, "version"),
        asset_id=_text(parsed.arguments, "asset_id"),
        declarations=_declarations(parsed.arguments),
        replay_params=_replay_params(plan, parsed.validation_params),
        bindings=bindings(parsed.params),
        request_id=request_id,
        execution_scope=parsed.execution_scope,
        enqueue=parsed.minimal_validation,
        allow_side_effects=parsed.minimal_validation,
    )
    return plan, capture


def _distill_review(
    analysis: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    """A stable, user-facing plan that does not need another open-ended analysis."""
    nodes = {item["step"]: item for item in analysis.get("nodes", [])}
    steps = []
    for item in analysis.get("steps", []):
        node = nodes.get(item["step"], {})
        steps.append(
            {
                "step": item["step"],
                "kind": item["kind"],
                "source": item.get("from"),
                "inputs": item.get("reads", []),
                "outputs": item.get("writes", []),
                "command": item.get("run") or item.get("tool"),
                "keep": item.get("keep") is True,
                "node_id": node.get("id"),
            }
        )
    human_gates = [
        {
            "step": item["step"],
            "node_id": item["id"],
            "work_order": item["command"],
        }
        for item in analysis.get("nodes", [])
        if item.get("kind") == "human"
    ]
    source = analysis.get("chain", {}).get("traces") or [analysis.get("trace_id")]
    return {
        "source_traces": source,
        "chain_checked": bool(analysis.get("chain", {}).get("checked")),
        "steps": steps,
        "scripts": analysis.get("scripts", []),
        "branches": analysis.get("branches", []),
        "human_gates": human_gates,
        "final_artifact": analysis.get("final_artifact"),
        "not_solidifiable": analysis.get("not_solidifiable", []),
        "validation": validation,
    }


def _distill_validation(
    parsed: _DistillArguments | _DistillInputs,
) -> dict[str, Any]:
    return {
        "mode": (
            parsed.execution_scope.get("mode")
            if isinstance(parsed.execution_scope, dict)
            else "unspecified"
        ),
        "description": (
            parsed.execution_scope.get("description")
            if isinstance(parsed.execution_scope, dict)
            else None
        ),
        "params": dict(parsed.validation_params or {}),
        "will_start_replay": parsed.minimal_validation,
        "ready": parsed.minimal_validation,
        "required": True,
    }


def _dry_build_problems(
    service: GenomeService,
    inputs: _DistillInputs,
    host: Any,
    traces: list[Trace],
    arguments: dict[str, Any],
) -> list[str]:
    """Build the package now and report what the compiler says.

    Planning used to answer with static opinions; the first real answer — can this
    chain be built at all — only arrived at confirmation, so every wrong plan cost
    a round trip. Build into a temp directory and hand the real errors back in the
    same response. Nothing is published and no replay is queued.
    """
    if inputs.steps is None:
        return []
    try:
        distill_plan(
            service, label=inputs.label, steps=inputs.steps, workspace=host,
            final_artifact=_text(arguments, "final_artifact"),
            asset_id=_text(arguments, "asset_id"),
            description=_text(arguments, "description"),
            source_traces=inputs.source_traces,
            owner=str(arguments.get("owner") or "local"),
            version=_text(arguments, "version"),
            threshold=inputs.threshold,
            declarations=_declarations(arguments),
            traces=traces, params=inputs.params, policy=inputs.policy,
            review_confirmed=arguments.get("review_confirmed") is True,
            review_reason=_text(arguments, "review_reason"),
            dry_run=True,
        )
    except DistillError as exc:
        return [str(exc)]
    except (ValueError, OSError) as exc:
        return [str(exc)]
    return []


def _prepare_distill(
    service: GenomeService,
    scope: str,
    arguments: dict[str, Any],
    host: Any,
    request_id: str,
) -> dict[str, Any]:
    if not isinstance(host, Source):
        raise ValueError("distill needs a host that can read the source traces")
    clock = _Clock()
    inputs = _distill_inputs(arguments)
    clock.mark("inputs")
    traces, evidence = _load_distill_traces(inputs, host)
    clock.mark("traces")
    planning: dict[str, Any] = {
        "mode": "provided_steps" if inputs.steps is not None else "mechanical_candidate",
    }
    resolved_arguments = dict(arguments)
    if inputs.steps is None:
        if not traces:
            raise ValueError(
                "distill needs source_traces or trace_refs to mechanically "
                "extract steps, or explicit steps"
            )
        candidate = build_candidate_plan(
            traces,
            final_artifact=_text(arguments, "final_artifact"),
            params=inputs.params,
        )
        if not candidate.steps:
            raise ValueError(
                "could not mechanically extract a replayable chain from the trace; "
                "inspect the trace, then call genome_distill again with corrected steps"
            )
        resolved_arguments = candidate.arguments(arguments)
        if not resolved_arguments.get("source_traces"):
            resolved_arguments["source_traces"] = [trace.trace_id for trace in traces]
        inputs = _distill_inputs(resolved_arguments)
        planning.update({
            "candidate_step_count": len(candidate.steps),
            "excluded": list(candidate.branches),
            "ambiguities": list(candidate.ambiguities),
            "generated_receipts": list(candidate.generated_receipts),
        })
    clock.mark("candidate")
    draft = _distill_draft_from_inputs(inputs, traces, evidence, host)
    clock.mark("draft")
    validation = _distill_validation(inputs)
    review = draft.review(validation)
    review["planning"] = planning
    clock.mark("review")
    # Build it now. "Can this chain be built at all" is the one answer the static
    # rules could only guess at, and confirming was the first place a plan heard
    # the truth — one wasted round per wrong plan. The build runs the chain and
    # artifact checks itself, so they are not repeated here.
    dry = _dry_build_problems(service, inputs, host, traces, resolved_arguments)
    if dry:
        review["blocking_issues"] = list(dict.fromkeys([*review["blocking_issues"], *dry]))
    review["validation"]["ready"] = not review["blocking_issues"]
    clock.mark("dry_build")
    analysis = {
        "status": "structural_plan",
        "planning": planning,
        "draft": {
            **draft.payload(validation),
            "review": review,
        },
        "review": review,
    }
    state = service.distill_plans.create(
        scope=scope,
        request_id=request_id,
        arguments=resolved_arguments,
        analysis=analysis,
    )
    clock.mark("persist")
    if state.get("repeated"):
        # Nothing changed since the plan already on the table: hand back that one
        # so the caller sees the same problems instead of a fresh round.
        stored = state["analysis"].get("review") or {}
        return {
            "plan_id": state["plan_id"],
            "digest": state["digest"],
            "status": "planned",
            "requires_confirmation": True,
            "planning": state["analysis"].get("planning"),
            "review": stored,
            "blocking_issues": stored.get("blocking_issues") or [],
            "timings": clock.payload(),
            "repeated": True,
        }
    return {
        "plan_id": state["plan_id"],
        "digest": state["digest"],
        "status": "planned",
        "requires_confirmation": True,
        "planning": planning,
        "review": review,
        "blocking_issues": review["blocking_issues"],
        # Timings never enter `analysis`: the plan digest is computed over it, and
        # a clock reading would make every retry look like a different plan.
        "timings": clock.payload(),
    }


def _confirm_distill(
    service: GenomeService,
    scope: str,
    arguments: dict[str, Any],
    host: Any,
) -> dict[str, Any]:
    plan_id = _text(arguments, "plan_id")
    if plan_id is None:
        raise ValueError("plan_id is required for distillation")
    digest = _text(arguments, "digest")
    planned = service.distill_plans.get(scope, plan_id)
    blocking = (
        planned.get("analysis", {})
        .get("review", {})
        .get("blocking_issues", [])
    )
    if blocking:
        raise ValueError(
            "distillation plan has blocking issues; fix them and call "
            "genome_distill again: " + "; ".join(str(item) for item in blocking)
        )
    state = service.distill_plans.begin_confirmation(
        scope,
        plan_id,
        digest=digest,
    )
    if state["status"] == "confirmed":
        if state["result"] is None:
            raise ValueError("confirmed distillation plan has no stored result")
        return state["result"]
    try:
        clock = _Clock()
        if "draft" not in state["analysis"]:
            raise ValueError(
                "this distillation plan uses an older format; "
                "call genome_distill again and show the structural plan to the user"
            )
        if not isinstance(host, Source):
            raise ValueError("distill needs a host that can read the source traces")
        inputs = _distill_inputs(state["arguments"])
        if not inputs.minimal_validation:
            raise ValueError(
                "confirm_distill requires a bounded validation replay: "
                "provide validation_params together with execution_scope.mode=sample|subset; "
                "full and unspecified scopes are not valid automatic validation"
            )
        traces, evidence = _load_distill_traces(inputs, host)
        clock.mark("traces")
        draft = _distill_draft_from_inputs(inputs, traces, evidence, host)
        clock.mark("draft")
        stored_digest = state["analysis"]["draft"].get("digest")
        if draft.digest != stored_digest:
            raise ValueError(
                "source traces or the structural plan changed after review; "
                "call genome_distill again, show the new plan, and ask the user to confirm it"
            )
        parsed = _distill_plan_from_inputs(service, inputs, host, traces)
        clock.mark("preflight")
        matches, difference = preflight_matches_draft(draft, parsed.plan)
        if not matches:
            raise ValueError(
                "preflight changed the reviewed plan"
                + (f" ({difference})" if difference else "")
                + "; call genome_distill again, show the new plan, and ask the user to confirm it"
            )
        summary = parsed.plan.summary()
        analysis = {
            **summary,
            "review": _distill_review(summary, _distill_validation(parsed)),
        }
        _, capture = _capture_distilled(
            service,
            scope,
            parsed,
            host,
            request_id=f"distill-plan:{plan_id}",
            capture_id=f"distill-{plan_id}",
        )
        clock.mark("capture")
        # No `persist` mark here: a repeated confirmation answers from the stored
        # result, and a post-write reading would make the two answers differ.
        result = {
            **capture.payload(),
            "preflight": analysis,
            "distilled": analysis,
            "review": analysis.get("review"),
            "distill_plan": {
                # Echo the stored id, not the caller's spelling: a dashed UUID
                # still names this plan, and the next call should not have to.
                "plan_id": state["plan_id"],
                "digest": state["digest"],
                "status": "confirmed",
            },
            "timings": clock.payload(),
        }
        confirmed = service.distill_plans.mark_confirmed(
            scope,
            plan_id,
            result=result,
            capture_id=capture.capture_id,
            run_id=capture.run_id,
        )
    except Exception:
        service.distill_plans.release_confirmation(scope, plan_id)
        raise
    # `result` and the stored row carry the same breakdown, so a repeated
    # confirmation answers identically.
    return confirmed["result"] if confirmed["result"] is not None else result
