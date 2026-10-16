import json
from pathlib import Path

import pytest

from agentgenome.capture.distill import DistillError, distill
from agentgenome.dispatch import invoke
from agentgenome.local_host import LocalExecutionHost
from core import Answer

FINAL = "reports/metrics.json"
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
    return {"label": "预打标到评测", "asset_id": "prelabel-train-eval",
            "steps": CHAIN, "final_artifact": FINAL, **overrides}


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


def test_distill_queues_the_same_replay_capture_would(
        service, host, pipeline_workspace):
    plan = invoke(service, "conv-1", "distill", distill_args(), "call-1", host=host)
    assert plan["asset_id"] == "prelabel-train-eval"
    assert plan["reused"] is False
    assert plan["run_id"]
    assert plan["distilled"]["trace_id"].startswith("distilled-")
    # 人工复核是链路的边界：预打标的产出由人消费，不在最终产物的依赖闭包里。
    assert [node["id"] for node in plan["distilled"]["nodes"]] == [
        "clean", "train", "evaluate"]
    assert [item["step"] for item in plan["distilled"]["auxiliary"]] == [1]
    assert "trace" not in plan["distilled"]

    state = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert service.get(plan["asset_id"], plan["version"])["published"] is True


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
                   "steps": HUMAN_CHAIN, "final_artifact": "out/clean.csv"},
                  "call-1", host=host)
    nodes = plan["distilled"]["nodes"]
    assert [node["kind"] for node in nodes] == ["run", "human", "run"]
    graph = (Path(plan["package"]) / "graph.yaml").read_text(encoding="utf-8")
    assert "human:" in graph
    assert "请到 Label Studio 复核" in graph

    parked = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert parked["status"] == "waiting"
    assert parked["result"]["waiting"]["node"].endswith("review")
    capture = service.captures.status(plan["capture_id"])
    assert capture["stage"] == "waiting"
    assert capture["published"] is False
    assert service.get(plan["asset_id"], plan["version"])["published"] is False
    assert "停在人工节点" in capture["report_text"]

    resumed = service.resume("conv-1", plan["run_id"])
    assert resumed["status"] == "queued"
    finished = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert finished["status"] == "succeeded", finished["result"].get("error")
    assert len(human.questions) == 2
    assert human.artifacts[0].endswith("--review.out"), human.artifacts
    assert Path(human.artifacts[0]).read_text(encoding="utf-8").startswith('{"text": "p-one"')
    assert finished["result"]["waiting"] is None
    cleaned = Path(finished["result"]["outputs"]["clean"]["execution_path"])
    assert "p-one" in cleaned.read_text(encoding="utf-8")
    assert service.captures.status(plan["capture_id"])["stage"] == "published"
    assert service.get(plan["asset_id"], plan["version"])["published"] is True


def test_distill_is_deterministic_so_a_retry_reuses_the_version(
        service, host, pipeline_workspace):
    first = invoke(service, "conv-1", "distill", distill_args(), "call-1", host=host)
    service.execute("conv-1", first["run_id"], host, lambda event: None)

    second = invoke(service, "conv-1", "distill", distill_args(), "call-2", host=host)
    assert second["distilled"]["trace_id"] == first["distilled"]["trace_id"]
    assert second["reused"] is True
    assert second["pin"] == first["pin"]
    assert len(service.versions(first["asset_id"])) == 1


def test_distill_writes_a_report_naming_every_step(service, host, pipeline_workspace):
    plan = invoke(service, "conv-1", "distill", distill_args(), "call-1", host=host)
    body = Path(plan["distilled"]["report"]).read_text(encoding="utf-8")
    assert "# 链路蒸馏报告" in body
    assert "scripts/train.py" in body
    assert "s1:6" in body
    assert "辅助" in body


def test_distill_carries_declarations(service, host, pipeline_workspace):
    plan = invoke(service, "conv-1", "distill", distill_args(
        applies_when=["原始问题 CSV 过滤后生成回答"],
        not_applies_when=["输入已是 messages-schema JSONL"],
        input_contract={"data_file": {"format": "csv", "required_fields": ["qid"]}},
    ), "call-1", host=host)
    package = Path(plan["distilled"]["package"])
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["determinism"] == "script"
    assert "references" not in manifest
    assert "gaps" not in manifest
    assert manifest["applies_when"] == ["原始问题 CSV 过滤后生成回答"]
    assert manifest["not_applies_when"] == ["输入已是 messages-schema JSONL"]
    assert manifest["input_contract"]["data_file"]["required_fields"] == ["qid"]

    state = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    listed = next(item for item in service.list() if item["id"] == plan["asset_id"])
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


def test_distill_rejects_a_final_artifact_no_step_produces(
        service, host, pipeline_workspace):
    with pytest.raises(DistillError, match="没有被任何步骤声明为产出"):
        invoke(service, "conv-1", "distill",
               distill_args(final_artifact="reports/ghost.json"), "call-1", host=host)


def test_distill_reports_a_script_that_is_not_on_disk(
        service, host, pipeline_workspace):
    steps = [{"run": "python3 scripts/ghost.py data/raw.csv > out/ghost.csv"}]
    with pytest.raises(DistillError, match="脚本不存在"):
        invoke(service, "conv-1", "distill",
               distill_args(steps=steps, final_artifact="out/ghost.csv"), "call-1", host=host)


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
    plan = invoke(service, "conv-1", "distill",
                  distill_args(trace_refs=refs, source_traces=["s1"]), "call-1", host=host)
    assert plan["distilled"]["chain"] == {"checked": True, "traces": ["s1"], "boundaries": []}


