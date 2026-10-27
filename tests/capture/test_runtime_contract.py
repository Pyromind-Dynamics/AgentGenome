import pytest

from agentgenome.capture.distill import DistillError, distill, draft_distill
from agentgenome.capture.replay import probe_replay


def test_verification_only_script_is_packaged_remapped_and_executed(
        service, host, pipeline_workspace, tmp_path):
    pipeline_workspace.write("checks/clean.py", "import csv,sys\nassert list(csv.DictReader(open(sys.argv[1])))\n")
    plan = distill(service, label="verification helper", workspace=host,
                   steps=[{"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv",
                           "verify": "python3 checks/clean.py out/clean.csv"}],
                   final_artifact="out/clean.csv")
    copies = dict(plan.package.scripts)
    assert copies["checks/clean.py"] != copies["scripts/clean.py"]
    assert copies["checks/clean.py"] in plan.package.nodes[0].verify
    result = probe_replay(host, plan.package, dict(plan.bound),
                          run_id="helper", run_dir=tmp_path / "replay")
    assert result.passed, result.failure


def test_missing_verification_only_script_blocks_preflight(service, host):
    with pytest.raises(DistillError, match="脚本不存在.*missing.py"):
        distill(service, label="missing verifier", workspace=host,
                steps=[{"run": "python3 scripts/clean.py out/review.jsonl > out/clean.csv",
                        "verify": "python3 checks/missing.py out/clean.csv"}],
                final_artifact="out/clean.csv")


def test_review_distinguishes_executable_steps_from_a_pruned_final_check():
    draft = draft_distill(label="sliced check", steps=[
        {"run": "python3 scripts/clean.py data/raw.csv > out/clean.csv"},
        {"run": "python3 scripts/check.py out/clean.csv > out/acceptance.json"},
    ], final_artifact="out/clean.csv")
    review = draft.review({"will_start_replay": True})
    assert [s["step"] for s in review["effective_steps"]] == [1]
    assert [s["step"] for s in review["excluded_steps"]] == [2]
    assert any("不会执行" in warning for warning in review["warnings"])


def test_status_prose_is_not_a_reported_product(trace_factory, tool_entry):
    trace = trace_factory([tool_entry(
        "check", "python3 scripts/check.py data/raw.csv",
        text="交付目录/中间产物/复核输入：无未脱敏 PII")])
    assert trace.steps[0].reported_writes == ()


def test_python_import_helpers_are_frozen_transitively_and_run_without_workspace(
        service, host, pipeline_workspace, tmp_path):
    import subprocess
    import sys

    pipeline_workspace.write("scripts/check.py", "from rules_io import check\ncheck()\n")
    pipeline_workspace.write("scripts/rules_io.py", "import constants\ndef check():\n    assert constants.VALUE == 42\n")
    pipeline_workspace.write("scripts/constants.py", "VALUE = 42\n")
    plan = distill(service, label="imported helpers", workspace=host, steps=[{
        "run": "python3 scripts/check.py data/raw.csv > out/result.txt",
    }], final_artifact="out/result.txt")
    assert {"scripts/rules_io.py", "scripts/constants.py"} <= dict(plan.package.scripts).keys()
    result = subprocess.run([sys.executable, "-E", "scripts/check.py"],
                            cwd=plan.package.directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_python_syntax_error_blocks_preflight(service, host, pipeline_workspace):
    pipeline_workspace.write("scripts/broken.py", "def broken(:\n")
    with pytest.raises(DistillError, match="broken.py.*SyntaxError"):
        distill(service, label="syntax check", workspace=host, steps=[{
            "run": "python3 scripts/broken.py data/raw.csv > out/result.txt",
        }], final_artifact="out/result.txt")


def test_opaque_task_returns_normalization_evidence_instead_of_a_dead_end(
        service, host, pipeline_workspace, user_entry, tool_entry):
    import json
    from agentgenome.dispatch import invoke

    document = {"trace_version": 1, "trace_id": "ordinary-task", "steps": [
        user_entry("u1", "Process this batch"),
        tool_entry("w1", "", tool="write", args={
            "path": "scripts/opaque.py", "content": "print('done')\n",
        }),
        tool_entry("r1", "python3 scripts/opaque.py", text="done"),
    ]}
    pipeline_workspace.write("trace.json", json.dumps(document))
    result = invoke(service, "normalization", "distill", {
        "label": "ordinary task", "source_traces": ["ordinary-task"],
        "trace_refs": [{"path": "trace.json"}],
    }, "extract", host=host)
    assert result["status"] == "needs_normalization"
    assert result["planning"]["mode"] == "mechanical_candidate"
    assert "scripts/opaque.py" in result["normalization"]["scripts"]
    assert any(s["source"] == "ordinary-task:2" for s in result["normalization"]["observed_steps"])
    assert result["requires_confirmation"] is False
    assert service.distill_plans.previews("normalization") == []


@pytest.mark.parametrize("keeps_audit,comparison", [(False, True), (True, True), (False, False)])
def test_cleanup_comparison_is_a_branch_only_when_all_outputs_are_removed(
        trace_factory, tool_entry, keeps_audit, comparison):
    from agentgenome.capture.distill import plan_steps, _scope_split

    final = "python3 scripts/clean.py data/raw.csv --output out/final.csv"
    trial = "python3 scripts/clean.py data/raw.csv --output scratch/result.csv"
    if not comparison:
        trial += " && python3 scripts/followup.py out/final.csv --output scratch/next.csv"
    elif keeps_audit:
        trial += " && diff scratch/result.csv out/final.csv > out/audit.txt"
    else:
        trial += " && diff scratch/result.csv out/final.csv"
    trial += " && rm -rf scratch"
    trace = trace_factory([tool_entry("final", final), tool_entry("trial", trial)])
    connected, branches = _scope_split(plan_steps([{"run": final}]), [trace], None)
    assert bool(connected) is (keeps_audit or not comparison)
    assert bool(branches) is (comparison and not keeps_audit)


def test_unmapped_secondary_output_token_blocks_package(service, host, pipeline_workspace):
    pipeline_workspace.write("scripts/export.py", "print('export')\n")
    with pytest.raises(DistillError, match="副产物.*side"):
        distill(service, label="secondary output", workspace=host,
                steps=[{"run": "python3 scripts/export.py data/raw.csv --output {main} --summary {side}",
                        "output": ["{main}", "{side}"]}],
                params={"main": "out/main.json", "side": "out/side.json"}, final_artifact="{main}")
