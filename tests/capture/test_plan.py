import pytest

from agentgenome.capture.plan import _key_base, build_candidate_plan


def test_a_root_input_is_named_by_role_not_by_the_trial_size():
    """`head -10` 的产物只是那次试跑的切片，参数名不该把条数固化进模板。"""
    assert _key_base(
        "storage/datasets/test_100/test_100/prelabel_manifest_10.jsonl"
    ) == "prelabel_manifest"
    assert _key_base(
        "storage/datasets/test_100/test_100/prelabel_10_review.jsonl"
    ) == "prelabel_review"
    assert _key_base("data/raw.csv") == "raw"


def test_candidate_plan_extracts_the_artifact_connected_chain(pipeline_trace):
    trace = pipeline_trace("session-1")

    candidate = build_candidate_plan(traces=[trace])

    assert [step.get("run") or step.get("tool") or step.get("human")
            for step in candidate.steps] == [
        "python3 scripts/prelabel.py {raw} {rules} > out/prelabeled.jsonl",
        "我去 Label Studio 复核完了，导出的结果在 out/review.jsonl，继续清洗\n"
        "路径：`out/prelabeled.jsonl`",
        "python3 scripts/clean.py out/review.jsonl > out/clean.csv",
        "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt",
        "python3 scripts/evaluate.py models/model.pt out/clean.csv > reports/metrics.json",
    ]
    assert candidate.final_artifact == "reports/metrics.json"
    assert candidate.params == {"raw": "data/raw.csv", "rules": "rules.json"}
    assert candidate.input_contract == {
        "raw": {"type": "path"},
        "rules": {"type": "path"},
    }