def test_distill_rejects_a_plan_that_drops_an_upstream_step(
        service, host, pipeline_workspace, trace_refs):
    refs = trace_refs({"trace_id": "s1"})
    steps = CHAIN[2:]
    with pytest.raises(DistillError, match="链路不完整"):
        invoke(service, "conv-1", "distill",
               distill_args(steps=steps, trace_refs=refs), "call-1", host=host)


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
    plan = invoke(service, "conv-1", "distill",
                  distill_args(steps=steps, params={"review_jsonl": "out/review.jsonl"}),
                  "call-1", host=host)
    assert plan["distilled"]["params"] == [
        {"key": "review_jsonl", "source": "{review_jsonl}", "value": "out/review.jsonl"}]
    package = Path(plan["distilled"]["package"])
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["parameters"]) == ["review_jsonl"]
    graph = (package / "graph.yaml").read_text(encoding="utf-8")
    assert "{input.review_jsonl}" in graph

    state = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert service.get(plan["asset_id"], plan["version"])["published"] is True


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
    plan = invoke(service, "conv-1", "distill",
                  {**CONVERTER_ARGS, "trace_refs": converter_session(tool_entry,
                                                                     pipeline_workspace)},
                  "call-1", host=host)
    distilled = plan["distilled"]
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

    state = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert service.get(plan["asset_id"], plan["version"])["published"] is True


def test_distill_keeps_an_unreadable_script_parameter_as_a_boundary(
        service, host, pipeline_workspace, tool_entry):
    plan = invoke(service, "conv-1", "distill",
                  {**CONVERTER_ARGS, "trace_refs": converter_session(tool_entry,
                                                                     pipeline_workspace)},
                  "call-1", host=host)
    distilled = plan["distilled"]
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
    plan = invoke(service, "conv-1", "distill", {
        "label": "运行 CLI", "asset_id": "runner-cli",
        "steps": [{"run": "python3 {runner_script} {review_jsonl} {clean_output}"}],
        "params": {"runner_script": "scripts/runner.py",
                   "review_jsonl": "out/review.jsonl",
                   "clean_output": "out/clean.jsonl"},
        "trace_refs": ["public_data/agentgenome-traces/s1.json"],
    }, "call-1", host=host)
    distilled = plan["distilled"]
    assert [item["package"] for item in distilled["scripts"]] == [
        "scripts/helper.py", "scripts/runner.py"]
    package = Path(distilled["package"])
    assert (package / "scripts/helper.py").read_text(encoding="utf-8") == (
        "print('helper')\n")


def test_distill_requires_params_for_placeholder_inputs(service, host, pipeline_workspace):
    steps = [{"run": "python3 scripts/clean.py {review_jsonl} > out/clean.csv"}]
    with pytest.raises(DistillError, match="占位符"):
        invoke(service, "conv-1", "distill",
               distill_args(steps=steps, final_artifact="out/clean.csv"), "call-1", host=host)


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
    {"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"},
    {"run": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt"},
    {"run": "python3 scripts/evaluate.py models/model.pt out/clean.csv > reports/metrics.json"},
]


def test_distill_promotes_a_declared_tool_call_to_a_tool_node(
        service, host, pipeline_workspace):
    plan = distill(service, label="提交平台预打标", steps=TOOL_CHAIN, workspace=host)
    assert [node.kind for node in plan.package.nodes] == ["tool", "run", "run", "run"]
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


def test_distill_replays_a_tool_node_through_the_host_port(
        service, host, pipeline_workspace):
    plan = invoke(service, "conv-1", "distill", {
        "label": "提交平台预打标", "steps": TOOL_CHAIN,
    }, "call-1", host=host)
    state = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    artifacts = service.root / "runs" / plan["run_id"] / "work" / "artifacts"
    submitted = [json.loads(path.read_text(encoding="utf-8"))
                 for path in sorted(artifacts.glob("*.json"))]
    submitted = [body for body in submitted if body.get("tool") == "df_submit_pipeline"]
    assert len(submitted) == 1
    assert submitted[0]["arguments"]["task_name"] == "pretag"


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
    plan = invoke(service, "conv-1", "distill", {
        "label": "生成回答到评测", "asset_id": "generate-answer-eval",
        "steps": LLM_CHAIN, "final_artifact": "reports/metrics.json",
        "params": {"data_file": "data/raw.csv"},
    }, "call-1", host=host)
    distilled = plan["distilled"]
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

    state = service.execute("conv-1", plan["run_id"], host,
                            stage_driver(service, "conv-1",
                                         body=json.dumps({"text": "p-one", "label": "a"}) + "\n"
                                         + json.dumps({"text": "p-two", "label": "b"}) + "\n"))
    assert state["status"] == "succeeded"
    assert service.get(plan["asset_id"], plan["version"])["published"] is True


def test_distill_rejects_an_llm_prompt_that_drops_its_declared_paths(
        service, host, pipeline_workspace):
    steps = [{"llm": "读输入生成回答", "input": ["{data_file}"],
              "output": "out/answers.jsonl"},
             *LLM_CHAIN[1:]]
    with pytest.raises(DistillError, match="原样出现"):
        invoke(service, "conv-1", "distill", {
            "label": "x", "steps": steps, "final_artifact": "reports/metrics.json",
            "params": {"data_file": "data/raw.csv"}}, "call-1", host=host)


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
    with pytest.raises(DistillError, match="根输入"):
        invoke(service, "conv-1", "distill", distill_args(params={"ghost": "data/raw.csv"}),
               "call-1", host=host)


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
