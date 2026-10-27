import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentgenome.capture.baseline import derive
from agentgenome.dispatch import invoke
from agentgenome.local_host import LocalExecutionHost
from agentgenome.regressions import validate_cases


def digest(char):
    return char * 64


def samples(directory, **entry):
    return {"directory": str(directory), "files": [
        {"key": "raw", "name": "raw.csv", "format": "csv", "truncated": False,
         "rows": 2, **entry}]}


def package(tmp_path, parameters=("raw",)):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(
        {"parameters": {name: {"type": "path"} for name in parameters}}))
    return root


def completed(*outputs):
    return [{"node": "slice", "outputs": {
        key: {"path": f"out/{key}", "sha256": value} for key, value in outputs}}]


def sampled(tmp_path, text="text,label\none,a\n"):
    directory = tmp_path / "samples"
    directory.mkdir()
    (directory / "raw.csv").write_text(text)
    return directory


def test_baseline_freezes_inputs_and_artifact_digests(tmp_path):
    result = derive("capture-abcdef", samples(sampled(tmp_path)), package(tmp_path),
                    completed(("clean", digest("a")), ("metrics", digest("b"))))
    assert result["ready"] is True
    assert result["reason"] is None
    case = result["cases"][0]
    assert case["id"] == "baseline-capture-abcdef"
    assert case["files"] == {"inputs/raw.csv": "text,label\none,a\n"}
    assert case["params"] == {"raw": "fixture:inputs/raw.csv"}
    assert case["assertions"] == [
        {"node": "slice", "output": "clean", "kind": "hash", "expected": digest("a")},
        {"node": "slice", "output": "metrics", "kind": "hash", "expected": digest("b")}]


def test_baseline_caps_the_number_of_assertions(tmp_path):
    outputs = [(f"out{index}", digest("a")) for index in range(20)]
    result = derive("c", samples(sampled(tmp_path)), package(tmp_path),
                    completed(*outputs))
    assert result["ready"] is True
    assert 0 < len(result["cases"][0]["assertions"]) <= 12


@pytest.mark.parametrize("entry", [
    {"truncated": True}, {"format": "binary"}, {"format": "unavailable"}])
def test_baseline_refuses_a_sample_it_cannot_replay(tmp_path, entry):
    result = derive("c", samples(sampled(tmp_path), **entry), package(tmp_path),
                    completed(("clean", digest("a"))))
    assert result["ready"] is False
    assert "不完整" in result["reason"]
    assert result["cases"] == []


def test_baseline_refuses_when_the_params_do_not_match(tmp_path):
    result = derive("c", samples(sampled(tmp_path)), package(tmp_path, ("raw", "review")),
                    completed(("clean", digest("a"))))
    assert result["ready"] is False
    assert result["cases"] == []


def test_baseline_refuses_without_a_completed_output(tmp_path):
    result = derive("c", samples(sampled(tmp_path)), package(tmp_path), [])
    assert result["ready"] is False
    assert "摘要" in result["reason"]


def test_baseline_refuses_without_samples():
    assert derive("c", None, Path("/nowhere"), [])["ready"] is False


def test_validate_cases_accepts_hash_assertions():
    cases = validate_cases([{"id": "case-1", "files": {"raw.csv": "a\n"},
                             "params": {"raw": "fixture:raw.csv"},
                             "assertions": [{"node": "slice", "output": "clean",
                                             "kind": "hash", "expected": digest("a")}]}])
    assert cases[0]["assertions"][0]["kind"] == "hash"


def test_validate_cases_rejects_an_unknown_assertion_kind():
    with pytest.raises(ValueError, match="kind"):
        validate_cases([{"id": "case-1", "files": {"raw.csv": "a\n"},
                         "params": {"raw": "fixture:raw.csv"},
                         "assertions": [{"node": "slice", "output": "clean",
                                         "kind": "size", "expected": 4}]}])


class RevisionHost(LocalExecutionHost):
    def prepare_revision(self, revision_id, package, allowed):
        directory = self.root / "drafts" / revision_id
        for name in allowed:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((package / name).read_bytes())
        return {"editable_dir": str(directory)}

    def collect_revision(self, draft, allowed):
        return {name: (Path(draft["editable_dir"]) / name).read_bytes()
                for name in allowed}

    def prepare(self, *args, verification_package=None, **options):
        ports, params = super().prepare(*args)
        return replace(ports, verification_cwd=verification_package), params


def publish(service, host, trace_refs):
    refs = trace_refs({"trace_id": "s1"}, {"trace_id": "s2", "prefix": "b"})
    plan = invoke(service, "conv-1", "capture",
                  {"trace_refs": refs, "label": "预打标到评测",
                   "asset_id": "prelabel-train-eval"},
                  "capture-1", host=host)
    executed = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert executed["status"] == "succeeded"
    return plan


def test_a_published_capture_offers_its_inputs_to_the_next_revision(
        service, host, pipeline_workspace, trace_refs):
    plan = publish(service, host, trace_refs)
    cases = service.captures.baseline_cases(plan["asset_id"], plan["version"])
    assert cases is not None
    assert [case["id"] for case in cases] == [f"baseline-{plan['capture_id'][:24]}"]
    assert all(assertion["kind"] == "hash" for assertion in cases[0]["assertions"])
    assert cases[0]["params"]["raw"] == "fixture:inputs/raw.csv"
    # The generated baseline is exactly what validate_cases accepts at submit time.
    assert validate_cases(cases) == cases

    revision_host = RevisionHost(service.root, pipeline_workspace.root)
    draft = service.revisions.prepare("conv-1", plan["run_id"], "draft", revision_host,
                                      reason="把预打标脚本换成新规则")
    assert draft["baseline_cases"] == cases
    assert draft["regression_cases"] == []