def test_a_cd_target_is_context_not_a_root_input(
    trace_factory, tool_entry
):
    """`cd <workspace>` 是执行上下文，不是数据。

    解析器会把 cd 的目标当成读，于是工作区根目录变成根输入参数——一个谁都
    供不上的目录。它不该进契约。
    """
    workspace = "/target-workspace/.pyromind-agent/abc"
    trace = trace_factory([
        tool_entry(
            "a1",
            f"cd {workspace}; python3 scripts/clean.py data/raw.csv > out/clean.csv",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert candidate.params == {"raw": "data/raw.csv"}
    assert workspace not in candidate.params.values()


def test_candidate_plan_keeps_a_platform_tool_before_its_wait_step(
    trace_factory, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/prelabel.py data/raw.csv rules.json > out/prelabeled.jsonl",
        ),
        tool_entry(
            "a2",
            "",
            tool="label_studio_list_projects",
            args={},
        ),
        tool_entry(
            "a3",
            "",
            tool="df_submit_pipeline",
            args={
                "input_path": "out/prelabeled.jsonl",
                "script_path": "public_data/pipeline.py",
            },
        ),
        tool_entry(
            "a4",
            "python3 wait_pipeline.py out/prelabeled.jsonl > reports/metrics.json",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert [step.get("run") or step.get("tool") for step in candidate.steps] == [
        "python3 scripts/prelabel.py {raw} {rules} > out/prelabeled.jsonl",
        "df_submit_pipeline",
        "python3 wait_pipeline.py out/prelabeled.jsonl > reports/metrics.json",
    ]
    tool = candidate.steps[1]
    assert tool["keep"] is True
    assert tool["output"].startswith("public_data/agentgenome-artifacts/")
    assert tool["input"] == ["out/prelabeled.jsonl", "public_data/pipeline.py"]
    assert candidate.generated_receipts == (tool["output"],)
    assert any("回执路径" in item for item in candidate.ambiguities)


def test_candidate_plan_excludes_an_unreferenced_trial_branch(
    trace_factory, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/clean.py data/raw.csv > out/trial.csv",
        ),
        tool_entry(
            "a2",
            "python3 scripts/clean.py data/raw.csv > out/final.csv",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert [step["run"] for step in candidate.steps] == [
        "python3 scripts/clean.py {raw} > out/final.csv"
    ]
    assert candidate.final_artifact == "out/final.csv"
    assert any("out/trial.csv" in item for item in candidate.branches)


def test_candidate_plan_keeps_an_initial_path_as_a_root_input(
    trace_factory, user_entry, tool_entry
):
    trace = trace_factory([
        user_entry("u1", "请处理 data/raw.csv"),
        tool_entry(
            "a1",
            "python3 scripts/clean.py data/raw.csv > out/clean.csv",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert list(candidate.steps) == [
        {
            "run": "python3 scripts/clean.py {raw} > out/clean.csv",
            "from": "session-1:1",
        }
    ]
    assert candidate.params == {"raw": "data/raw.csv"}


def test_candidate_plan_ignores_a_write_remove_probe(
    trace_factory, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/filter.py data/raw.csv > out/clean.csv",
        ),
        tool_entry(
            "a2",
            "python3 scripts/generate.py out/clean.csv > out/answers.jsonl",
        ),
        tool_entry(
            "a3",
            "cd out && test -w storage/ && echo test > storage/_wtest "
            "&& rm storage/_wtest",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert candidate.final_artifact == "out/answers.jsonl"
    assert [step["run"] for step in candidate.steps] == [
        "python3 scripts/filter.py {raw} > out/clean.csv",
        "python3 scripts/generate.py out/clean.csv > out/answers.jsonl",
    ]
    assert any("临时/试跑产物" in item for item in candidate.branches)


def test_candidate_plan_keeps_a_side_effect_tool_without_a_consumer(
    trace_factory, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/prelabel.py data/raw.csv > out/prelabeled.jsonl",
        ),
        tool_entry(
            "a2",
            "",
            tool="label_studio_import_project",
            args={"source": "out/prelabeled.jsonl"},
        ),
        tool_entry(
            "a3",
            "python3 scripts/clean.py out/prelabeled.jsonl > out/clean.csv",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert [step.get("run") or step.get("tool") for step in candidate.steps] == [
        "python3 scripts/prelabel.py {raw} > out/prelabeled.jsonl",
        "label_studio_import_project",
        "python3 scripts/clean.py out/prelabeled.jsonl > out/clean.csv",
    ]
    assert candidate.steps[1]["keep"] is True
    assert candidate.generated_receipts == (candidate.steps[1]["output"],)
    assert any("修改外部状态" in item for item in candidate.ambiguities)


def test_candidate_plan_closes_a_chain_ending_at_a_human_artifact(
    trace_factory, user_entry, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/prelabel.py data/raw.csv > out/prelabeled.jsonl",
        ),
        user_entry(
            "u2",
            "我在 Label Studio 复核完了，导出的结果在 out/review.jsonl",
        ),
    ])

    candidate = build_candidate_plan(
        traces=[trace],
        final_artifact="out/review.jsonl",
    )

    assert [step.get("run") or step.get("human") for step in candidate.steps] == [
        "python3 scripts/prelabel.py {raw} > out/prelabeled.jsonl",
        "我在 Label Studio 复核完了，导出的结果在 out/review.jsonl\n"
        "路径：`out/prelabeled.jsonl`",
    ]


def test_candidate_plan_falls_back_when_final_artifact_is_a_description(
    pipeline_trace,
):
    description = (
        "SFT 训练 JSONL(storage/datasets/sft.jsonl)，每行包含 "
        "{id,task,images}，图片按 images/<id>/ 相对路径复制。"
    )

    candidate = build_candidate_plan(
        traces=[pipeline_trace("session-1")],
        final_artifact=description,
    )

    assert candidate.final_artifact == "reports/metrics.json"
    assert len(candidate.steps) == 5
    assert candidate.arguments({})["final_artifact"] == "reports/metrics.json"
    assert any("不是单个产物路径" in item for item in candidate.ambiguities)


def test_candidate_plan_recovers_a_produced_path_from_a_description(
    pipeline_trace,
):
    description = (
        "最终输出 reports/metrics.json，包含 rows 和 epochs；"
        "这是一个结构化评测结果。"
    )

    candidate = build_candidate_plan(
        traces=[pipeline_trace("session-1")],
        final_artifact=description,
    )

    assert candidate.final_artifact == "reports/metrics.json"
    assert len(candidate.steps) == 5
    assert any("描述中唯一由 trace 产出的路径" in item for item in candidate.ambiguities)


def test_candidate_plan_excludes_genome_management_calls(
    trace_factory, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/clean.py data/raw.csv > out/clean.csv",
        ),
        tool_entry(
            "a2",
            "",
            tool="genome_distill",
            args={
                "source_traces": ["session-1"],
                "final_artifact": "out/clean.csv",
            },
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert [step["run"] for step in candidate.steps] == [
        "python3 scripts/clean.py {raw} > out/clean.csv"
    ]
    assert any("AgentGenome 管理调用" in item for item in candidate.branches)


def test_candidate_plan_exposes_a_product_written_inside_a_script(
    trace_factory, tool_entry
):
    trace = trace_factory([
        tool_entry(
            "a1",
            "python3 scripts/ls_to_sft.py",
            text="wrote 3 rows -> storage/datasets/review/sft.jsonl",
        ),
    ])

    candidate = build_candidate_plan(traces=[trace])

    assert candidate.final_artifact == "storage/datasets/review/sft.jsonl"
    assert candidate.steps == (
        {
            "run": "python3 scripts/ls_to_sft.py",
            "from": "session-1:0",
        },
    )
    assert any("命令没有显式接收输出路径" in item for item in candidate.ambiguities)


def test_candidate_plan_requires_a_trace():
    with pytest.raises(ValueError, match="at least one trace"):
        build_candidate_plan(traces=[])
