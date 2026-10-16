import json

import pytest
import yaml

from agentgenome.capture.pipeline import CaptureError, next_version, prepare_capture


def capture(service, host, workspace, traces, **kwargs):
    return prepare_capture(service, traces=traces, label="预打标到评测",
                           workspace=workspace.root, scope="conv-1", **kwargs)


def execute(service, host, plan):
    return service.execute(plan.scope, plan.run_id, host, lambda event: None)


def new_data(workspace, review="out/review2.jsonl"):
    return workspace.params(raw="data/raw2.csv", review=review)


def test_capture_publishes_after_a_successful_replay(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(service, host, pipeline_workspace, traces,
                   new_params=new_data(pipeline_workspace))
    assert plan.run_id
    assert plan.reused is False
    assert plan.generalized is None

    state = execute(service, host, plan)
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert state["result"]["capture"]["generalized"] is True
    assert "### 回放验证与发布" in state["result"]["capture"]["report_text"]
    assert service.get(plan.asset_id, plan.version)["published"] is True
    assert [(item["id"], item["version"]) for item in service.list()] == [
        (plan.asset_id, plan.version)]

    report = plan.report_path.read_text(encoding="utf-8")
    for section in ("### 轨迹对齐", "### 反向切片", "### 生成", "### 回放计划",
                    "### 回放验证与发布"):
        assert section in report
    assert "泛化：通过" in report
    assert "已发布" in report
    assert "回放 B（换参数）：succeeded" in report

    status = service.captures.status(plan.capture_id)
    assert status["stage"] == "published"
    assert status["generalized"] is True
    assert status["run"]["status"] == "succeeded"
    assert status["run_id"] == plan.run_id
    assert "已发布" in status["report_text"]


def test_capture_is_idempotent_for_the_same_traces(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    first = capture(service, host, pipeline_workspace, traces)
    execute(service, host, first)

    second = capture(service, host, pipeline_workspace, traces)
    assert second.reused is True
    assert second.run_id is None
    assert (second.asset_id, second.version, second.pin) == (
        first.asset_id, first.version, first.pin)
    assert len(service.versions(first.asset_id)) == 1
    reused = service.captures.status(second.capture_id)
    assert reused["stage"] == "reused"
    assert reused["published"] is True
    assert "未产生新版本" in second.report_path.read_text(encoding="utf-8")


def test_capture_only_freezes_the_package_and_queues_the_replay(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(service, host, pipeline_workspace, traces,
                   new_params=new_data(pipeline_workspace))
    # Neither replay may run inside the capture call: the host only prepares a
    # run directory when a run executes, and that happens after this returns.
    assert not (service.root / "runs").exists()
    assert plan.report_path.read_text(encoding="utf-8").count("### 回放验证与发布") == 0


def test_capture_pairs_a_placeholder_write_with_a_literal_read(
        service, host, pipeline_workspace, tool_entry, trace_factory):
    """One file, two spellings: the slice must not read it as a dropped step."""
    trace = trace_factory([
        tool_entry("s1", "python3 scripts/prelabel.py data/raw.csv rules.json "
                         "> {filtered_jsonl}"),
        tool_entry("s2", "python3 scripts/clean.py out/prelabeled.jsonl > out/clean.csv"),
    ])
    plan = capture(service, host, pipeline_workspace, [trace],
                   final_artifact="out/clean.csv",
                   bindings={"filtered_jsonl": "out/prelabeled.jsonl"},
                   replay_params={"raw": "data/raw.csv", "rules": "rules.json"},
                   new_params={"raw": "data/raw2.csv", "rules": "rules.json"})
    manifest = json.loads((plan.package_dir / "manifest.json").read_text())
    assert list(manifest["parameters"]) == ["raw", "rules"]
    graph = yaml.safe_load((plan.package_dir / "graph.yaml").read_text())
    assert [node["id"] for node in graph["node"]["children"]] == ["prelabel", "clean"]
    state = execute(service, host, plan)
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert state["result"]["capture"]["generalized"] is True


def test_capture_carries_applicability_and_contracts(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(
        service, host, pipeline_workspace, traces,
        declarations={
            "applies_when": ["原始 CSV 预打标后训练与评测"],
            "not_applies_when": ["输入已是 messages-schema JSONL"],
            "input_contract": {"raw": {"format": "csv", "required_fields": ["text"]}},
            "output_contract": {"metrics": {"format": "json", "required_fields": ["rows"]}},
        })
    assert execute(service, host, plan)["status"] == "succeeded"

    asset = service.get(plan.asset_id, plan.version)
    assert asset["applies_when"] == ["原始 CSV 预打标后训练与评测"]
    assert asset["not_applies_when"] == ["输入已是 messages-schema JSONL"]
    assert asset["input_contract"]["raw"]["required_fields"] == ["text"]
    assert asset["output_contract"]["metrics"]["format"] == "json"
    assert asset["determinism"] == "script"
    assert "references" not in asset
    assert "gaps" not in asset
    listed = next(item for item in service.list() if item["id"] == plan.asset_id)
    assert listed["applies_when"] == asset["applies_when"]
    assert listed["input_contract"] == asset["input_contract"]
    assert listed["determinism"] == "script"
    assert "has_references" not in listed
    assert "has_gaps" not in listed


def test_capture_refuses_to_drop_unsolidifiable_steps(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """Inline code has no fixed invocation, so the chain is refused, not half-frozen."""
    trace = trace_factory([
        tool_entry("s1", "python3 -c 'print(1)' > out/prelabeled.jsonl"),
        tool_entry("s2", "python3 scripts/clean.py out/prelabeled.jsonl > out/clean.csv"),
    ])
    with pytest.raises(CaptureError, match="无法固化的步骤"):
        capture(service, host, pipeline_workspace, [trace],
                final_artifact="out/clean.csv")




def test_capture_keeps_an_llm_step_as_a_graph_node(
        service, host, pipeline_workspace, trace_factory, tool_entry):
    """A fixed script call stays in the graph even when it is an LLM step."""
    pipeline_workspace.write("out/review3.jsonl", '{"text": "p-three"}\n')
    trace = trace_factory([
        tool_entry("s1", "python3 scripts/clean.py out/review3.jsonl > out/clean3.csv"),
    ])
    plan = capture(service, host, pipeline_workspace, [trace],
                   final_artifact="out/clean3.csv",
                   new_params={"reviewed": "out/review.jsonl"})
    manifest = json.loads((plan.package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [node for node in manifest["capture"]["skeleton"]] == [0]
    assert "gaps" not in manifest
    assert "scripts/clean.py" in manifest["revision"]["editable_scripts"]
    graph = (plan.package_dir / "graph.yaml").read_text(encoding="utf-8")
    assert "scripts/clean.py" in graph


def test_replaying_the_recorded_parameters_is_not_generalization_evidence(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(service, host, pipeline_workspace, traces,
                   new_params=pipeline_workspace.params())
    assert plan.generalized is False
    assert "换参数回放与原参数相同" in " ".join(plan.notes)

    state = execute(service, host, plan)
    assert state["result"]["capture"]["stage"] == "published"
    assert state["result"]["capture"]["generalized"] is False
    assert "未提供换参数回放数据" in plan.report_path.read_text(encoding="utf-8")


def test_probe_drops_params_that_scripts_materialized_away(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    probe = {**new_data(pipeline_workspace), "review_script": "scripts/clean.py"}
    plan = capture(service, host, pipeline_workspace, traces, new_params=probe)
    assert plan.generalized is None
    assert "review_script" not in service.captures.status(plan.capture_id)["probe"]
    state = execute(service, host, plan)
    assert state["result"]["capture"]["stage"] == "published"
    assert state["result"]["capture"]["generalized"] is True


def test_failed_probe_publishes_a_narrow_experience(
        service, host, pipeline_workspace, pipeline_trace):
    pipeline_workspace.write("out/review3.jsonl", '{"text": "p-three"}\n')
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(service, host, pipeline_workspace, traces,
                   new_params=new_data(pipeline_workspace, review="out/review3.jsonl"))
    assert plan.generalized is None

    state = execute(service, host, plan)
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert service.captures.status(plan.capture_id)["generalized"] is False
    report = plan.report_path.read_text(encoding="utf-8")
    assert "换参数回放未通过" in report
    assert "回放 B（换参数）：failed" in report
    assert ".clean" in report


def test_failed_replay_keeps_a_draft(service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1", reviewed="out/missing.jsonl"),
              pipeline_trace("s2", prefix="b", reviewed="out/missing.jsonl")]
    plan = capture(service, host, pipeline_workspace, traces)
    assert plan.generalized is False

    state = execute(service, host, plan)
    assert state["status"] == "failed"
    assert state["result"]["capture"]["stage"] == "draft"
    assert state["result"]["capture"]["generalized"] is False
    assert service.get(plan.asset_id, plan.version)["published"] is False
    assert service.list() == []
    report = plan.report_path.read_text(encoding="utf-8")
    assert "未发布，保持草稿" in report
    assert "停在节点" in report


def test_changed_scripts_publish_a_new_version(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    first = capture(service, host, pipeline_workspace, traces)
    execute(service, host, first)

    script = pipeline_workspace.root / "scripts" / "train.py"
    script.write_text(script.read_text(encoding="utf-8") + "# retuned\n", encoding="utf-8")

    second = capture(service, host, pipeline_workspace, traces)
    assert second.reused is False
    assert second.version == "0.1.1"
    assert execute(service, host, second)["status"] == "succeeded"
    assert [item["version"] for item in service.versions(first.asset_id)] == ["0.1.0", "0.1.1"]
    assert [(item["id"], item["version"]) for item in service.list()] == [
        (first.asset_id, "0.1.1")]


def test_next_version_bumps_the_patch():
    assert next_version(["0.1.0", "0.1.3", "0.2.0"]) == "0.2.1"
    with pytest.raises(CaptureError, match="无法自动递增版本"):
        next_version(["latest"])


def test_restart_turns_a_queued_capture_into_a_draft(
        service, host, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(service, host, pipeline_workspace, traces)
    assert plan.run_id
    assert service.captures.status(plan.capture_id)["stage"] == "replaying"

    service.recover()

    status = service.captures.status(plan.capture_id)
    assert status["stage"] == "draft"
    assert status["run_status"] == "interrupted"
    assert status["published"] is False
    assert status["generalized"] is False
    assert "回放 A（原参数）：interrupted" in plan.report_path.read_text(encoding="utf-8")