def test_a_revision_replays_the_captured_baseline_before_publishing(
        service, host, pipeline_workspace, trace_refs):
    plan = publish(service, host, trace_refs)
    cases = service.captures.baseline_cases(plan["asset_id"], plan["version"])
    revision_host = RevisionHost(service.root, pipeline_workspace.root)
    draft = service.revisions.prepare("conv-1", plan["run_id"], "draft", revision_host,
                                      reason="给脚本补一行说明")
    script = sorted(Path(name) for name in draft["allowed_scripts"])[0]
    editable = Path(draft["editable_dir"]) / script
    # A no-op edit moves the package pin without moving any artifact digest.
    editable.write_bytes(editable.read_bytes() + b"\n")
    new_case = {"id": "case-edge", "files": cases[0]["files"],
                "params": cases[0]["params"],
                "assertions": cases[0]["assertions"][:1]}

    child = service.revisions.submit("conv-1", "revision-1", draft["revision_id"],
                                     draft["params"], "只补注释", revision_host,
                                     regression_cases=[new_case])
    result = service.execute("conv-1", child["id"], revision_host, lambda event: None)
    assert result["status"] == "succeeded"
    assert result["result"]["publication"] == {"status": "published", "version": "0.1.1"}
    assert service.get(plan["asset_id"], "0.1.1")["published"] is True


def test_the_captured_baseline_fails_a_package_that_moves_an_artifact(
        service, host, pipeline_workspace, trace_refs):
    plan = publish(service, host, trace_refs)
    cases = service.captures.baseline_cases(plan["asset_id"], plan["version"])
    revision_host = RevisionHost(service.root, pipeline_workspace.root)
    draft = service.revisions.prepare("conv-1", plan["run_id"], "draft", revision_host,
                                      reason="换一个改变产物的脚本")
    script = sorted(Path(name) for name in draft["allowed_scripts"])[0]
    editable = Path(draft["editable_dir"]) / script
    editable.write_bytes(editable.read_bytes() + b"\nprint('drift', flush=True)\n")
    new_case = {"id": "case-edge", "files": cases[0]["files"],
                "params": cases[0]["params"],
                "assertions": cases[0]["assertions"][:1]}

    child = service.revisions.submit("conv-1", "revision-2", draft["revision_id"],
                                     draft["params"], "改变产物", revision_host,
                                     regression_cases=[new_case])
    result = service.execute("conv-1", child["id"], revision_host, lambda event: None)
    assert result["status"] == "failed"
    assert "does not match expected hash" in result["result"]["error"]


def test_a_pending_output_parameter_still_yields_a_baseline(tmp_path):
    """输出目录在采集时不存在是常态：它没有 fixture，但不能因此取消整条基线。"""
    data = {
        "directory": str(sampled(tmp_path)),
        "files": [
            {"key": "raw", "name": "raw.csv", "format": "csv",
             "truncated": False, "rows": 2},
            {"key": "out", "name": None, "format": "pending",
             "truncated": False},
        ],
    }
    result = derive("capture-abcdef", data, package(tmp_path, ("raw", "out")),
                    completed(("clean", digest("a"))))
    assert result["ready"] is True
    assert result["cases"][0]["params"] == {
        "raw": "fixture:inputs/raw.csv", "out": "output:out"}


def test_pending_output_baseline_executes_in_an_isolated_directory(service, host, pipeline_workspace, tmp_path):
    import hashlib
    from threading import Event
    from agentgenome.capture.distill import distill
    from agentgenome.capture.pipeline import _output_params
    from agentgenome.capture.sample import sample_inputs

    pipeline_workspace.write("scripts/produce.py", '''import argparse
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--input')
p.add_argument('--output-dir')
a = p.parse_args()
out = Path(a.output_dir)
assert not out.exists(), 'regression output must be fresh'
out.mkdir(parents=True)
text = Path(a.input).read_text()
(out / 'copy.csv').write_text(text)
print(text, end='')
''')
    plan = distill(service, label="isolated output", workspace=host,
                   steps=[{"run": "python3 scripts/produce.py --input {raw} --output-dir {out} > reports/result.txt"}],
                   params={"raw": "data/raw.csv", "out": "business-output"},
                   final_artifact="reports/result.txt")
    text = (pipeline_workspace.root / "data/raw.csv").read_text()
    optional = _output_params(plan.package)
    assert optional == {"out"}
    sampled_inputs = sample_inputs(host, dict(plan.bound), tmp_path / "samples", optional=optional)
    assert sampled_inputs["pending"] == ["out"]
    node = plan.package.nodes[0]
    result = derive("isolated", sampled_inputs, plan.package.directory, [{"node": f"{plan.package.root_id}.{node.id}",
                                  "outputs": {node.output[0]: {"sha256": hashlib.sha256(text.encode()).hexdigest()}}}])
    assert result["ready"]
    case = result["cases"][0]
    for attempt in range(2):
        service.regressions.execute_case(plan.package.directory, case, host, Event(),
                                         lambda _: None, lambda: None, tmp_path / f"evidence-{attempt}")
    assert not (pipeline_workspace.root / "business-output").exists()
    copies = list((service.root / "runs").glob("*/work/package/.regression-outputs/out/copy.csv"))
    assert len(copies) == 2
    assert all(path.read_text() == text for path in copies)


@pytest.mark.parametrize("reference", ["output:", "output:../escape", "output:/absolute", "output:."])
def test_regression_output_references_cannot_escape_the_case(reference):
    with pytest.raises(ValueError, match="output reference"):
        validate_cases([{"id": "bad", "files": {}, "params": {"out": reference},
                         "assertions": [{"node": "n", "output": "o", "kind": "text", "expected": "x"}]}])
