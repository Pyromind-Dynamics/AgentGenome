import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import yaml

from agentgenome import paths
from agentgenome.capture.distill import (
    KEEP_REASON,
    DistillError,
    _intermediate_token_issues,
    _meaningful_shell,
    _template_path_issues,
    distill,
    draft_distill,
    plan_steps,
    preflight_matches_draft,
)
from agentgenome.capture.emit import _Plan
from agentgenome.capture.replay import values as replay_values
from agentgenome.capture.trace import KIND_SHELL, Step, canonical_path
from agentgenome.dispatch import _replay_params, invoke
from agentgenome.local_host import LocalExecutionHost
from core import Answer

FINAL = "reports/metrics.json"
MINIMAL_VALIDATION = {
    "execution_scope": {"mode": "sample", "description": "定向验证小样"},
    "validation_params": {"review": "out/review.jsonl"},
}
HUMAN_VALIDATION = {
    "execution_scope": {"mode": "sample", "description": "定向验证小样"},
    "validation_params": {
        "raw": "data/raw.csv",
        "rules": "rules.json",
    },
}
TO_SFT = '''import json, sys

with open(sys.argv[1]) as stream:
    rows = [json.loads(line) for line in stream if line.strip()]
with open(sys.argv[2], "w") as stream:
    for row in rows:
        stream.write(json.dumps({"messages": [{"role": "system", "content": "s"},
                                              {"role": "user", "content": row["text"]},
                                              {"role": "assistant", "content": "a"}]},
                                ensure_ascii=False) + "\\n")
'''
TO_MESSAGES = '''import argparse, json

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
with open(args.input) as stream:
    rows = [json.loads(line) for line in stream if line.strip()]
with open(args.output, "w") as stream:
    for row in rows:
        stream.write(json.dumps({"messages": [
            {"role": "user", "content": row["user_prompt"]},
            {"role": "assistant", "content": row["gt"]}]}, ensure_ascii=False) + "\\n")
'''
CHAIN = [
    {"run": "python3 scripts/prelabel.py data/raw.csv rules.json > out/prelabeled.jsonl",
     "from": "s1:2"},
    {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv", "from": "s1:4"},
    {"run": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt",
     "from": "s1:5"},
    {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv > reports/metrics.json",
     "from": "s1:6"},
]


def distill_args(**overrides):
    return {
        "label": "预打标到评测",
        "asset_id": "prelabel-train-eval",
        "steps": CHAIN,
        "final_artifact": FINAL,
        **MINIMAL_VALIDATION,
        **overrides,
    }


def prepare(service, arguments, request_id="call-1", host=None):
    return invoke(
        service, "conv-1", "distill", arguments, request_id, host=host
    )


def confirm(service, plan, host, request_id="confirm-1"):
    result = invoke(
        service,
        "conv-1",
        "confirm_distill",
        {"plan_id": plan["plan_id"], "digest": plan["digest"]},
        request_id,
        host=host,
    )
    assert result["distill_plan"]["status"] == "confirmed"
    return result


def test_distill_rejects_a_flag_value_that_only_resolves_in_the_session_cwd(
        service, pipeline_workspace):
    (pipeline_workspace.root / "storage").mkdir()
    pipeline_workspace.write("scripts/to_sft.py", TO_SFT)
    with pytest.raises(DistillError) as error:
        distill(service, label="复核导出转 SFT", workspace=pipeline_workspace.root,
                final_artifact="out/sft.jsonl",
                steps=[{"run": "python3 scripts/to_sft.py out/review.jsonl "
                               "--out out/sft.jsonl --images-root storage"}])
    assert "--images-root storage" in str(error.value)
    assert "{images_root}" in str(error.value)


def test_distill_parameterizes_a_flag_value_that_points_into_the_workspace(
        service, pipeline_workspace):
    (pipeline_workspace.root / "storage").mkdir()
    pipeline_workspace.write("scripts/to_sft.py", TO_SFT)
    plan = distill(service, label="复核导出转 SFT", workspace=pipeline_workspace.root,
                   final_artifact="out/sft.jsonl",
                   params={"review": "out/review.jsonl", "images_root": "storage"},
                   steps=[{"run": "python3 scripts/to_sft.py out/review.jsonl "
                                  "--out out/sft.jsonl --images-root {images_root}"}])
    graph = (plan.package.directory / "graph.yaml").read_text(encoding="utf-8")
    assert "{input.images_root}" in graph
    assert "{input.review}" in graph


def test_distill_keeps_a_flag_value_the_workspace_does_not_have(
        service, pipeline_workspace):
    pipeline_workspace.write("scripts/to_sft.py", TO_SFT)
    plan = distill(service, label="复核导出转 SFT", workspace=pipeline_workspace.root,
                   final_artifact="out/sft.jsonl",
                   steps=[{"run": "python3 scripts/to_sft.py out/review.jsonl "
                                  "--out out/sft.jsonl --pipeline llm-filter"}])
    assert "--pipeline llm-filter" in plan.package.nodes[0].command


def test_distill_defaults_to_a_plan_without_queueing(
        service, host, pipeline_workspace):
    arguments = distill_args()
    arguments.pop("execution_scope")
    arguments.pop("validation_params")
    plan = prepare(service, arguments, "call-plan", host)
    assert plan["plan_id"]
    assert plan["status"] == "planned"
    assert plan["requires_confirmation"] is True
    assert "capture_id" not in plan
    assert "run_id" not in plan
    assert plan["review"]["validation"]["ready"] is False
    assert any(
        "bounded validation replay" in item
        for item in plan["blocking_issues"]
    )
    assert not (service.root / "captures").exists()


def test_distill_reports_where_the_time_went(service, host, pipeline_workspace):
    """A slow call must say which phase was slow.

    Planning itself is milliseconds of CPU, so a distill call that takes minutes
    spent the time in the host: reading the exported trace, or materializing
    scripts. The breakdown rides on the result, and stays out of the digest —
    otherwise every retry would look like a different plan.
    """
    arguments = distill_args()
    first = prepare(service, arguments, "call-timing-1", host)
    second = prepare(service, arguments, "call-timing-2", host)

    timings = first["timings"]
    assert set(timings) >= {
        "inputs", "traces", "candidate", "draft", "review", "persist", "total_ms",
    }
    assert all(isinstance(value, int) and value >= 0 for value in timings.values())
    assert timings["total_ms"] == sum(
        value for key, value in timings.items() if key != "total_ms"
    )
    assert "timings" not in first["review"]
    assert first["digest"] == second["digest"]


def test_distill_mechanically_extracts_a_candidate_when_steps_are_omitted(
        service, host, trace_refs):
    refs = trace_refs({"trace_id": "s1"})

    plan = prepare(
        service,
        {
            "label": "预打标到评测",
            **MINIMAL_VALIDATION,
            "source_traces": ["s1"],
            "trace_refs": refs,
        },
        "candidate-plan",
        host,
    )

    assert plan["planning"]["mode"] == "mechanical_candidate"
    assert plan["planning"]["candidate_step_count"] == 5
    assert [step["kind"] for step in plan["review"]["steps"]] == [
        "run",
        "human",
        "run",
        "run",
        "run",
    ]
    assert plan["review"]["final_artifact"] == "reports/metrics.json"
    assert plan["review"]["steps"][1]["outputs"] == ["out/review.jsonl"]
    stored = service.distill_plans.get("conv-1", plan["plan_id"])
    assert len(stored["arguments"]["steps"]) == 5


def test_a_numeric_limit_stays_numeric_through_distill_and_replay(
        service, pipeline_workspace):
    plan = distill(
        service,
        label="限制行数并训练",
        workspace=pipeline_workspace.root,
        steps=[
            {
                "run": (
                    "python3 scripts/train.py out/review.jsonl "
                    "--epochs {limit} --output reports/metrics.json"
                ),
            }
        ],
        final_artifact="reports/metrics.json",
        params={"review": "out/review.jsonl", "limit": "3"},
        declarations={
            "input_contract": {
                "limit": {
                    "type": "number",
                    "description": "最多训练的样本数",
                }
            }
        },
    )

    assert plan.package.parameter_specs["limit"].type == "number"
    assert dict(plan.bound)["limit"] == 3
    graph = yaml.safe_load(
        (plan.package.directory / "graph.yaml").read_text(encoding="utf-8")
    )
    assert graph["node"]["input"]["limit"]["type"] == "number"
    manifest = json.loads(
        (plan.package.directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["parameters"]["limit"]["type"] == "number"
    assert replay_values(
        plan.package, {"review": "out/review.jsonl", "limit": "3"}
    ) == {
        "review": "out/review.jsonl",
        "limit": 3,
    }


def test_a_numeric_placeholder_is_not_reviewed_as_a_path(
        pipeline_workspace):
    draft = draft_distill(
        label="限制行数并训练",
        steps=[
            {
                "run": (
                    "python3 scripts/train.py out/review.jsonl "
                    "--epochs {limit} --output reports/metrics.json"
                ),
            }
        ],
        final_artifact="reports/metrics.json",
        params={"limit": "3"},
        input_contract={"limit": {"type": "number"}},
        workspace=pipeline_workspace.root,
    )

    review = draft.review({"will_start_replay": True})

    assert "{limit}" not in review["steps"][0]["inputs"]
    assert "3" not in review["steps"][0]["inputs"]
    assert not any("`3`" in item for item in review["blocking_issues"])
    assert not any("`{limit}`" in item for item in review["unresolved"])


def test_a_parameter_name_must_not_encode_the_trial_size(pipeline_workspace):
    """`head -10` 试跑留下的输入，不能把模板固化成只处理十条。

    参数名只描述角色；条数属于执行范围，改输入时由 execution_scope 决定。
    """
    draft = draft_distill(
        label="复核十条后转 SFT",
        steps=[
            {
                "run": (
                    "python3 scripts/ls_to_sft.py out/review_10.jsonl "
                    "out/images reports/sft.jsonl"
                ),
            }
        ],
        final_artifact="reports/sft.jsonl",
        params={"review_10": "out/review_10.jsonl", "images": "out/images"},
        input_contract={
            "review_10": {"type": "path"},
            "images": {"type": "path"},
        },
        workspace=pipeline_workspace.root,
    )

    review = draft.review({"will_start_replay": True})

    # Advice, not a defect: the chain runs, it just bakes the trial size into a name.
    assert any("记录数" in item and "review_10" in item for item in review["warnings"])
    assert not any("记录数" in item and "images" in item for item in review["warnings"])


def test_static_review_skips_intermediate_artifacts_and_storage_aliases(
        pipeline_workspace):
    class MetadataHost:
        def __init__(self, root):
            self.root = root
            self.checked = []

        def read(self, path):
            return (self.root / path).read_bytes()

        def metadata(self, path):
            self.checked.append(path)
            target = self.root / path.removeprefix("storage/")
            if not target.exists():
                return {"exists": False}
            return {
                "exists": True,
                "is_file": target.is_file(),
                "is_dir": target.is_dir(),
                "size": target.stat().st_size,
            }

    host = MetadataHost(pipeline_workspace.root)
    draft = draft_distill(
        label="清洗两段",
        steps=[
            {
                "run": (
                    "python3 scripts/prelabel.py data/raw.csv rules.json "
                    "> storage/datasets/tmp/intermediate.jsonl"
                ),
            },
            {
                "run": (
                    "python3 scripts/clean.py "
                    "datasets/tmp/intermediate.jsonl "
                    "storage/datasets/tmp/final.csv"
                ),
                "output": "storage/datasets/tmp/final.csv",
            },
        ],
        final_artifact="storage/datasets/tmp/final.csv",
        workspace=host,
    )

    review = draft.review({"will_start_replay": True})

    assert not any(
        "intermediate.jsonl" in item for item in review["blocking_issues"]
    )
    assert "storage/datasets/tmp/intermediate.jsonl" not in host.checked


def test_a_prose_final_artifact_names_the_available_outputs():
    draft = draft_distill(
        label="预打标到评测",
        steps=CHAIN,
        final_artifact="SFT 训练 JSONL(storage/datasets/sft.jsonl)，每行包含 messages",
        workspace=object(),
    )

    review = draft.review({"will_start_replay": True})

    assert any(
        "final_artifact" in item and "必须是单个产物路径" in item
        for item in review["blocking_issues"]
    )
    assert any(
        "`reports/metrics.json`" in item
        for item in review["blocking_issues"]
    )


def test_a_prose_final_artifact_is_recovered_when_it_names_one_output():
    draft = draft_distill(
        label="预打标到评测",
        steps=CHAIN,
        final_artifact=(
            "最终输出 reports/metrics.json，包含 rows 和 epochs，"
            "用于评测结果登记。"
        ),
        workspace=object(),
    )

    review = draft.review({"will_start_replay": True})

    assert review["final_artifact"] == "reports/metrics.json"
    assert not any(
        item.startswith("final_artifact")
        for item in review["blocking_issues"]
    )
    assert any(
        "唯一可产出路径恢复为 `reports/metrics.json`" in item
        for item in review["unresolved"]
    )


def test_an_untyped_scalar_contract_blocks_confirmation(
        service, host, pipeline_workspace):
    plan = prepare(
        service,
        distill_args(
            input_contract={"limit": {"description": "最多保留多少行"}}
        ),
        host=host,
    )

    assert any(
        "parameter 'limit' contract must declare type" in issue
        for issue in plan["blocking_issues"]
    )


def test_distill_plan_has_a_structural_review_for_the_user(
    service, host, pipeline_workspace):
    plan = prepare(service, distill_args(), host=host)
    review = plan["review"]
    assert set(plan) == {
        "plan_id",
        "revision",
        "digest",
        "status",
        "requires_confirmation",
        "planning",
        "review",
        "blocking_issues",
        "timings",
    }
    assert review["plan_type"] == "structural"
    assert review["source_traces"] == []
    assert [step["kind"] for step in review["steps"]] == ["run", "run", "run", "run"]
    assert review["steps"][1]["outputs"] == ["out/clean.csv"]
    assert review["scripts"] == [
        {
            "path": "scripts/prelabel.py",
            "resolved": "scripts/prelabel.py",
            "status": "preflight_pending",
        },
        {
            "path": "scripts/clean.py",
            "resolved": "scripts/clean.py",
            "status": "preflight_pending",
        },
        {
            "path": "scripts/train.py",
            "resolved": "scripts/train.py",
            "status": "preflight_pending",
        },
        {
            "path": "scripts/evaluate.py",
            "resolved": "scripts/evaluate.py",
            "status": "preflight_pending",
        },
    ]
    assert review["final_artifact"] == "reports/metrics.json"
    assert review["expected_graph_steps"] == [2, 3, 4]
    assert review["preflight"]["reads_scripts"] is False
    assert review["preflight"]["builds_package"] is False
    assert review["preflight"]["compiles_graph"] is False
    assert review["validation"]["will_start_replay"] is True
    assert review["human_gates"] == []


def test_preflight_graph_steps_match_the_review_across_storage_aliases(
        service, host, pipeline_workspace):
    steps = [
        {
            "run": (
                "python3 scripts/prelabel.py data/raw.csv rules.json "
                "--output storage/datasets/tmp/prelabeled.jsonl"
            ),
        },
        {
            "tool": "label_studio_project",
            "args": {
                "adapter": "jsonl",
                "dataset_path": "datasets/tmp/prelabeled.jsonl",
                "operation": "create",
            },
            "output": "public_data/project.json",
            "keep": True,
        },
        {
            "human": (
                "根据 public_data/project.json 复核后导出到 "
                "storage/datasets/tmp/review.jsonl"
            ),
            "input": ["public_data/project.json"],
            "output": "storage/datasets/tmp/review.jsonl",
        },
        {
            "run": (
                "python3 scripts/clean.py datasets/tmp/review.jsonl "
                "--output storage/datasets/tmp/clean.csv"
            ),
        },
        {
            "run": (
                "python3 scripts/evaluate.py datasets/tmp/clean.csv "
                "--output reports/metrics.json"
            ),
        },
    ]

    draft = draft_distill(
        label="跨 Storage 路径别名的完整链路",
        steps=steps,
        final_artifact="reports/metrics.json",
        workspace=host,
    )
    plan = distill(
        service,
        label="跨 Storage 路径别名的完整链路",
        steps=steps,
        final_artifact="reports/metrics.json",
        workspace=host,
    )

    assert draft.graph_steps == (1, 2, 3, 4, 5)
    assert tuple(node.order + 1 for node in plan.package.nodes) == draft.graph_steps
    assert preflight_matches_draft(draft, plan) == (True, None)


def test_distill_plan_builds_into_a_temp_dir_and_publishes_nothing(
        service, host, pipeline_workspace):
    """计划期就把包建一遍：能不能建出来是事实，不该等到确认才知道。

    建出来的东西落在临时目录，不发布、不排回放、不留下 distills 记录。
    """
    plan = prepare(service, distill_args(), host=host)
    assert plan["review"]["preflight"]["status"] == "pending_confirmation"
    assert not (service.root / "distills").exists()
    assert not (service.root / "captures").exists()


def test_a_derived_root_is_bound_from_the_package_not_demanded(
        service, host, pipeline_workspace):
    """根参数名是从路径片段推出来的（`test_100` 这种目录名），调用方猜不到。

    包里已经存着轨迹读到的真实路径，直接绑上，不要拿一个没人能猜到的 key 卡住确认。
    """
    steps = [
        {"run": "python3 scripts/clean.py data/raw.csv > out/clean.csv",
         "output": "out/clean.csv"},
    ]
    plan = distill(
        service, label="少给一个根参数", steps=steps, workspace=host,
        params={}, final_artifact="out/clean.csv",
    )
    assert dict(plan.package.params)["raw"] == "data/raw.csv"


def test_confirm_accepts_the_dashed_spelling_of_a_plan_id(
        service, host, pipeline_workspace):
    """把 plan_id 抄成带连字符的 UUID，不该变成"计划不存在"。

    计划 id 存的是裸 hex，人手抄一遍很容易写成 8-4-4-4-12；两种写法指的是同一行。
    确认必须同时带当前 digest：绑定的是用户看过的那一版。
    """
    plan = prepare(service, distill_args(), host=host)
    raw = plan["plan_id"]
    dashed = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    result = invoke(
        service, "conv-1", "confirm_distill",
        {"plan_id": dashed, "digest": plan["digest"]}, "confirm-dash", host=host
    )
    assert result["distill_plan"]["plan_id"] == raw


def test_confirm_distill_is_idempotent(service, host, pipeline_workspace):
    plan = prepare(service, distill_args(), host=host)
    first = confirm(service, plan, host, "confirm-1")
    second = confirm(service, plan, host, "confirm-2")
    assert second == first
    assert second["capture_id"] == first["capture_id"]
    assert second["run_id"]


def test_confirm_distill_requires_a_bounded_validation_replay(
        service, host, pipeline_workspace):
    arguments = distill_args()
    arguments.pop("execution_scope")
    arguments.pop("validation_params")
    plan = prepare(service, arguments, host=host)

    with pytest.raises(ValueError, match="bounded validation replay"):
        confirm(service, plan, host)
    assert service.distill_plans.get("conv-1", plan["plan_id"])["status"] == "planned"
    assert not (service.root / "captures").exists()


def test_confirm_distill_rejects_full_as_the_automatic_validation_scope(
        service, host, pipeline_workspace):
    plan = prepare(
        service,
        distill_args(
            execution_scope={"mode": "full", "description": "全量执行"},
        ),
        host=host,
    )

    assert plan["review"]["validation"]["ready"] is False
    with pytest.raises(ValueError, match="bounded validation replay"):
        confirm(service, plan, host)
    assert not (service.root / "captures").exists()


def test_confirm_distill_rejects_a_trace_changed_after_review(
        service, host, pipeline_workspace, trace_refs):
    refs = trace_refs({"trace_id": "s1"})
    plan = prepare(
        service,
        distill_args(trace_refs=refs, source_traces=["s1"]),
        host=host,
    )
    target = pipeline_workspace.root / refs[0]["path"]
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="structural plan changed"):
        confirm(service, plan, host)
    assert service.distill_plans.get(
        "conv-1", plan["plan_id"]
    )["status"] == "planned"
    assert not (service.root / "distills").exists()
    assert not (service.root / "captures").exists()


def test_confirm_distill_reloads_each_trace_once(
        service, host, pipeline_workspace, trace_refs, monkeypatch):
    refs = trace_refs({"trace_id": "s1"})
    trace_path = refs[0]["path"]
    reads = []
    read = host.read
    steps = [
        CHAIN[0],
        {
            "human": "请在 Label Studio 复核 out/prelabeled.jsonl，并把导出写到 out/review.jsonl",
            "input": ["out/prelabeled.jsonl"],
            "output": "out/review.jsonl",
        },
        *CHAIN[1:],
    ]

    def record(path):
        reads.append(path)
        return read(path)

    monkeypatch.setattr(host, "read", record)
    arguments = distill_args(
        steps=steps,
        trace_refs=refs,
        source_traces=["s1"],
        params={
            "raw": "data/raw.csv",
            "rules": "rules.json",
        },
        **HUMAN_VALIDATION,
    )
    plan = prepare(
        service,
        arguments,
        host=host,
    )
    assert reads.count(trace_path) == 1

    confirm(service, plan, host)
    assert reads.count(trace_path) == 2


def test_concurrent_confirm_distill_builds_one_capture(
        service, host, pipeline_workspace, monkeypatch):
    from agentgenome import dispatch

    plan = prepare(service, distill_args(), host=host)
    started = Event()
    release = Event()
    calls = 0
    build = dispatch._capture_distilled

    def slow_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        return build(*args, **kwargs)

    monkeypatch.setattr(dispatch, "_capture_distilled", slow_build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm, service, plan, host, "confirm-concurrent-1")
        assert started.wait(timeout=5)
        second = pool.submit(confirm, service, plan, host, "confirm-concurrent-2")
        release.set()
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert results[0] == results[1]
    assert calls == 1


def test_confirm_distill_rejects_a_changed_plan_digest(
        service, host, pipeline_workspace):
    plan = prepare(service, distill_args(), host=host)
    with pytest.raises(ValueError, match="digest changed"):
        invoke(
            service,
            "conv-1",
            "confirm_distill",
            {"plan_id": plan["plan_id"], "digest": "stale"},
            "confirm-stale",
            host=host,
        )
    assert service.distill_plans.get(
        "conv-1", plan["plan_id"]
    )["status"] == "planned"
    assert not (service.root / "captures").exists()


def test_confirm_distill_releases_the_plan_when_capture_build_fails(
        service, host, pipeline_workspace, monkeypatch):
    from agentgenome import dispatch

    plan = prepare(service, distill_args(), host=host)

    def fail(*args, **kwargs):
        raise RuntimeError("capture build failed")

    monkeypatch.setattr(dispatch, "_capture_distilled", fail)
    with pytest.raises(RuntimeError, match="capture build failed"):
        confirm(service, plan, host)
    assert service.distill_plans.get(
        "conv-1", plan["plan_id"]
    )["status"] == "planned"
    assert not (service.root / "captures").exists()


def test_distill_queues_with_explicit_minimal_validation(
        service, host, pipeline_workspace):
    plan = prepare(service, distill_args(), host=host)
    assert plan["status"] == "planned"
    assert plan["review"]["plan_type"] == "structural"
    assert plan["review"]["expected_graph_steps"] == [2, 3, 4]
    assert "capture_id" not in plan
    assert "run_id" not in plan
    assert plan["blocking_issues"] == []
    assert not (service.root / "captures").exists()

    confirmed = confirm(service, plan, host)
    # 人工复核是链路的边界：预打标的产出由人消费，不在最终产物的依赖闭包里。
    preflight = confirmed["preflight"]
    assert [node["id"] for node in preflight["nodes"]] == [
        "clean", "train", "evaluate"]
    assert [item["step"] for item in preflight["auxiliary"]] == [1]
    assert confirmed["distill_plan"]["status"] == "confirmed"
    assert confirmed["asset_id"] == "prelabel-train-eval"
    assert confirmed["reused"] is False
    assert confirmed["run_id"]

    state = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is True


class RehearsedHuman:
    """A human port that parks once, then writes the artifact it was asked for."""

    def __init__(self) -> None:
        self.questions: list[str] = []
        self.artifacts: list[str] = []

    def ask(self, question: str, evidence: dict[str, object]) -> Answer:
        self.questions.append(question)
        if len(self.questions) == 1:
            return Answer(False, pending=True)
        artifact = str(evidence["artifact"])
        self.artifacts.append(artifact)
        Path(artifact).write_text(
            json.dumps({"text": "p-one", "label": "a"}) + "\n", encoding="utf-8")
        return Answer(True)

    def capabilities(self) -> dict[str, object]:
        return {"interactive": True}


HUMAN_CHAIN = [
    {"run": "python3 scripts/prelabel.py data/raw.csv rules.json > out/prelabeled.jsonl",
     "from": "s1:2"},
    {"human": "请到 Label Studio 复核 out/prelabeled.jsonl，"
              "把导出的复核结果写到 {review}",
     "input": ["out/prelabeled.jsonl"], "output": "{review}", "from": "s1:3"},
    {"run": "python3 scripts/clean.py {review} > out/clean.csv", "from": "s1:4"},
]


def test_distill_freezes_a_human_gate_and_parks_its_replay(
        service, pipeline_workspace):
    human = RehearsedHuman()
    host = LocalExecutionHost(service.root, pipeline_workspace.root, human=human)
    plan = invoke(service, "conv-1", "distill",
                  {"label": "预打标到清洗", "asset_id": "prelabel-human-clean",
                   "steps": HUMAN_CHAIN, "final_artifact": "out/clean.csv",
                   **HUMAN_VALIDATION},
                  "call-1", host=host)
    assert [step["kind"] for step in plan["review"]["steps"]] == ["run", "human", "run"]
    assert len(plan["review"]["human_gates"]) == 1

    confirmed = confirm(service, plan, host)
    nodes = confirmed["preflight"]["nodes"]
    assert [node["kind"] for node in nodes] == ["run", "human", "run"]
    graph = (Path(confirmed["preflight"]["package"]) / "graph.yaml").read_text(
        encoding="utf-8")
    assert "human:" in graph
    assert "请到 Label Studio 复核" in graph
    parked = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert parked["status"] == "waiting"
    assert parked["result"]["waiting"]["node"].endswith("review")
    capture = service.captures.status(confirmed["capture_id"])
    assert capture["stage"] == "waiting"
    assert capture["published"] is False
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is False
    assert "停在人工节点" in capture["report_text"]

    resumed = service.resume("conv-1", confirmed["run_id"])
    assert resumed["status"] == "queued"
    finished = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert finished["status"] == "succeeded", finished["result"].get("error")
    assert len(human.questions) == 2
    assert human.artifacts[0].endswith("--review.out"), human.artifacts
    assert Path(human.artifacts[0]).read_text(encoding="utf-8").startswith('{"text": "p-one"')
    assert finished["result"]["waiting"] is None
    cleaned = Path(finished["result"]["outputs"]["clean"]["execution_path"])
    assert "p-one" in cleaned.read_text(encoding="utf-8")
    assert service.captures.status(confirmed["capture_id"])["stage"] == "published"
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is True


def test_queued_human_resume_keeps_checkpoint_and_can_park_at_next_gate(
        service, pipeline_workspace):
    class TwoGates:
        def __init__(self):
            self.questions = []

        def ask(self, question, evidence):
            self.questions.append(question)
            if len(self.questions) in (1, 3):
                return Answer(False, pending=True)
            Path(evidence["artifact"]).write_text(
                json.dumps({"text": "p-one", "label": "a"}) + "\n", encoding="utf-8")
            return Answer(True)

        def capabilities(self):
            return {"interactive": True}

    human = TwoGates()
    host = LocalExecutionHost(service.root, pipeline_workspace.root, human=human)
    steps = [
        HUMAN_CHAIN[0], HUMAN_CHAIN[1],
        {"human": "请再次核对 {review}，将结果写到 out/approved.jsonl",
         "input": ["{review}"], "output": "out/approved.jsonl"},
        {"run": "python3 scripts/clean.py out/approved.jsonl > out/clean.csv"},
    ]
    plan = distill(service, label="两次人工复核", steps=steps, workspace=host,
                   final_artifact="out/clean.csv")
    service.import_asset(plan.package.directory)
    run_id = service.submit("conv-1", "two-gates", plan.package.asset_id,
                            plan.package.version,
                            {"raw": "data/raw.csv", "rules": "rules.json"},
                            allow_draft=True)["id"]
    first = service.execute("conv-1", run_id, host, lambda _: None)
    assert first["status"] == "waiting"
    assert first["result"]["waiting"]["node"].endswith("review")
    assert service.resume("conv-1", run_id)["status"] == "queued"
    # A new service instance must read the waiting receipt from durable state.
    restarted = type(service)(service.root)
    second = restarted.execute("conv-1", run_id, host, lambda _: None)
    assert second["status"] == "waiting", second["result"]
    assert second["result"]["waiting"]["node"].endswith("approved")
    ledger = [json.loads(line) for line in
              (service.root / "runs" / run_id / "ledger.jsonl").read_text().splitlines()]
    assert sum(entry["event"] == "run_start" for entry in ledger) == 1
    assert sum(entry["event"] == "node_enter" and entry["node"].endswith("prelabel")
               for entry in ledger) == 1
    assert len(human.questions) == 3
    assert restarted.resume("conv-1", run_id)["status"] == "queued"
    finished = restarted.execute("conv-1", run_id, host, lambda _: None)
    assert finished["status"] == "succeeded", finished["result"]
    assert len(human.questions) == 4


def test_interrupted_wakeup_resumes_from_its_parked_checkpoint(
        service, pipeline_workspace):
    """A crash between resume and re-entry leaves the answered gate in the
    checkpoint; waking again must re-enter that same gate, not fail the run."""
    human = RehearsedHuman()
    host = LocalExecutionHost(service.root, pipeline_workspace.root, human=human)
    plan = distill(service, label="预打标到清洗", steps=HUMAN_CHAIN,
                   workspace=host, final_artifact="out/clean.csv")
    service.import_asset(plan.package.directory)
    run_id = service.submit("conv-1", "restart", plan.package.asset_id,
                            plan.package.version,
                            {"raw": "data/raw.csv", "rules": "rules.json"},
                            allow_draft=True)["id"]
    assert service.execute("conv-1", run_id, host, lambda _: None)["status"] == "waiting"
    assert service.resume("conv-1", run_id)["status"] == "queued"
    restarted = type(service)(service.root)
    restarted.recover()
    assert restarted.status("conv-1", run_id)["status"] == "interrupted"
    assert restarted.parked_checkpoint(run_id).endswith("review")

    assert restarted.resume("conv-1", run_id)["status"] == "queued"
    finished = restarted.execute("conv-1", run_id, host, lambda _: None)
    assert finished["status"] == "succeeded", finished["result"]
    assert len(human.questions) == 2
    assert Path(finished["result"]["outputs"]["clean"]["execution_path"]).exists()


def test_replay_requeue_wakes_an_interrupted_run_from_its_checkpoint(
        service, pipeline_workspace):
    """A requeue on a run whose checkpoint still parks a gate resumes it."""
    human = RehearsedHuman()
    host = LocalExecutionHost(service.root, pipeline_workspace.root, human=human)
    plan = distill(service, label="预打标到清洗", steps=HUMAN_CHAIN,
                   workspace=host, final_artifact="out/clean.csv")
    service.import_asset(plan.package.directory)
    run_id = service.submit("conv-1", "restart", plan.package.asset_id,
                            plan.package.version,
                            {"raw": "data/raw.csv", "rules": "rules.json"},
                            allow_draft=True)["id"]
    assert service.execute("conv-1", run_id, host, lambda _: None)["status"] == "waiting"
    assert service.resume("conv-1", run_id)["status"] == "queued"
    restarted = type(service)(service.root)
    restarted.recover()

    queued = restarted.replay_requeue("conv-1", run_id)
    assert queued["status"] == "queued"
    assert queued["id"] == run_id
    finished = restarted.execute("conv-1", run_id, host, lambda _: None)
    assert finished["status"] == "succeeded", finished["result"]
    assert len(human.questions) == 2


def test_an_interrupted_capture_keeps_its_answered_gate_and_can_publish(
        service, pipeline_workspace):
    """A capture whose wakeup died is not decided: the answer in the checkpoint
    still counts, so the resumed attempt publishes instead of starting over."""
    human = RehearsedHuman()
    host = LocalExecutionHost(service.root, pipeline_workspace.root, human=human)
    plan = invoke(service, "conv-1", "distill",
                  {"label": "预打标到清洗", "asset_id": "interrupted-human",
                   "steps": HUMAN_CHAIN, "final_artifact": "out/clean.csv",
                   **HUMAN_VALIDATION},
                  "call-1", host=host)
    confirmed = confirm(service, plan, host)
    parked = service.execute("conv-1", confirmed["run_id"], host, lambda _: None)
    assert parked["status"] == "waiting"
    assert service.resume("conv-1", confirmed["run_id"])["status"] == "queued"

    restarted = type(service)(service.root)
    restarted.recover()
    interrupted = restarted.captures.status(confirmed["capture_id"])
    assert interrupted["stage"] == "interrupted"
    assert interrupted["published"] is False

    assert restarted.resume("conv-1", confirmed["run_id"])["status"] == "queued"
    finished = restarted.execute("conv-1", confirmed["run_id"], host, lambda _: None)
    assert finished["status"] == "succeeded", finished["result"].get("error")
    assert restarted.captures.status(confirmed["capture_id"])["stage"] == "published"
    assert restarted.get(confirmed["asset_id"], confirmed["version"])["published"] is True


def test_recovery_interrupts_a_queued_human_resume(service, pipeline_workspace):
    human = RehearsedHuman()
    host = LocalExecutionHost(service.root, pipeline_workspace.root, human=human)
    plan = distill(service, label="预打标到清洗", steps=HUMAN_CHAIN,
                   workspace=host, final_artifact="out/clean.csv")
    service.import_asset(plan.package.directory)
    run_id = service.submit("conv-1", "restart", plan.package.asset_id,
                            plan.package.version,
                            {"raw": "data/raw.csv", "rules": "rules.json"},
                            allow_draft=True)["id"]
    assert service.execute("conv-1", run_id, host, lambda _: None)["status"] == "waiting"
    assert service.resume("conv-1", run_id)["status"] == "queued"
    restarted = type(service)(service.root)
    restarted.recover()
    assert restarted.status("conv-1", run_id)["status"] == "interrupted"
    assert restarted.execute("conv-1", run_id, host, lambda _: None)["status"] == "interrupted"
    assert len(human.questions) == 1


def test_distill_is_deterministic_so_a_retry_reuses_the_version(
        service, host, pipeline_workspace):
    first_plan = prepare(service, distill_args(), "call-1", host)
    first = confirm(service, first_plan, host, "confirm-1")
    service.execute("conv-1", first["run_id"], host, lambda event: None)

    second_plan = prepare(service, distill_args(), "call-2", host)
    second = confirm(service, second_plan, host, "confirm-2")
    assert second["preflight"]["trace_id"] == first["preflight"]["trace_id"]
    assert second["reused"] is True
    assert second["pin"] == first["pin"]
    assert len(service.versions(first["asset_id"])) == 1


def test_distill_writes_a_report_naming_every_step(service, host, pipeline_workspace):
    plan = prepare(service, distill_args(), host=host)
    assert "draft" not in plan
    assert "report" not in plan
    confirmed = confirm(service, plan, host)
    body = Path(confirmed["preflight"]["report"]).read_text(encoding="utf-8")
    assert "# 链路蒸馏报告" in body
    assert "scripts/train.py" in body
    assert "s1:6" in body
    assert "辅助" in body


def test_distill_carries_declarations(service, host, pipeline_workspace):
    plan = prepare(service, distill_args(
        applies_when=["原始问题 CSV 过滤后生成回答"],
        not_applies_when=["输入已是 messages-schema JSONL"],
        input_contract={"data_file": {"format": "csv", "required_fields": ["qid"]}},
    ), host=host)
    confirmed = confirm(service, plan, host)
    package = Path(confirmed["preflight"]["package"])
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["determinism"] == "script"
    assert "references" not in manifest
    assert "gaps" not in manifest
    assert manifest["applies_when"] == ["原始问题 CSV 过滤后生成回答"]
    assert manifest["not_applies_when"] == ["输入已是 messages-schema JSONL"]
    assert manifest["input_contract"]["data_file"]["required_fields"] == ["qid"]

    state = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    listed = next(item for item in service.list() if item["id"] == confirmed["asset_id"])
    assert "has_references" not in listed
    assert "has_gaps" not in listed
    assert listed["determinism"] == "script"
    assert listed["applies_when"] == ["原始问题 CSV 过滤后生成回答"]
    assert listed["input_contract"]["data_file"]["format"] == "csv"


def test_distill_rejects_inline_code(service, host, pipeline_workspace):
    steps = [{"run": "python3 -c 'print(1)' > out/probe.json"}]
    with pytest.raises(DistillError, match="内联代码"):
        invoke(service, "conv-1", "distill", distill_args(steps=steps), "call-1", host=host)


def test_distill_rejects_probes(service, host, pipeline_workspace):
    steps = [{"run": "ls -la data/raw.csv"}]
    with pytest.raises(DistillError, match="只读探测"):
        invoke(service, "conv-1", "distill", distill_args(steps=steps), "call-1", host=host)


def test_distill_rejects_a_step_without_paths(service, host, pipeline_workspace):
    steps = [{"run": "sleep 3"}]
    with pytest.raises(DistillError, match="没有可识别的路径"):
        invoke(service, "conv-1", "distill", distill_args(steps=steps), "call-1", host=host)


def test_distill_declares_a_positional_output_the_parser_reads_as_an_input(
        service, host, pipeline_workspace):
    """`script.py in out` hides the write: the plan declares the product instead."""
    pipeline_workspace.write("scripts/to_sft.py", TO_SFT)
    pipeline_workspace.write("out/review.jsonl",
                             json.dumps({"text": "p-one"}) + "\n")
    plan = prepare(service, {
        "label": "复核导出转 SFT", "asset_id": "review-to-sft",
        "steps": [{"run": "python3 scripts/to_sft.py out/review.jsonl out/sft.jsonl",
                   "output": "out/sft.jsonl"}],
        "final_artifact": "out/sft.jsonl",
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {"review": "out/review.jsonl"},
    }, host=host)
    assert plan["review"]["steps"][0]["outputs"] == ["out/sft.jsonl"]
    confirmed = confirm(service, plan, host)
    distilled = confirmed["preflight"]
    assert [item["key"] for item in distilled["params"]] == ["review"]
    package = Path(distilled["package"])
    graph = (package / "graph.yaml").read_text(encoding="utf-8")
    assert "do:\n      run: python3 scripts/to_sft.py {input.review} {artifact}" in graph
    assert "output:\n      sft:" in graph

    state = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is True


def test_distill_rejects_truncated_source_even_with_scope_confirmation(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    from dataclasses import replace

    trace = trace_factory([
        tool_entry("c1", "python3 scripts/clean.py out/review.jsonl > out/clean.csv"),
    ], trace_id="s1")
    trace = replace(trace, export_dropped_steps=30)
    with pytest.raises(DistillError, match="来源轨迹导出不完整.*s1 \\(30\\)"):
        distill(service, label="清洗", workspace=host, traces=[trace],
                steps=[{"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"}],
                final_artifact="out/clean.csv", review_confirmed=True,
                review_reason="Reviewed only the visible section")
    assert not (service.root / "distills").exists()


def test_distill_output_contract_requires_explicit_final_artifact(
        service, host, pipeline_workspace):
    steps = [
        {"human": "请把复核结果写到 out/review.jsonl", "output": "out/review.jsonl"},
        {"run": "python3 scripts/to_sft.py out/review.jsonl out/sft.jsonl"},
    ]
    with pytest.raises(DistillError, match="final_artifact"):
        distill(service, label="人工导出转 SFT", steps=steps, workspace=host,
                declarations={"output_contract": {"sft": {"format": "jsonl"}}})
    with pytest.raises(DistillError, match="没有被任何步骤声明为产出"):
        distill(service, label="人工导出转 SFT", steps=steps, workspace=host,
                final_artifact="out/sft.jsonl",
                declarations={"output_contract": {"sft": {"format": "jsonl"}}})
    assert not (service.root / "distills").exists()


def test_distill_keeps_declared_positional_output_after_script_materialization(
        service, host, pipeline_workspace):
    pipeline_workspace.write("scripts/to_sft.py", TO_SFT)
    steps = [
        {"human": "请把复核结果写到 out/review.jsonl", "output": "out/review.jsonl"},
        {"run": "python3 {converter_script} out/review.jsonl out/sft.jsonl",
         "output": "out/sft.jsonl"},
        {"run": "python3 scripts/clean.py out/sft.jsonl > out/audit.csv", "keep": True},
    ]
    plan = distill(service, label="人工导出转 SFT", steps=steps, workspace=host,
                   final_artifact="out/sft.jsonl",
                   params={"converter_script": "scripts/to_sft.py"},
                   declarations={"output_contract": {"sft": {
                       "format": "jsonl", "required_fields": ["messages"]}}})
    assert [node.id for node in plan.package.nodes] == ["review", "to-sft", "clean"]
    sft = plan.package.graph.node.children[1]
    key = next(iter(sft.output))
    assert plan.package.graph.node.output[key].compose[0].node_path == sft.path
    assert "{artifact}" in plan.package.nodes[1].command
    assert plan.package.final_artifact == "out/sft.jsonl"
    root_verify = (Path(plan.package.directory) / "graph.yaml").read_text(encoding="utf-8")
    assert "missing required fields" in root_verify
    assert "messages" in root_verify


def test_final_output_contract_rejects_malformed_or_incomplete_jsonl(
        service, host, pipeline_workspace):
    pipeline_workspace.write(
        "scripts/to_sft.py",
        "import json, sys\n"
        "open(sys.argv[2], 'w').write(json.dumps({'id': 'sample'}) + '\\n')\n",
    )
    pipeline_workspace.write("out/review.jsonl", '{"text":"reviewed"}\n')
    plan = prepare(service, {
        "label": "复核转 SFT", "asset_id": "review-to-sft-verified",
        "steps": [{"run": "python3 scripts/to_sft.py out/review.jsonl out/sft.jsonl",
                   "output": "out/sft.jsonl"}],
        "final_artifact": "out/sft.jsonl",
        "params": {"review": "out/review.jsonl"},
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {"review": "out/review.jsonl"},
        "output_contract": {"sft": {
            "format": "jsonl", "required_fields": ["id", "gt"]
        }},
    }, host=host)
    confirmed = confirm(service, plan, host)
    result = service.execute("conv-1", confirmed["run_id"], host, lambda _: None)
    assert result["status"] == "failed"
    events = [
        json.loads(line)
        for line in (service.root / "runs" / confirmed["run_id"] / "ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        event.get("event") == "check" and event.get("verdict") == "fail"
        for event in events
    )
    assert service.get(
        "review-to-sft-verified", confirmed["version"]
    )["published"] is False


def test_distill_rejects_a_declared_output_missing_from_compiled_graph(
        service, host, pipeline_workspace, monkeypatch):
    from dataclasses import replace
    from agentgenome.capture import emit

    compile_graph = emit.compile_graph

    def drop_output(directory):
        graph = compile_graph(directory)
        child = graph.node.children[0]
        return replace(graph, node=replace(
            graph.node, children=(replace(child, output={}), *graph.node.children[1:])))

    monkeypatch.setattr(emit, "compile_graph", drop_output)
    with pytest.raises(DistillError, match="图节点没有对应的 output"):
        distill(service, label="清洗", workspace=host,
                steps=[{"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"}],
                final_artifact="out/clean.csv")
    assert service.versions("清洗") == []


def test_distill_rejects_a_declared_output_the_command_never_names(
        service, host, pipeline_workspace):
    steps = [{"run": "python3 scripts/to_sft.py out/review.jsonl out/sft.jsonl",
              "output": "out/ghost.jsonl"}]
    with pytest.raises(DistillError, match="命令里找不到"):
        invoke(service, "conv-1", "distill", distill_args(steps=steps), "call-1", host=host)


def test_distill_rejects_a_final_artifact_no_step_produces(
        service, host, pipeline_workspace):
    plan = prepare(
        service,
        distill_args(final_artifact="reports/ghost.json"),
        "call-1",
        host,
    )
    assert any("final_artifact" in item for item in plan["review"]["unresolved"])
    assert any("final_artifact" in item for item in plan["blocking_issues"])
    with pytest.raises(ValueError, match="final_artifact"):
        confirm(service, plan, host)


def test_distill_reports_a_script_that_is_not_on_disk(
        service, host, pipeline_workspace):
    steps = [{"run": "python3 scripts/ghost.py data/raw.csv > out/ghost.csv"}]
    plan = prepare(
        service,
        distill_args(steps=steps, final_artifact="out/ghost.csv"),
        "call-1",
        host,
    )
    # The dry build runs at plan time, so the missing script is reported now
    # instead of costing a confirmation round.
    assert any("脚本不存在" in item for item in plan["review"]["blocking_issues"])


def test_distill_needs_a_host_that_can_read_scripts(service, pipeline_workspace):
    class NoReader:
        pass

    with pytest.raises(ValueError, match="host"):
        invoke(service, "conv-1", "distill", distill_args(), "call-1", host=NoReader())


def test_distill_rejects_missing_arguments(service, host, pipeline_workspace):
    with pytest.raises(ValueError, match="label"):
        invoke(service, "conv-1", "distill", {"steps": CHAIN}, "call-1", host=host)
    with pytest.raises(ValueError, match="steps"):
        invoke(service, "conv-1", "distill", {"label": "x"}, "call-2", host=host)
    with pytest.raises(ValueError, match="new_params"):
        invoke(service, "conv-1", "distill", distill_args(new_params=[]), "call-3", host=host)
    with pytest.raises(ValueError, match="source_traces"):
        invoke(service, "conv-1", "distill", distill_args(source_traces=[7]), "call-4",
               host=host)


def test_distill_without_a_final_artifact_keeps_the_whole_chain(
        service, host, pipeline_workspace):
    plan = distill(service, label="预打标到评测", steps=CHAIN, workspace=host)
    assert plan.package.final_artifact is None
    assert [node.id for node in plan.package.nodes] == [
        "prelabel", "clean", "train", "evaluate"]


def test_distill_checks_the_chain_against_the_source_trace(
        service, host, pipeline_workspace, trace_refs):
    refs = trace_refs({"trace_id": "s1"})
    steps = [CHAIN[0],
             {"human": "请复核预打标结果 out/prelabeled.jsonl，"
                       "把导出的标注写到 out/review.jsonl",
              "input": ["out/prelabeled.jsonl"], "output": "out/review.jsonl"},
             *CHAIN[1:]]
    plan = invoke(service, "conv-1", "distill",
                  distill_args(steps=steps, trace_refs=refs, source_traces=["s1"],
                               validation_params={"raw": "data/raw.csv",
                                                  "rules": "rules.json"}),
                  "call-1", host=host)
    assert plan["review"]["source_traces"] == ["s1"]
    confirmed = confirm(service, plan, host)
    assert confirmed["preflight"]["chain"] == {
        "checked": True,
        "traces": ["s1"],
        "boundaries": [],
    }


def test_distill_rejects_a_declared_step_nothing_consumes(
        service, host, pipeline_workspace, trace_refs):
    """本地试跑/旁支步骤不该进 steps：没人消费它的产出，反向切片就会把它丢掉。"""
    refs = trace_refs({"trace_id": "s1"})
    plan = prepare(
        service,
        distill_args(trace_refs=refs, source_traces=["s1"]),
        "call-1",
        host,
    )
    assert plan["review"]["expected_graph_steps"] == [2, 3, 4]
    # An unconsumed product is advice, not a defect: the plan builds and can run.
    assert not any("keep: true" in item for item in plan["review"]["blocking_issues"])


def test_distill_keeps_a_side_effect_tool_step_nothing_consumes(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """建项目、提交任务这类调用没有下游消费者：keep: true 才留得住它。"""
    trace = trace_factory([
        tool_entry("c1", "python3 scripts/prelabel.py data/raw.csv rules.json "
                         "> out/prelabeled.jsonl"),
        tool_entry("c2", "python3 scripts/clean.py out/prelabeled.jsonl > out/clean.csv"),
    ], trace_id="s1")
    steps = [
        {"run": "python3 scripts/prelabel.py data/raw.csv rules.json "
                "> out/prelabeled.jsonl"},
        {"tool": "label_studio_project",
         "args": {"dataset_path": "out/prelabeled.jsonl", "title": "复核"},
         "output": "out/project.json", "keep": True},
        {"run": "python3 scripts/clean.py out/prelabeled.jsonl > out/clean.csv"},
    ]
    plan = distill(service, label="预打标到清洗", steps=steps, workspace=host,
                   final_artifact="out/clean.csv", traces=[trace])
    assert [node.id for node in plan.package.nodes] == [
        "prelabel", "label-studio-project", "clean"]
    assert plan.kept == (1,)
    assert plan.payload()["kept"] == [{"step": 2, "reason": KEEP_REASON}]


def test_distill_turns_an_async_tool_receipt_into_a_wait_step(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """异步平台工具：tool 节点落受理单，等待脚本把它换成真实产物再往下传。"""
    pipeline_workspace.write("scripts/wait_pipeline.py", TO_SFT)
    trace = trace_factory([
        tool_entry("c1", "python3 scripts/wait_pipeline.py out/receipt.json "
                         "out/processed.jsonl"),
        tool_entry("c2", "python3 scripts/clean.py out/processed.jsonl > out/clean.csv"),
    ], trace_id="s1")
    steps = [
        {"tool": "df_submit_pipeline",
         "args": {"input_path": "data/raw.csv", "script_path": "scripts/prelabel.py"},
         "output": "out/receipt.json"},
        {"run": "python3 scripts/wait_pipeline.py out/receipt.json out/processed.jsonl",
         "output": "out/processed.jsonl"},
        {"run": "python3 scripts/clean.py out/processed.jsonl > out/clean.csv"},
    ]
    plan = distill(service, label="平台预打标到清洗", steps=steps, workspace=host,
                   final_artifact="out/clean.csv", traces=[trace])
    assert [node.id for node in plan.package.nodes] == [
        "df-submit-pipeline", "wait-pipeline", "clean"]
    graph = (plan.package.directory / "graph.yaml").read_text(encoding="utf-8")
    # 等待固化成引擎的 await 节点：不占终端、不占 worker，由任务回调唤醒。
    assert "await:" in graph
    assert "wait_pipeline.py" not in graph
    assert "from: df-submit-pipeline.output.receipt" in graph


def test_distill_reports_every_problem_in_one_submission(service, host, pipeline_workspace):
    """一次提交报全部问题：五轮「改一个再提交」的校验循环太慢。"""
    steps = [{"run": "python3 -c 'print(1)' > out/probe.json"},
             {"run": "ls -la data/raw.csv"}]
    with pytest.raises(DistillError, match="2 个问题") as error:
        invoke(service, "conv-1", "distill", distill_args(steps=steps), "call-1", host=host)
    assert "内联代码" in str(error.value)
    assert "只读探测" in str(error.value)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "rm -rf out && mkdir -p out && "
            "python3 scripts/ls_to_sft.py data/raw.jsonl > out/train.jsonl",
            "python3 scripts/ls_to_sft.py data/raw.jsonl > out/train.jsonl",
        ),
        (
            "nice -n 19 python3 scripts/ls_to_sft.py data/raw.jsonl > out/train.jsonl",
            "python3 scripts/ls_to_sft.py data/raw.jsonl > out/train.jsonl",
        ),
        (
            "sudo -u user -H python3 scripts/ls_to_sft.py data/raw.jsonl "
            "> out/train.jsonl",
            "python3 scripts/ls_to_sft.py data/raw.jsonl > out/train.jsonl",
        ),
    ],
)
def test_meaningful_shell_removes_retry_scaffolding(command, expected):
    assert _meaningful_shell(command) == expected


def test_distill_rejects_a_plan_that_drops_an_upstream_step(
        service, host, pipeline_workspace, trace_refs):
    refs = trace_refs({"trace_id": "s1"})
    steps = CHAIN[2:]
    plan = prepare(
        service,
        distill_args(steps=steps, trace_refs=refs),
        "call-1",
        host,
    )
    assert plan["review"]["scope_warnings"]
    with pytest.raises((DistillError, ValueError), match="链路不完整"):
        confirm(service, plan, host)


def test_distill_asks_the_user_before_freezing_a_narrower_chain(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """与链路相连的步骤掉出链路时，先停下让用户复核，而不是默默固化半条链路。"""
    trace = trace_factory([
        tool_entry("c1", "python3 scripts/clean.py out/review.jsonl > out/clean.csv"),
        tool_entry("c2", "python3 scripts/train.py out/clean.csv --epochs 3 "
                         "--output models/model.pt"),
        tool_entry("c3", "python3 scripts/evaluate.py models/model.pt out/clean.csv "
                         "> reports/metrics.json"),
    ], trace_id="s1")
    steps = [{"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"},
             {"run": "python3 scripts/train.py out/clean.csv --epochs 3 "
                      "--output models/model.pt"}]
    with pytest.raises(DistillError, match="s1:2") as error:
        distill(service, label="清洗到训练", steps=steps, workspace=host,
                final_artifact="models/model.pt", traces=[trace])
    assert "复核" in str(error.value)
    with pytest.raises(DistillError, match="review_reason"):
        distill(service, label="清洗到训练", steps=steps, workspace=host,
                final_artifact="models/model.pt", traces=[trace],
                review_confirmed=True)
    plan = distill(service, label="清洗到训练", steps=steps, workspace=host,
                   final_artifact="models/model.pt", traces=[trace],
                   review_confirmed=True, review_reason="用户确认这次只要清洗到训练")
    report = Path(plan.report_path).read_text(encoding="utf-8")
    assert "范围复核：用户确认这次只要清洗到训练" in report


def test_distill_records_a_side_branch_without_asking_the_user(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """明显不在正式流程里的旁支步骤（本地试跑小样、探测产物）不挡住沉淀。"""
    trace = trace_factory([
        tool_entry("c0", "python3 scripts/prelabel.py data/raw.csv rules.json "
                         "> out/trial.jsonl"),
        tool_entry("c1", "python3 scripts/clean.py out/review.jsonl > out/clean.csv"),
        tool_entry("c2", "python3 scripts/train.py out/clean.csv --epochs 3 "
                         "--output models/model.pt"),
    ], trace_id="s1")
    steps = [{"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"},
             {"run": "python3 scripts/train.py out/clean.csv --epochs 3 "
                      "--output models/model.pt"}]
    plan = distill(service, label="清洗到训练", steps=steps, workspace=host,
                   final_artifact="models/model.pt", traces=[trace])
    assert plan.scope_review is None
    assert len(plan.branches) == 1
    assert plan.branches[0].startswith("`s1:0` [shell] `python3 scripts/prelabel.py")
    assert "out/trial.jsonl" in plan.branches[0]
    report = Path(plan.report_path).read_text(encoding="utf-8")
    assert "旁支步骤（未进链路，链路不依赖它）" in report
    assert plan.payload()["branches"] == list(plan.branches)


def test_distill_treats_a_cleaned_retry_as_the_same_step(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    command = "python3 scripts/clean.py out/review.jsonl > out/clean.csv"
    trace = trace_factory(
        [
            tool_entry("c0", "rm -rf out && mkdir -p out && " + command),
            tool_entry("c1", command),
        ],
        trace_id="s1",
    )
    plan = distill(
        service,
        label="清洗",
        steps=[{"run": command}],
        workspace=host,
        final_artifact="out/clean.csv",
        traces=[trace],
    )
    assert plan.scope_review is None
    assert plan.branches == ()


def test_distill_drops_a_local_trial_that_shares_the_submitted_script(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """本地试跑小样和平台提交共用同一个脚本，试跑也不该被算成链路的一部分。

    会话先跑小样确认脚本（脚本是小样与提交之间唯一的交集），再提交平台执行；沉淀
    只留提交那一步：试跑落进旁支，不触发范围复核，脚本随包搬运而不是留成输入边界。
    """
    trace = trace_factory([
        tool_entry("t1", "python3 scripts/prelabel.py data/raw.csv rules.json "
                         "> out/trial.jsonl"),
        tool_entry("p1", "", tool="submit_pipeline",
                   args={"script_path": "scripts/prelabel.py",
                         "input_path": "data/raw2.csv"}),
    ], trace_id="s1")
    steps = [{"tool": "submit_pipeline",
              "args": {"script_path": "{pipeline_script}", "input_path": "{raw}"},
              "input": ["{pipeline_script}", "{raw}"],
              "output": "out/submitted.jsonl", "from": "s1:1"}]
    plan = distill(service, label="提交平台执行", steps=steps, workspace=host,
                   final_artifact="out/submitted.jsonl", traces=[trace],
                   params={"pipeline_script": "scripts/prelabel.py",
                           "raw": "data/raw2.csv"})
    assert plan.scope_review is None
    assert len(plan.branches) == 1
    assert plan.branches[0].startswith("`s1:0` [shell]")
    assert "out/trial.jsonl" in plan.branches[0]
    assert plan.materialized == (("pipeline_script", "scripts/prelabel.py"),)
    assert plan.package.scripts == (("scripts/prelabel.py", "scripts/prelabel.py"),)


def test_distill_asks_the_user_before_starting_at_a_human_handover(
        service, host, pipeline_workspace, pipeline_trace):
    """人工交回的产物不是根输入：从它开始等于把人工门禁从链路里吸收掉。

    会话先跑预打标，用户去 Label Studio 复核完回来说继续；计划如果从复核结果开始，
    前半段（预打标）掉出链路，却正是这次沉淀边界的前一步，必须先问用户。用户点名
    交回了 `out/review.jsonl`，这就是人工交接的凭据。
    """
    trace = pipeline_trace("s1")
    with pytest.raises(DistillError, match="s1:1") as error:
        distill(service, label="复核到评测", steps=CHAIN[1:], workspace=host,
                final_artifact=FINAL, traces=[trace])
    assert "用户复核" in str(error.value)
    assert "人工交接的前半段" in str(error.value)
    plan = distill(service, label="复核到评测", steps=CHAIN[1:], workspace=host,
                   final_artifact=FINAL, traces=[trace],
                   review_confirmed=True,
                   review_reason="用户确认这次只要复核导出之后的一段")
    report = Path(plan.report_path).read_text(encoding="utf-8")
    assert "范围复核：用户确认这次只要复核导出之后的一段" in report


def test_distill_keeps_the_front_half_when_the_human_gate_is_in_the_chain(
        service, host, pipeline_workspace, pipeline_trace):
    """人工复核写成 human 步骤、预打标补进 steps 之后，范围复核不再触发。"""
    steps = [
        {"run": "python3 scripts/prelabel.py data/raw.csv rules.json "
                "> out/prelabeled.jsonl", "from": "s1:1"},
        {"human": "请到 Label Studio 复核 out/prelabeled.jsonl，"
                  "把导出的复核结果写到 out/review.jsonl",
         "input": ["out/prelabeled.jsonl"], "output": "out/review.jsonl",
         "from": "s1:2"},
        {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv",
         "from": "s1:3"},
        {"run": "python3 scripts/train.py out/clean.csv --epochs 3 "
                 "--output models/model.pt", "from": "s1:4"},
        {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv "
                 "> reports/metrics.json", "from": "s1:5"},
    ]
    plan = distill(service, label="预打标到评测", steps=steps, workspace=host,
                   final_artifact=FINAL, traces=[pipeline_trace("s1")])
    assert plan.scope_review is None
    assert [node.kind for node in plan.package.nodes] == [
        "run", "human", "run", "run", "run"]


def test_distill_reads_a_script_invoked_after_a_cleanup_command(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """`rm -f` 的清理清单不是这一步的产出。

    真实会话把清理与脚本调用写在同一段命令里；把脚本本身记成 rm 的产出，
    计划里读它的每一步都会被判成链路不完整。
    """
    trace = trace_factory([
        tool_entry("c1", "rm -f out/prelabeled.jsonl\n"
                         "python3 scripts/prelabel.py data/raw.csv rules.json "
                         "> out/prelabeled.jsonl"),
    ], trace_id="s1")
    steps = [{"run": "python3 scripts/prelabel.py data/raw.csv rules.json "
                     "> out/prelabeled.jsonl"}]
    plan = distill(service, label="清理后预打标", steps=steps, workspace=host,
                   final_artifact="out/prelabeled.jsonl", traces=[trace])
    assert plan.boundaries == ()
    assert [node.id for node in plan.package.nodes] == ["prelabel"]


def test_distill_rejects_an_input_the_editor_tool_wrote(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    trace = trace_factory([tool_entry("w1", "", tool="write",
                                      args={"path": "out/clean.csv"})], trace_id="s1")
    steps = [{"run": "python3 scripts/train.py out/clean.csv --epochs 3 "
                     "--output models/model.pt"}]
    with pytest.raises(DistillError, match="编辑工具"):
        distill(service, label="x", steps=steps, workspace=host, traces=[trace])


def test_distill_reports_a_declared_input_boundary(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    trace = trace_factory([
        tool_entry("w1", "", tool="write", args={"path": "out/clean.csv"}),
    ], trace_id="s1")
    steps = [
        {"run": "python3 scripts/train.py {clean_csv} --epochs 3 --output models/model.pt"},
        {"run": "python3 scripts/evaluate.py models/model.pt {clean_csv} "
                "> reports/metrics.json"},
    ]
    plan = distill(service, label="x", steps=steps, workspace=host,
                   final_artifact=FINAL, params={"clean_csv": "out/clean.csv"},
                   traces=[trace])
    boundaries = plan.boundaries
    assert len(boundaries) == 1
    assert "输入边界" in boundaries[0]
    assert "out/clean.csv" in boundaries[0]
    body = plan.report_path.read_text(encoding="utf-8")
    assert "输入边界" in body


def test_distill_rejects_a_boundary_that_hides_a_fixed_command(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    trace = trace_factory([
        tool_entry("c1", "python3 scripts/clean.py out/review.jsonl > out/clean.csv"),
    ], trace_id="s1")
    steps = [
        {"run": "python3 scripts/train.py {clean_csv} --epochs 3 --output models/model.pt"},
    ]
    with pytest.raises(DistillError, match="固定命令"):
        distill(service, label="x", steps=steps, workspace=host,
                final_artifact="models/model.pt",
                params={"clean_csv": "out/clean.csv"}, traces=[trace])


def test_distill_binds_placeholder_parameters(service, host, pipeline_workspace):
    steps = [
        {"run": "python3 scripts/clean.py {review_jsonl} > out/clean.csv"},
        {"run": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt"},
        {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv "
                "> reports/metrics.json"},
    ]
    plan = prepare(service, distill_args(
        steps=steps,
        params={"review_jsonl": "out/review.jsonl"},
        validation_params={"review_jsonl": "out/review.jsonl"},
    ), host=host)
    confirmed = confirm(service, plan, host)
    assert confirmed["preflight"]["params"] == [
        {"key": "review_jsonl", "source": "{review_jsonl}", "value": "out/review.jsonl"}]
    package = Path(confirmed["preflight"]["package"])
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["parameters"]) == ["review_jsonl"]
    graph = (package / "graph.yaml").read_text(encoding="utf-8")
    assert "{input.review_jsonl}" in graph

    state = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is True


def converter_session(tool_entry, workspace, *, script="scripts/to_sft.py"):
    """A converter authored with the editor tool, then run once by hand."""
    document = {
        "trace_version": 1, "trace_id": "s1", "session_id": "s1",
        "session_name": "转 SFT messages", "step_count": 2,
        "steps": [tool_entry("w1", "", tool="write", args={"path": script}),
                  tool_entry("c1", "python3 scripts/to_sft.py out/review.jsonl "
                                   "out/sft.jsonl")],
    }
    workspace.write("public_data/agentgenome-traces/s1.json", json.dumps(document))
    return ["public_data/agentgenome-traces/s1.json"]


CONVERTER_ARGS = {
    "label": "转 SFT messages", "asset_id": "text-to-sft-messages",
    "steps": [{"run": "python3 {converter_script} {answers_jsonl} {sft_output}"}],
    "params": {"converter_script": "scripts/to_sft.py",
               "answers_jsonl": "out/review.jsonl", "sft_output": "out/sft.jsonl"},
}


def test_distill_freezes_a_script_parameter_into_the_package(
        service, host, pipeline_workspace, tool_entry):
    """The session's converter is a script, not an input: the package has to carry it."""
    pipeline_workspace.write("scripts/to_sft.py", TO_SFT)
    plan = prepare(service, {
        **CONVERTER_ARGS,
        "trace_refs": converter_session(tool_entry, pipeline_workspace),
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {
            "answers_jsonl": "out/review.jsonl",
            "sft_output": "out/sft.jsonl",
        },
    }, host=host)
    confirmed = confirm(service, plan, host)
    distilled = confirmed["preflight"]
    assert distilled["materialized"] == [{"key": "converter_script",
                                          "path": "scripts/to_sft.py"}]
    assert [item["key"] for item in distilled["params"]] == ["answers_jsonl", "sft_output"]
    assert distilled["scripts"] == [{"source": "scripts/to_sft.py",
                                     "package": "scripts/to_sft.py"}]
    # 脚本已随包搬运，不再是一条未固化的输入边界。
    assert distilled["chain"]["boundaries"] == []
    package = Path(distilled["package"])
    assert (package / "scripts/to_sft.py").read_text(encoding="utf-8") == TO_SFT
    graph = (package / "graph.yaml").read_text(encoding="utf-8")
    assert "converter_script" not in graph
    assert "scripts/to_sft.py" in graph
    assert "脚本参数已物化" in Path(distilled["report"]).read_text(encoding="utf-8")

    state = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is True


def test_distill_keeps_an_unreadable_script_parameter_as_a_boundary(
        service, host, pipeline_workspace, tool_entry):
    plan = prepare(
        service,
        {
            **CONVERTER_ARGS,
            "trace_refs": converter_session(tool_entry, pipeline_workspace),
            "validation_params": dict(CONVERTER_ARGS["params"]),
            "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        },
        "call-1",
        host,
    )
    assert plan["review"]["scripts"] == [
        {
            "path": "{converter_script}",
            "resolved": "scripts/to_sft.py",
            "status": "preflight_pending",
        }
    ]
    confirmed = confirm(service, plan, host)
    distilled = confirmed["preflight"]
    assert distilled["materialized"] == []
    assert [item["key"] for item in distilled["params"]] == [
        "answers_jsonl", "converter_script", "sft_output"]
    assert len(distilled["chain"]["boundaries"]) == 1
    assert "读不到" in distilled["materialize_notes"][0]


def test_distill_freezes_a_cli_with_its_runtime_helpers(
        service, host, pipeline_workspace, tool_entry):
    """A skill CLI is not a single file: the siblings it names travel with it."""
    pipeline_workspace.write(
        "scripts/runner.py",
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "subprocess.run([sys.executable, Path(__file__).parent / 'helper.py'])\n")
    pipeline_workspace.write("scripts/helper.py", "print('helper')\n")
    document = {
        "trace_version": 1, "trace_id": "s1", "session_id": "s1",
        "session_name": "运行 CLI", "step_count": 1,
        "steps": [tool_entry("c1", "python3 scripts/runner.py out/review.jsonl "
                                   "out/clean.jsonl")],
    }
    pipeline_workspace.write("public_data/agentgenome-traces/s1.json",
                             json.dumps(document))
    plan = prepare(service, {
        "label": "运行 CLI", "asset_id": "runner-cli",
        "steps": [{"run": "python3 {runner_script} {review_jsonl} {clean_output}"}],
        "params": {"runner_script": "scripts/runner.py",
                   "review_jsonl": "out/review.jsonl",
                   "clean_output": "out/clean.jsonl"},
        "trace_refs": ["public_data/agentgenome-traces/s1.json"],
        "validation_params": {
            "review_jsonl": "out/review.jsonl",
            "clean_output": "out/clean.jsonl",
        },
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
    }, "call-1", host)
    confirmed = confirm(service, plan, host)
    distilled = confirmed["preflight"]
    assert [item["package"] for item in distilled["scripts"]] == [
        "scripts/helper.py", "scripts/runner.py"]
    package = Path(distilled["package"])
    assert (package / "scripts/helper.py").read_text(encoding="utf-8") == (
        "print('helper')\n")


def test_distill_requires_params_for_placeholder_inputs(service, host, pipeline_workspace):
    steps = [{"run": "python3 scripts/clean.py {review_jsonl} > out/clean.csv"}]
    plan = prepare(
        service,
        distill_args(steps=steps, final_artifact="out/clean.csv"),
        "call-1",
        host,
    )
    assert any("review_jsonl" in item for item in plan["review"]["unresolved"])
    assert any("review_jsonl" in item for item in plan["blocking_issues"])
    with pytest.raises(ValueError, match="review_jsonl"):
        confirm(service, plan, host)


LLM_CHAIN = [
    {"llm": "读 {data_file} 里的问题，逐条生成回答，把 {text, answer} 写到 out/answers.jsonl"
            "（JSONL）。",
     "input": ["{data_file}"], "output": "out/answers.jsonl"},
    {"run": "python3 scripts/clean.py out/answers.jsonl > out/clean.csv"},
    {"run": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt"},
    {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv > reports/metrics.json"},
]


TOOL_CHAIN = [
    {"tool": "df_submit_pipeline",
     "args": {"dataset": "data/raw.csv", "task_name": "pretag", "wait": False},
     "output": "out/submission.json"},
    {"run": "python3 scripts/wait_pipeline.py out/submission.json out/processed.jsonl",
     "output": "out/processed.jsonl"},
    {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"},
    {"run": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt"},
    {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv > reports/metrics.json"},
]


def test_a_root_input_may_not_be_a_trial_slice(
        pipeline_workspace, trace_factory, tool_entry):
    """`head -10 x > x_10` 的产物是试跑范围，不是模板的输入契约。"""
    trace = trace_factory([
        tool_entry("c1", "head -10 data/raw.csv > data/raw_10.csv"),
    ], trace_id="s1")
    draft = draft_distill(
        label="试跑十条后清洗",
        steps=[{"run": "python3 scripts/clean.py {raw} > out/clean.csv"}],
        params={"raw": "data/raw_10.csv"},
        input_contract={"raw": {"type": "path"}},
        final_artifact="out/clean.csv",
        workspace=pipeline_workspace.root,
        traces=[trace],
    )

    review = draft.review({"will_start_replay": True})

    # The plan still runs on the slice; it is worth capturing, just narrower.
    assert any("试跑切片" in item and "data/raw.csv" in item for item in review["warnings"])


def test_distill_rejects_the_platform_chain_that_skipped_the_wait(
        service, host, pipeline_workspace):
    """真实沉淀出来的那条链：提交平台后直接导入 Label Studio。

    平台任务还是 Pending，复核 JSONL 根本不存在，导入必然报
    `storage object not found`。这种图必须在校验阶段就被拒绝。
    """
    steps = [
        {"tool": "df_submit_pipeline",
         "args": {"input_path": "{manifest}", "output_schema": "structured",
                  "script_path": "scripts/prelabel.py"},
         "output": "out/receipt.json", "keep": True},
        {"tool": "label_studio_project",
         "args": {"operation": "create", "dataset_path": "{review}"},
         "output": "out/project.json", "keep": True},
        {"human": "复核 {review} 并导出到 out/export.json", "input": ["{review}"],
         "output": "out/export.json"},
        {"run": "python3 scripts/to_sft.py out/export.json out/sft.jsonl",
         "output": "out/sft.jsonl"},
    ]
    with pytest.raises(DistillError, match="异步任务"):
        distill(service, label="提交后直接导入", steps=steps, workspace=host,
                params={"manifest": "data/raw.csv", "review": "out/review.jsonl"},
                final_artifact="out/sft.jsonl")


def test_a_wait_step_turns_the_receipt_into_the_product(
        service, host, pipeline_workspace):
    """提交与等待是两个节点：受理单给等待步骤，产物给下游。

    平台提交还带审批门禁，回放不会自己再提交一次。
    """
    plan = distill(service, label="提交后等待", steps=TOOL_CHAIN[:2], workspace=host)
    assert [node.kind for node in plan.package.nodes] == ["tool", "await"]
    graph = (plan.package.directory / "graph.yaml").read_text(encoding="utf-8")
    assert "name: df_submit_pipeline" in graph
    assert "danger: true" in graph
    assert "await:" in graph
    assert "wait_pipeline.py" not in graph


def test_distill_rejects_an_async_tool_without_a_wait_step(
        service, host, pipeline_workspace):
    """平台受理不等于产物就绪：提交后必须等终态，否则下游读到空目录。"""
    steps = [
        {"tool": "df_submit_pipeline",
         "args": {"dataset": "data/raw.csv", "task_name": "pretag"},
         "output": "out/submission.json"},
        {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"},
    ]
    with pytest.raises(DistillError, match="异步任务"):
        distill(service, label="提交后直接清洗", steps=steps, workspace=host)


def test_distill_accepts_an_async_tool_followed_by_a_wait_step(
        service, host, pipeline_workspace):
    plan = distill(service, label="提交后等待", steps=TOOL_CHAIN[:2],
                   workspace=host)
    assert [node.kind for node in plan.package.nodes] == ["tool", "await"]


def test_an_await_step_is_recognised_by_consuming_the_receipt(
        service, host, pipeline_workspace):
    """等待是数据关系，不是脚本名。

    判据是这一步读了受理单；换个名字（`poll_task.py`）不该被当成"没等待"。
    """
    pipeline_workspace.write("scripts/poll_task.py", "import sys\n")
    steps = [
        {"tool": "df_submit_pipeline",
         "args": {"dataset": "data/raw.csv", "task_name": "pretag"},
         "output": "out/submission.json", "keep": True},
        {"run": "python3 scripts/poll_task.py out/submission.json out/processed.jsonl",
         "output": "out/processed.jsonl"},
        {"run": "python3 scripts/clean.py out/processed.jsonl > out/clean.csv",
         "output": "out/clean.csv"},
    ]
    plan = distill(service, label="提交后轮询", steps=steps, workspace=host,
                   final_artifact="out/clean.csv")
    # 脚本叫什么不影响判据：消费受理单的那一步固化成 await，而不是 run。
    assert [node.kind for node in plan.package.nodes] == ["tool", "await", "run"]


def test_a_run_step_that_ignores_the_receipt_is_not_an_await(
        service, host, pipeline_workspace):
    """只碰巧排在提交后面的步骤不算等待——它没读受理单，就不知道任务结束没有。"""
    steps = [
        {"tool": "df_submit_pipeline",
         "args": {"dataset": "data/raw.csv", "task_name": "pretag"},
         "output": "out/submission.json", "keep": True},
        {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv",
         "output": "out/clean.csv"},
    ]
    with pytest.raises(DistillError, match="异步任务"):
        distill(service, label="提交后直接清洗", steps=steps, workspace=host,
                final_artifact="out/clean.csv")


def test_a_bare_word_is_not_probed_as_a_missing_input(
        service, host, pipeline_workspace):
    """`project_ref: pcb_prelabel_10_review` 是名字不是路径。

    拿它去问工作区"这个文件在吗"，会把一个标量变成一个假的缺失输入。
    """
    from agentgenome.capture.distill import _looks_like_path

    assert _looks_like_path("data/raw.csv")
    assert _looks_like_path("out/review.jsonl")
    assert _looks_like_path("./scripts/clean.py")
    assert not _looks_like_path("pcb_prelabel_10_review")
    assert not _looks_like_path("vision")

    steps = [
        {"tool": "label_studio_project",
         "args": {"operation": "create", "dataset_path": "{review}",
                  "project_ref": "pcb_prelabel_10_review"},
         "output": "out/project.json"},
        {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv",
         "output": "out/clean.csv"},
    ]
    plan = prepare(
        service,
        {
            "label": "导入复核",
            "steps": steps,
            "final_artifact": "out/clean.csv",
            **MINIMAL_VALIDATION,
        },
        "call-bare-word",
        host,
    )
    assert not any(
        "pcb_prelabel_10_review" in issue for issue in plan["blocking_issues"]
    )


def test_resubmitting_the_same_problems_answers_with_the_plan_on_the_table(
        service, host, pipeline_workspace):
    """没改任何东西的重提，不该开一条新计划让用户以为进展了一轮。"""
    arguments = distill_args()
    arguments.pop("execution_scope")
    arguments.pop("validation_params")

    first = prepare(service, arguments, "call-same-1", host)
    second = prepare(service, arguments, "call-same-2", host)

    assert first["blocking_issues"]
    assert second.get("repeated") is True
    assert second["plan_id"] == first["plan_id"]
    assert second["blocking_issues"] == first["blocking_issues"]


def test_an_omitted_validation_scope_defaults_to_the_captured_inputs(
        service, host, pipeline_workspace):
    """验证回放就是用原参数再跑一遍，不该让调用方把参数抄第二遍。"""
    arguments = distill_args()
    arguments["params"] = {"raw": "data/raw.csv", "rules": "rules.json"}
    arguments.pop("execution_scope")
    arguments.pop("validation_params")

    plan = prepare(service, arguments, "call-default-scope", host)

    validation = plan["review"]["validation"]
    assert validation["ready"] is True
    assert validation["mode"] == "sample"
    assert validation["params"] == {"raw": "data/raw.csv", "rules": "rules.json"}
    assert not any("bounded validation" in item for item in plan["blocking_issues"])


def test_a_plan_may_not_remember_a_sandbox_mount_path(
        service, host, pipeline_workspace):
    """挂载前缀是执行环境的临时拼写，写进契约下次回放就不存在了。"""
    arguments = distill_args()
    arguments["params"] = {
        "raw": "/target-workspace/.pyromind-agent/abc/data/raw.csv",
        "rules": "rules.json",
    }

    plan = prepare(service, arguments, "call-mount-spelling", host)

    warnings = plan["review"]["warnings"]
    assert any("沙箱挂载路径" in item for item in warnings)
    assert any("data/raw.csv" in item for item in warnings)


def test_a_manifest_sliced_out_of_storage_must_stay_beside_it(
        service, host, pipeline_workspace):
    """清单里的图片按清单所在目录解析：切片产物挪进运行目录，图就找不到了。"""
    steps = [
        {"run": ("python3 scripts/slice_manifest.py "
                 "--in storage/datasets/test_100/prelabel_manifest.jsonl "
                 "--out out/prelabel_subset.jsonl"),
         "output": "out/prelabel_subset.jsonl"},
    ]
    plan = prepare(
        service,
        distill_args(steps=steps, final_artifact="out/prelabel_subset.jsonl"),
        "call-relocation",
        host,
    )
    blocking = plan["review"]["blocking_issues"]
    assert any("运行目录" in item and "/datasets/test_100" in item
               for item in blocking)


def test_a_final_artifact_written_inside_a_script_says_how_to_declare_it(
        service, host, pipeline_workspace):
    """脚本内部写产物时，报错要直接说清怎么改，别再让用户猜一轮。"""
    steps = [
        {"run": "python3 scripts/clean.py data/raw.csv > out/clean.csv",
         "output": "out/clean.csv"},
        {"run": "python3 scripts/to_sft.py out/clean.csv out/sft_run.jsonl"},
    ]
    with pytest.raises(DistillError) as error:
        distill(service, label="发布到 Storage", steps=steps, workspace=host,
                final_artifact="datasets/sft.jsonl")
    message = str(error.value)
    assert "final_artifact" in message
    assert "显式输出" in message or "output" in message


def test_distill_promotes_a_declared_tool_call_to_a_tool_node(
        service, host, pipeline_workspace):
    plan = distill(service, label="提交平台预打标", steps=TOOL_CHAIN, workspace=host)
    assert [node.kind for node in plan.package.nodes] == [
        "tool", "await", "run", "run", "run"]
    node = plan.package.nodes[0]
    assert node.id == "df-submit-pipeline"
    assert node.tool_name == "df_submit_pipeline"
    assert dict(node.arguments) == {"dataset": "{input.raw}", "task_name": "pretag",
                                    "wait": False}
    graph = (plan.package.directory / "graph.yaml").read_text(encoding="utf-8")
    assert "do:\n      tool:" in graph
    assert "name: df_submit_pipeline" in graph
    assert plan.document["steps"][0]["kind"] == "tool_call"
    assert plan.document["steps"][0]["output"] == "out/submission.json"
    assert plan.payload()["steps"][0]["tool"] == "df_submit_pipeline"
    assert "run" not in plan.payload()["steps"][0]


def test_distill_does_not_submit_to_the_platform_without_approval(
        service, host, pipeline_workspace):
    """平台提交是外部副作用：回放停在审批门禁，不替用户按下提交。

    这条链路是从一次真实运行里沉淀出来的，但“那次提交过”不等于“以后每次都自动提交”。
    """
    plan = prepare(service, {
        "label": "提交平台预打标", "steps": TOOL_CHAIN,
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {
            "raw": "data/raw.csv",
            "review": "out/review.jsonl",
        },
    }, host=host)
    confirmed = confirm(service, plan, host)
    state = service.execute("conv-1", confirmed["run_id"], host, lambda event: None)
    assert state["status"] != "succeeded"
    artifacts = service.root / "runs" / confirmed["run_id"] / "work" / "artifacts"
    submitted = [json.loads(path.read_text(encoding="utf-8"))
                 for path in sorted(artifacts.glob("*.json"))
                 if path.is_file()]
    assert not [body for body in submitted
                if body.get("tool") == "df_submit_pipeline"]


def test_distill_rejects_a_tool_step_without_an_artifact(
        service, host, pipeline_workspace):
    steps = [{"tool": "df_submit_pipeline", "args": {"dataset": "data/raw.csv"}}]
    with pytest.raises(DistillError, match="output"):
        distill(service, label="x", steps=steps, workspace=host)


def test_distill_rejects_a_tool_step_whose_input_is_not_in_the_args(
        service, host, pipeline_workspace):
    steps = [{"tool": "df_submit_pipeline", "args": {"task_name": "pretag"},
              "input": ["data/raw.csv"], "output": "out/submission.json"}]
    with pytest.raises(DistillError, match="args 里找不到"):
        distill(service, label="x", steps=steps, workspace=host)


def test_distill_rejects_a_bad_tool_declaration(service, host, pipeline_workspace):
    bad_name = [{"tool": "DF Submit", "output": "out/submission.json"}]
    with pytest.raises(DistillError, match="不合法"):
        distill(service, label="x", steps=bad_name, workspace=host)
    bad_args = [{"tool": "df_submit_pipeline", "args": ["data/raw.csv"],
                 "output": "out/submission.json"}]
    with pytest.raises(DistillError, match="args 必须是对象"):
        distill(service, label="x", steps=bad_args, workspace=host)
    mixed = [{"run": "echo hi", "tool": "df_submit_pipeline"}]
    with pytest.raises(DistillError, match="只能取其一"):
        distill(service, label="x", steps=mixed, workspace=host)


def test_distill_rejects_a_session_tool_call_declared_as_llm(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    trace = trace_factory([
        tool_entry("c1", "", tool="df_submit_pipeline",
                   args={"dataset": "data/raw.csv", "task_name": "pretag"}),
    ], trace_id="s1")
    steps = [{"llm": "提交 {data_file} 到平台并写 out/submission.json",
              "input": ["{data_file}"], "output": "out/submission.json", "from": "s1:0"}]
    with pytest.raises(DistillError, match="固定工具调用"):
        distill(service, label="x", steps=steps, workspace=host,
                params={"data_file": "data/raw.csv"}, traces=[trace])


def stage_driver(service, scope, *, body="ok"):
    """Answer a waiting_agent event: write the artifact, then submit the receipt."""
    def on_event(event):
        if event.get("event") != "waiting_agent":
            return None
        request = event["request"]
        output = Path(request["expected_outputs"][0])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body)
        service.stages.receipt(scope, request["request_id"], "completed", "done", "round")
        return {"status": "completed", "execution_id": "round"}

    return on_event


def test_distill_promotes_a_declared_llm_step_to_a_llm_node(
        service, host, pipeline_workspace):
    plan = prepare(service, {
        "label": "生成回答到评测", "asset_id": "generate-answer-eval",
        "steps": LLM_CHAIN, "final_artifact": "reports/metrics.json",
        "params": {"data_file": "data/raw.csv"},
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {"data_file": "data/raw.csv"},
    }, host=host)
    assert [step["kind"] for step in plan["review"]["steps"]] == [
        "llm", "run", "run", "run"]
    confirmed = confirm(service, plan, host)
    distilled = confirmed["preflight"]
    assert [node["kind"] for node in distilled["nodes"]] == ["llm", "run", "run", "run"]
    llm_node = distilled["nodes"][0]
    assert llm_node["id"] == "answers"
    graph = (Path(distilled["package"]) / "graph.yaml").read_text(encoding="utf-8")
    assert "do:\n      llm:" in graph
    assert "{input.data_file}" in graph
    assert "{artifact}" in graph
    # 根输入只剩 {data_file}：llm 的产出由节点携带，不再是一条输入边界。
    assert [item["key"] for item in distilled["params"]] == ["data_file"]
    assert distilled["chain"]["boundaries"] == []

    state = service.execute("conv-1", confirmed["run_id"], host,
                            stage_driver(service, "conv-1",
                                         body=json.dumps({"text": "p-one", "label": "a"}) + "\n"
                                         + json.dumps({"text": "p-two", "label": "b"}) + "\n"))
    assert state["status"] == "succeeded"
    assert service.get(confirmed["asset_id"], confirmed["version"])["published"] is True


def test_distill_rejects_an_llm_prompt_that_drops_its_declared_paths(
        service, host, pipeline_workspace):
    steps = [{"llm": "读输入生成回答", "input": ["{data_file}"],
              "output": "out/answers.jsonl"},
             *LLM_CHAIN[1:]]
    with pytest.raises(DistillError, match="原样出现"):
        invoke(service, "conv-1", "distill", {
            "label": "x", "steps": steps, "final_artifact": "reports/metrics.json",
            "params": {"data_file": "data/raw.csv"}}, "call-1", host=host)


def test_plan_steps_rejects_an_llm_token_that_is_not_declared():
    """A verify-only `{r}` has no input slot: emit would compile it to a literal."""
    with pytest.raises(DistillError, match="未声明的占位符"):
        plan_steps([{"llm": "按 {r} 为 {c} 逐条打标，写 {l}", "output": "{l}",
                     "verify": "python3 scripts/verify_labels.py "
                               "--labels {l} --cleaned {c} --label-rules {r}"}])


def test_plan_steps_rejects_a_human_prompt_token_that_is_not_declared():
    with pytest.raises(DistillError, match="未声明的占位符"):
        plan_steps([{"human": "复核 {q} 并把决定写到 {dec}", "output": "{dec}"}])


def test_plan_steps_rejects_a_run_verify_token_off_the_command_line():
    with pytest.raises(DistillError, match="未声明的占位符"):
        plan_steps([{"run": "python3 scripts/build.py --in data/raw.csv --out out/r.json",
                     "verify": "python3 scripts/check.py --rules {r}"}])


def test_plan_steps_accepts_an_llm_step_whose_tokens_are_all_declared():
    steps = plan_steps([{"llm": "按 {r} 为 {c} 逐条打标，写 {l}", "input": ["{c}", "{r}"],
                         "output": "{l}",
                         "verify": "python3 scripts/verify_labels.py "
                                   "--labels {l} --cleaned {c} --label-rules {r}"}])
    assert steps[0].reads == ("{c}", "{r}")
    assert steps[0].verify is not None


def test_plan_steps_leaves_engine_refs_in_a_run_verify_alone():
    steps = plan_steps([{"run": "python3 scripts/build.py --out out/r.json",
                         "verify": "test -s {output.r}"}])
    assert steps[0].verify == "test -s {output.r}"


def test_freezing_a_script_token_rewrites_the_verify_too(pipeline_workspace, host):
    from agentgenome.capture import distill as distill_module
    pipeline_workspace.write("scripts/prep.py", "print('prep')\n")
    planned = plan_steps([{"run": "python3 {prep} --in data/raw.csv --out out/p.jsonl",
                           "verify": "python3 {prep} --selfcheck"}])
    rewritten, _, materialized, _ = distill_module._materialize_scripts(
        planned, {"prep": "scripts/prep.py"}, host)
    assert materialized == (("prep", "scripts/prep.py"),)
    assert "scripts/prep.py" in rewritten[0].run
    assert rewritten[0].verify == "python3 scripts/prep.py --selfcheck"


def test_draft_distill_flags_a_params_key_no_step_produces(service, host, trace_factory,
                                                            tool_entry):
    """`--reviewed {rev}` parses as a read: a params key nobody writes must be flagged.

    Live failure: the phantom root input passed replay (no existence check) and only
    broke at reuse compatibility, so the review the operator confirms has to name it.
    """
    trace = trace_factory([
        tool_entry("c1", "python3 scripts/build_report.py --cleaned data/clean.csv "
                         "--labels data/labels.csv --reviewed {rev} --report out/rep.json"),
    ], trace_id="s1")
    draft = draft_distill(
        label="x",
        steps=[{"run": "python3 scripts/build_report.py --cleaned {cleaned} "
                        "--labels {labels} --reviewed {rev} --report {rep}",
                "output": "{rep}"}],
        workspace=host, final_artifact="{rep}",
        params={"cleaned": "data/clean.csv", "labels": "data/labels.csv",
                "rev": "out/reviewed.csv", "rep": "out/rep.json"},
        input_contract={"cleaned": {"type": "path"}, "labels": {"type": "path"}},
        traces=[trace])
    assert any("rev" in item and "幽灵根输入" in item for item in draft.unresolved)


def test_distill_rejects_a_fixed_command_declared_as_llm(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    trace = trace_factory([
        tool_entry("c1", "python3 sandbox_sample.py --pipeline scripts/prelabel.py "
                         "--input data/raw.csv --output out/answers.jsonl"),
    ], trace_id="s1")
    steps = [{"llm": "读 {data_file} 生成回答到 out/answers.jsonl",
              "input": ["{data_file}"], "output": "out/answers.jsonl", "from": "s1:0"},
             *LLM_CHAIN[1:]]
    with pytest.raises(DistillError, match="固定命令"):
        distill(service, label="x", steps=steps, workspace=host,
                final_artifact="reports/metrics.json",
                params={"data_file": "data/raw.csv"}, traces=[trace])


def test_distill_rejects_an_llm_step_without_a_from_link(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """scripts-first: an unlinked llm node is indistinguishable from a promoted command.

    The session ran the filter as a fixed command; without `from` the plugin cannot
    tell whether the llm node is genuine discretion or that command in disguise, so
    the default policy rejects it and lists the session steps to pick from.
    """
    trace = trace_factory([
        tool_entry("c1", "python3 sandbox_sample.py --pipeline scripts/prelabel.py "
                         "--input data/raw.csv --output out/answers.jsonl"),
    ], trace_id="s1")
    steps = [{"llm": "读 {data_file} 生成回答到 out/answers.jsonl",
              "input": ["{data_file}"], "output": "out/answers.jsonl"},
             *LLM_CHAIN[1:]]
    with pytest.raises(DistillError, match="没有 from") as error:
        distill(service, label="x", steps=steps, workspace=host,
                final_artifact="reports/metrics.json",
                params={"data_file": "data/raw.csv"}, traces=[trace])
    assert "s1:0" in str(error.value)
    assert "policy=adaptive-first" in str(error.value)


def test_distill_accepts_an_llm_step_without_a_from_link_under_adaptive_policy(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """adaptive-first trusts the agent: the same unlinked llm step passes."""
    trace = trace_factory([
        tool_entry("c1", "python3 sandbox_sample.py --pipeline scripts/prelabel.py "
                         "--input data/raw.csv --output out/answers.jsonl"),
    ], trace_id="s1")
    steps = [{"llm": "读 {data_file} 生成回答到 out/answers.jsonl",
              "input": ["{data_file}"], "output": "out/answers.jsonl"},
             *LLM_CHAIN[1:]]
    plan = distill(service, label="x", steps=steps, workspace=host,
                   final_artifact="reports/metrics.json",
                   params={"data_file": "data/raw.csv"}, traces=[trace],
                   policy="adaptive-first")
    assert [node.kind for node in plan.package.nodes] == ["llm", "run", "run", "run"]
    assert plan.package.nodes[0].id == "answers"


def test_distill_rejects_an_unknown_policy(service, host, pipeline_workspace):
    with pytest.raises(DistillError, match="policy"):
        distill(service, label="x", steps=CHAIN, workspace=host,
                final_artifact=FINAL, policy="yolo-first")


def test_distill_links_a_literal_llm_output_into_the_next_step(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """The real case: literal intermediate paths, two llm steps, one converter.

    `sandbox_sample.py` ran the filter and answer pipelines as fixed commands in the
    session; under `adaptive-first` the plan turns both into llm nodes without `from`,
    keeps their literal artifact paths, and links the converter to the second node's
    output.
    """
    pipeline_workspace.write("scripts/convert_to_messages.py", TO_MESSAGES)
    trace = trace_factory([
        tool_entry("c1", "python3 .agents/skills/data-processing/scripts/preparation/"
                         "sandbox_sample.py --pipeline public_data/data-preparation/"
                         "filter_malicious.py --input public_data/data-preparation/"
                         "testdata_3.jsonl --output public_data/data-preparation/"
                         "processed.jsonl --model-profile text --output-schema text "
                         "--timeout 1800"),
        tool_entry("c2", "python3 .agents/skills/data-processing/scripts/preparation/"
                         "sandbox_sample.py --pipeline public_data/data-preparation/"
                         "generate_answers.py --input public_data/data-preparation/"
                         "processed.jsonl --output public_data/data-preparation/"
                         "answers.jsonl --model-profile text --output-schema text "
                         "--timeout 1800"),
    ], trace_id="s1")
    steps = [
        {"llm": "读取 {data_file} 中的问题，过滤掉恶意/有害问题，把保留的问题写入 "
                "public_data/data-preparation/processed.jsonl（JSONL，每行保留 qid 与 "
                "question_text）。",
         "input": ["{data_file}"],
         "output": "public_data/data-preparation/processed.jsonl"},
        {"llm": "读取 public_data/data-preparation/processed.jsonl 中的问题，逐条生成回答，"
                "把 {qid, question_text, answer} 写入 public_data/data-preparation/"
                "answers.jsonl（JSONL）。",
         "input": ["public_data/data-preparation/processed.jsonl"],
         "output": "public_data/data-preparation/answers.jsonl"},
        {"run": "python3 scripts/convert_to_messages.py "
                "--input public_data/data-preparation/answers.jsonl "
                "--output public_data/data-preparation/sft_train.jsonl"},
    ]
    plan = distill(service, label="QA 过滤生成转 SFT", steps=steps, workspace=host,
                   final_artifact="public_data/data-preparation/sft_train.jsonl",
                   params={"data_file": "public_data/data-preparation/testdata_3.jsonl"},
                   traces=[trace], policy="adaptive-first")
    assert [node.kind for node in plan.package.nodes] == ["llm", "llm", "run"]
    assert [node.id for node in plan.package.nodes] == [
        "processed", "answers", "convert-to-messages"]
    graph = plan.package.graph
    answers = next(child for child in graph.node.children if child.id == "answers")
    assert answers.input["processed"].source.node_path.endswith("processed")


def test_distill_rejects_the_literal_case_under_scripts_first(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """The same literal plan fails by default: both llm nodes ran as fixed commands."""
    pipeline_workspace.write("scripts/convert_to_messages.py", TO_MESSAGES)
    trace = trace_factory([
        tool_entry("c1", "python3 sandbox_sample.py --pipeline "
                         "public_data/data-preparation/filter_malicious.py "
                         "--input public_data/data-preparation/testdata_3.jsonl "
                         "--output public_data/data-preparation/processed.jsonl"),
    ], trace_id="s1")
    steps = [
        {"llm": "读取 {data_file} 过滤恶意问题，把保留的问题写入 "
                "public_data/data-preparation/processed.jsonl",
         "input": ["{data_file}"],
         "output": "public_data/data-preparation/processed.jsonl"},
        {"run": "python3 scripts/convert_to_messages.py "
                "--input public_data/data-preparation/processed.jsonl "
                "--output public_data/data-preparation/sft_train.jsonl"},
    ]
    with pytest.raises(DistillError, match="没有 from"):
        distill(service, label="x", steps=steps, workspace=host,
                final_artifact="public_data/data-preparation/sft_train.jsonl",
                params={"data_file": "public_data/data-preparation/testdata_3.jsonl"},
                traces=[trace])


def test_distill_rejects_a_human_gate_declared_as_llm(
        service, host, pipeline_workspace, trace_factory, user_entry):
    trace = trace_factory([user_entry("u1", "我在 Label Studio 复核完了")], trace_id="s1")
    steps = [{"llm": "读 {data_file} 生成回答到 out/answers.jsonl",
              "input": ["{data_file}"], "output": "out/answers.jsonl", "from": "s1:0"},
             *LLM_CHAIN[1:]]
    with pytest.raises(DistillError, match="人工门禁"):
        distill(service, label="x", steps=steps, workspace=host,
                final_artifact="reports/metrics.json",
                params={"data_file": "data/raw.csv"}, traces=[trace])


def test_distill_rejects_params_that_do_not_match_the_chain_inputs(
        service, host, pipeline_workspace):
    # An unused key is not a defect; a *missing* root is. Extra params are ignored.
    plan = prepare(
        service,
        distill_args(params={"ghost": "data/raw.csv"}),
        "call-1",
        host,
    )
    assert plan["review"]["validation"]["ready"] is True


def test_distill_takes_chain_placeholders_in_params(service, host, pipeline_workspace):
    """Agents name every token their commands use, including intermediate products.

    `{clean_csv}` is written by step 1 and read by steps 2 and 3, so it is a chain
    binding, not a root input: the replay keeps only the root input.
    """
    steps = [
        {"run": "python3 scripts/clean.py {review_jsonl} > {clean_csv}"},
        {"run": "python3 scripts/train.py {clean_csv} --epochs 3 --output models/model.pt"},
        {"run": "python3 scripts/evaluate.py models/model.pt {clean_csv} "
                "> reports/metrics.json"},
    ]
    plan = distill(service, label="预打标到评测", steps=steps, workspace=host,
                   final_artifact="reports/metrics.json",
                   params={"review_jsonl": "out/review.jsonl",
                           "clean_csv": "out/clean.csv"})
    assert plan.bound == (("review_jsonl", "out/review.jsonl"),)
    assert [node.id for node in plan.package.nodes] == ["clean", "train", "evaluate"]


def test_distill_matches_a_concrete_final_artifact_to_a_placeholder_output(
        service, host, pipeline_workspace):
    """The plan writes `{metrics_json}` while the artifact is named by its path."""
    steps = [
        {"run": "python3 scripts/clean.py {review_jsonl} > out/clean.csv"},
        {"run": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt"},
        {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv "
                "> {metrics_json}"},
    ]
    plan = distill(service, label="预打标到评测", steps=steps, workspace=host,
                   final_artifact="reports/metrics.json",
                   params={"review_jsonl": "out/review.jsonl",
                           "metrics_json": "reports/metrics.json"})
    assert plan.package.final_artifact == "{metrics_json}"
    assert [node.id for node in plan.package.nodes] == ["clean", "train", "evaluate"]
    assert plan.bound == (("review_jsonl", "out/review.jsonl"),)


def test_distill_matches_a_placeholder_final_artifact_to_a_literal_output(
        service, host, pipeline_workspace):
    """The artifact is declared as `{clean_csv}` while step 1 writes the path."""
    steps = [
        {"run": "python3 scripts/clean.py {review_jsonl} > out/clean.csv"},
        {"run": "python3 scripts/train.py {clean_csv} --epochs 3 --output models/model.pt"},
        {"run": "python3 scripts/evaluate.py models/model.pt {clean_csv} "
                "> reports/metrics.json"},
    ]
    plan = distill(service, label="预打标到评测", steps=steps, workspace=host,
                   final_artifact="{clean_csv}",
                   params={"review_jsonl": "out/review.jsonl",
                           "clean_csv": "out/clean.csv"})
    assert plan.package.final_artifact == "out/clean.csv"
    # 切片停在产物本身：训练与评测不依赖它，是辅助步骤。
    assert [node.id for node in plan.package.nodes] == ["clean"]


def test_confirm_keeps_a_placeholder_final_artifact_as_the_output(
        service, host, pipeline_workspace, tool_entry):
    trace_path = "public_data/agentgenome-traces/s1.json"
    pipeline_workspace.write(trace_path, json.dumps({
        "trace_version": 1,
        "trace_id": "s1",
        "session_id": "s1",
        "session_name": "复核导出转 SFT",
        "step_count": 1,
        "steps": [
            tool_entry(
                "a1",
                "python3 scripts/clean.py out/review.jsonl > out/sft.jsonl",
            ),
        ],
    }))
    arguments = {
        "label": "复核导出转 SFT",
        "steps": [
            {"run": "python3 scripts/clean.py {review} > {sft_jsonl}"},
        ],
        "final_artifact": "{sft_jsonl}",
        "params": {
            "review": "out/review.jsonl",
            "sft_jsonl": "out/sft.jsonl",
        },
        "source_traces": ["s1"],
        "trace_refs": [{"trace_id": "s1", "path": trace_path}],
        **MINIMAL_VALIDATION,
    }
    # Validation overrides name root inputs; sft_jsonl is an output binding.
    arguments["validation_params"] = {"review": "out/review.jsonl"}

    plan = prepare(service, arguments, host=host)
    assert plan["blocking_issues"] == []

    confirmed = confirm(service, plan, host)
    assert confirmed["distill_plan"]["status"] == "confirmed"
    assert confirmed["preflight"]["final_artifact"] == "{sft_jsonl}"


def test_distill_reads_a_literal_path_a_placeholder_step_produced(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """Step 1 writes `{clean_csv}`; step 2 reads the path that placeholder binds to.

    The session ran step 1 as a fixed command, so without normalizing the two
    spellings the chain check takes the literal path for an unplanned producer and
    reports an input boundary.
    """
    trace = trace_factory([
        tool_entry("c1", "python3 scripts/clean.py out/review.jsonl > out/clean.csv"),
        tool_entry("c2", "python3 scripts/train.py out/clean.csv --epochs 3 "
                         "--output models/model.pt"),
    ], trace_id="s1")
    steps = [
        {"run": "python3 scripts/clean.py {review_jsonl} > {clean_csv}"},
        {"run": "python3 scripts/train.py out/clean.csv --epochs 3 "
                "--output models/model.pt"},
    ]
    plan = distill(service, label="清洗到训练", steps=steps, workspace=host,
                   final_artifact="models/model.pt",
                   params={"review_jsonl": "out/review.jsonl",
                           "clean_csv": "out/clean.csv"},
                   traces=[trace])
    assert plan.boundaries == ()
    assert [node.id for node in plan.package.nodes] == ["clean", "train"]
    # 两种写法是同一个文件：训练接上清洗节点的产出，而不是多出一条根输入。
    assert plan.package.params == (("review_jsonl", "{review_jsonl}"),)
    train = next(node for node in plan.package.nodes if node.id == "train")
    assert train.inputs == (("clean_csv", "path", "clean.output.clean_csv"),)


def test_an_intermediate_token_product_is_rejected():
    """A run step's own product may not be a placeholder.

    A placeholder leaves emit without a literal file name, so `artifact.at` cannot pin
    where the file belongs. A manifest that names sibling images then lands in the run
    directory while the images stay in Storage, and the platform rejects every relative
    path — the failure that made `675361cc…` look like a path problem.
    """
    steps = [
        {"run": "python3 scripts/slice.py {manifest} {sliced} {records}",
         "output": ["{sliced}"]},
        {"run": "python3 scripts/run.py {sliced} > out/result.jsonl"},
    ]
    planned = plan_steps(steps)
    issues = _intermediate_token_issues(
        planned, {"manifest": "storage/datasets/x/m.jsonl", "records": 3})
    assert len(issues) == 1
    assert "{sliced}" in issues[0]

    # A token bound in `params` is a declared root input, not an intermediate product.
    assert _intermediate_token_issues(
        planned,
        {"manifest": "storage/datasets/x/m.jsonl", "sliced": "out/s.jsonl",
         "records": 3},
    ) == []


def test_a_token_product_is_pinned_beside_its_input():
    """A surviving `{token}` product still lands next to the input it derives from.

    This is the fallback for a token product that passes the intermediate-product check
    (one bound in `params`). Without it the manifest lands in the run directory, away
    from the images it names.
    """
    step = Step(seq=0, trace_id="t", kind=KIND_SHELL, tool_name="terminal",
                command="python3 scripts/slice.py {manifest} {sliced}",
                command_shape="python3 <script:slice> <path> <path>",
                reads=(), writes=(), read_paths=("storage/datasets/x/m.jsonl",),
                write_paths=("{sliced}",), success=True, rejected=False, raw_ref="e0")

    class _Stub:
        scripts: dict = {}
        bindings: dict = {}

        def file_of(self, path):
            return paths.display(canonical_path(path, self.bindings))

        def _input_for(self, path, order):
            return ("manifest", "path", "root.input.manifest")

        _pin_token_artifact = _Plan._pin_token_artifact

    assert _Stub()._pin_token_artifact(step, 0, "{sliced}") == (
        "{input.manifest.dir}/sliced.jsonl"
    )


def test_validation_params_override_instead_of_replacing():
    """A probe that shrinks `records` must not drop the other root inputs.

    Replacing the bound set with `validation_params` made replay report
    "needs ['manifest', 'records'], received ['records']" while the agent's own
    `params` looked complete.
    """
    plan = SimpleNamespace(bound=(("manifest", "storage/x/m.jsonl"), ("records", 10)))

    assert _replay_params(plan, {"records": 3}) == {
        "manifest": "storage/x/m.jsonl", "records": 3}
    # A misspelled validation override must not silently replay the original input.
    with pytest.raises(ValueError, match="validation_params.*unknown"):
        _replay_params(plan, {"unknown": 1})
    assert _replay_params(plan, None) == {
        "manifest": "storage/x/m.jsonl", "records": 10}


def test_unknown_validation_parameter_blocks_plan_before_confirmation(
        service, host, pipeline_workspace):
    plan = prepare(service, distill_args(
        validation_params={"data_file": "data/raw2.csv"}), host=host)
    assert plan["review"]["validation"]["ready"] is False
    assert any("validation_params" in issue and "data_file" in issue
               for issue in plan["blocking_issues"])
    with pytest.raises(ValueError, match="blocking issues"):
        confirm(service, plan, host)


def test_a_template_path_is_rejected_with_a_way_out():
    """A placeholder inside a path is not a parameter; say so instead of inventing one.

    `prelabel_manifest_{records}.jsonl` was read as a literal file name, so the key
    extractor named a parameter `prelabel_manifest_records`; a second such path claimed
    the same base and got `_2`; the params check then demanded keys nobody declared.
    The error has to name the real rule and give both ways out.
    """
    steps = [
        {"run": "head -n {records} {manifest} > out/prelabel_{records}.jsonl"},
        {"run": "python3 scripts/to_sft.py out/prelabel_{records}.jsonl > out/sft.jsonl"},
    ]
    issues = _template_path_issues(plan_steps(steps))
    assert len(issues) == 1
    assert "{records}" in issues[0]
    assert "{output_path}" in issues[0]
    assert "整个路径是一个参数" in issues[0]

    # A whole-path placeholder is the supported spelling and stays quiet.
    assert _template_path_issues(plan_steps([
        {"run": "head -n {records} {manifest} > {slice_path}"},
    ])) == []


def test_plan_review_reports_template_paths_as_one_rule(pipeline_workspace):
    """The review must find the rule at plan time, not emit one issue per path."""
    draft = draft_distill(
        label="模板路径计划",
        steps=[
            {
                "run": (
                    "python3 scripts/prelabel.py {manifest} rules.json "
                    "> out/prelabel_{records}.jsonl"
                ),
            },
            {
                "run": (
                    "python3 scripts/clean.py out/prelabel_{records}.jsonl "
                    "> out/clean_{records}.jsonl"
                ),
            },
            {
                "run": (
                    "python3 scripts/train.py out/clean_{records}.jsonl "
                    "--output out/model_{records}.json"
                ),
            },
        ],
        params={"manifest": "data/raw.csv", "records": 10},
        workspace=pipeline_workspace.root,
    )

    review = draft.review({"will_start_replay": True})
    issues = [
        issue for issue in review["blocking_issues"]
        if "把占位符嵌在路径中间" in issue
    ]

    assert len(issues) == 1
    assert "第 1、2、3 步" in issues[0]
    assert "3 个路径" in issues[0]
    assert review["validation"]["ready"] is False


def test_a_revision_inherits_the_trace_that_verified_it(
        service, host, pipeline_workspace, trace_refs):
    """修订只提交变化的部分，但来源轨迹不是可变参数：丢了它就等于没校验。"""
    refs = trace_refs({"trace_id": "s1"})
    first = prepare(service, distill_args(trace_refs=refs, source_traces=["s1"],
                                          policy="adaptive-first"), "call-1", host)
    assert first["review"]["source_traces"] == ["s1"]

    revised = prepare(
        service,
        distill_args(plan_id=first["plan_id"], digest=first["digest"],
                     policy="adaptive-first"),
        "call-2", host,
    )
    assert revised["review"]["source_traces"] == ["s1"]
    stored = service.distill_plans.get("conv-1", revised["plan_id"])["arguments"]
    assert stored["source_traces"] == ["s1"]


def test_adaptive_policy_needs_a_trace_unless_it_is_explicitly_accepted(
        service, host, pipeline_workspace):
    """adaptive-first 免除逐条 from 核对，导出的轨迹就是唯一的完整性证据。"""
    with pytest.raises(ValueError, match="adaptive-first"):
        prepare(service, distill_args(policy="adaptive-first"), "call-1", host)

    accepted = prepare(service, distill_args(policy="adaptive-first",
                                             allow_unverified=True), "call-2", host)
    assert accepted["review"]["source_traces"] == []


@pytest.mark.parametrize("policy", ["scripts-first", "adaptive-first"])
def test_report_never_claims_source_checks_without_exported_traces(service, host, pipeline_workspace, policy):
    plan = distill(service, label="unverified", steps=CHAIN, workspace=host,
                   final_artifact=FINAL, policy=policy)
    report = plan.report_path.read_text()
    assert "链路完整性：未校验" in report
    assert "按每步 from 核对" not in report
    assert plan.payload()["chain"]["checked"] is False


def test_plan_steps_append_a_truncation_hint_when_many_fields_go_missing():
    with pytest.raises(DistillError) as error:
        plan_steps([{"from": "t:1"}, {"from": "t:2"}])
    message = str(error.value)
    assert "沉淀校验发现 2 个问题" in message
    assert "仅凭缺失项不能判断载荷是否被截断" in message
    assert "当前 digest 和完整修正参数" in message
    assert "不支持分批合并" in message


def test_plan_steps_truncation_hint_stays_quiet_for_a_single_missing_problem():
    with pytest.raises(DistillError) as error:
        plan_steps([{"from": "t:1"}])
    assert "仅凭缺失项" not in str(error.value)


@pytest.mark.parametrize("use_binding", [False, True])
def test_preparation_warns_about_packaged_helpers_and_bound_scripts(service, host, pipeline_workspace, use_binding):
    pipeline_workspace.write("scripts/helpers.py", "from pathlib import Path\ndef labels(p): return Path(p).with_name('labels.csv')\n")
    pipeline_workspace.write("scripts/sibling.py", "import helpers, sys\nprint(helpers.labels(sys.argv[1]))\n")
    command = "python3 {script} {raw} > reports/result.txt" if use_binding else "python3 scripts/sibling.py {raw} > reports/result.txt"
    params = {"raw": "data/raw.csv", **({"script": "scripts/sibling.py"} if use_binding else {})}
    plan = prepare(service, {"label": "sibling warning", "steps": [{"run": command}],
                             "final_artifact": "reports/result.txt", "params": params}, host=host)
    warnings = plan["review"]["warnings"]
    assert any("helpers.py" in warning and "疑似" in warning for warning in warnings)
    assert "sibling_name_guesses" in plan["review"]["planning_preflight"]["checked"]
    assert not plan["review"]["blocking_issues"]
    assert plan["review"]["validation"]["ready"]
