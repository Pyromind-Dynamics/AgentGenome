import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from agentgenome.capture.align import align_traces
from agentgenome.capture.emit import (
    EmitError,
    artifact_ext,
    build_package,
    output_type,
    package_payload,
    render_emit,
)
from agentgenome.capture.slice import reverse_slice

SCRIPT_NAMES = ("prelabel", "clean", "train", "evaluate", "normalize", "broken")


@dataclass(frozen=True)
class RemoteSource:
    """A host that answers a missing file with a request error, not `OSError`."""

    root: Path

    def read(self, path: str) -> bytes:
        target = self.root / path
        if not target.is_file():
            raise RuntimeError(f"sandbox file request failed: FILE_READ_ERROR {path}")
        return target.read_bytes()


class BatchedRemoteSource:
    """A host that answers many reads in one call and records every batch."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[list[str]] = []

    def read(self, path: str) -> bytes:
        raise AssertionError("a batching host must not be read one file at a time")

    def read_many(self, paths: list[str]) -> dict[str, bytes | None]:
        self.calls.append(list(paths))
        answers: dict[str, bytes | None] = {}
        for path in paths:
            target = self.root / path
            answers[path] = target.read_bytes() if target.is_file() else None
        return answers


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "scripts").mkdir(parents=True)
    for name in SCRIPT_NAMES:
        (root / "scripts" / f"{name}.py").write_text(
            f"print('{name}')\n", encoding="utf-8")
    return root


@pytest.fixture
def capture(tmp_path, workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    alignment = align_traces(traces)

    def build(*, final_artifact=None, directory=None, label="预打标到评测"):
        slice_ = reverse_slice(alignment, final_artifact)
        return build_package(
            directory or tmp_path / "capture" / "package",
            alignment=alignment,
            slice_=slice_,
            traces=traces,
            label=label,
            workspace=workspace,
        )

    return build


def test_output_type_follows_the_extension():
    assert output_type("reports/metrics.json") == "json"
    assert output_type("out/clean.csv") == "text"
    assert output_type("models/model.pt") == "path"


def test_artifact_ext_follows_the_captured_output_name():
    assert artifact_ext("out/clean.jsonl") == ".jsonl"
    assert artifact_ext("models/model.PT") == ".pt"
    assert artifact_ext("out/archive") is None
    assert artifact_ext("out/file.notarealsuffix") is None


def test_full_pipeline_emits_a_compiled_package(capture):
    package = capture()
    assert [node.id for node in package.nodes] == ["prelabel", "clean", "train", "evaluate"]
    assert [node.command for node in package.nodes] == [
        "python3 scripts/prelabel.py {input.raw} {input.rules} > {artifact}",
        "python3 scripts/clean.py {input.review} > {artifact}",
        "python3 scripts/train.py {input.clean} --epochs 3 --output {artifact}",
        "python3 scripts/evaluate.py {input.model} {input.clean} > {artifact}",
    ]
    assert package.params == (
        ("raw", "data/raw.csv"), ("review", "out/review.jsonl"), ("rules", "rules.json"))
    assert package.graph.params == frozenset({"raw", "review", "rules"})
    assert package.solidifiable
    assert package.graph.pin
    assert package.outputs == (("prelabeled", "text"), ("clean", "text"),
                               ("model", "path"), ("metrics", "json"))
    assert (package.directory / "scripts" / "train.py").read_text() == "print('train')\n"


def test_repeated_command_shapes_get_unique_sibling_ids(tmp_path, workspace, trace_factory,
                                                       tool_entry):
    trace = trace_factory([
        tool_entry("a1", "cd storage/data && ls -la"),
        tool_entry("a2", "cd storage/data && ls -la"),
        tool_entry("a3", "python3 scripts/clean.py data/raw.csv > out/clean.csv"),
    ], "s1")
    alignment = align_traces([trace])
    package = build_package(tmp_path / "package", alignment=alignment,
                            slice_=reverse_slice(alignment), traces=[trace],
                            label="重复命令", workspace=workspace)
    assert [node.id for node in package.nodes] == ["cd", "cd-2", "clean"]


def test_graph_yaml_carries_provenance_and_slots(capture):
    package = capture()
    data = yaml.safe_load((package.directory / "graph.yaml").read_text(encoding="utf-8"))
    assert data["version"] == "gt/1.0"
    root = data["node"]
    assert root["id"] == package.root_id
    assert root["mode"] == "sequence"
    assert list(root["input"]) == ["raw", "review", "rules"]
    assert root["output"] == {"metrics": {"type": "json", "compose": ["evaluate.output.metrics"]}}
    assert root["verify"] == [{"run": "test -s {output.metrics}"}]
    assert [child["id"] for child in root["children"]] == [
        "prelabel", "clean", "train", "evaluate"]
    prelabel = root["children"][0]
    assert prelabel["provenance"] == ["s1:1", "s2:1"]
    assert prelabel["input"] == {
        "raw": {"type": "path", "from": f"{package.root_id}.input.raw"},
        "rules": {"type": "path", "from": f"{package.root_id}.input.rules"},
    }
    assert prelabel["output"] == {"prelabeled": {"type": "text"}}
    assert prelabel["artifact"] == {"ext": "jsonl"}
    assert prelabel["verify"] == [{"run": "test -s {output.prelabeled}"}]
    train = root["children"][2]
    assert train["input"]["clean"]["from"] == "clean.output.clean"
    assert train["artifact"] == {"ext": "pt"}


def test_manifest_matches_the_compiled_graph(capture):
    package = capture()
    manifest = json.loads((package.directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == package.asset_id
    assert manifest["version"] == "0.1.0"
    assert manifest["owner"] == "local"
    assert set(manifest["parameters"]) == set(package.graph.params)
    assert manifest["expected_outputs"] == {
        "metrics": "由节点 evaluate 产出，原轨迹路径 reports/metrics.json"}
    assert manifest["revision"]["editable_scripts"] == [
        "scripts/clean.py", "scripts/evaluate.py", "scripts/prelabel.py", "scripts/train.py"]
    assert manifest["capture"]["traces"] == ["s1", "s2"]
    assert manifest["capture"]["single_trace"] is False


def test_reverse_slice_drops_auxiliary_steps_from_the_graph(capture):
    package = capture(final_artifact="reports/metrics.json")
    assert [node.id for node in package.nodes] == ["clean", "train", "evaluate"]
    assert package.auxiliary == (0,)
    assert package.params == (("review", "out/review.jsonl"),)
    assert "辅助步骤（不在切片内，未入图）：#0" in render_emit(package)


def test_inline_code_is_reported_instead_of_guessed(tmp_path, workspace, trace_factory,
                                                   tool_entry):
    trace = trace_factory(
        [tool_entry("a1", 'python3 -c "import pandas; print(pandas.__version__)" '
                          "> out/version.json")], "s1")
    alignment = align_traces([trace])
    package = build_package(
        tmp_path / "package", alignment=alignment, slice_=reverse_slice(alignment),
        traces=[trace], label="inline", workspace=workspace)
    assert not package.solidifiable
    assert package.nodes == ()
    assert package.not_solidifiable == ((0, "内联代码未固化"),)
    assert package.graph.pin
    report = render_emit(package)
    assert "**未固化步骤**：#0 内联代码未固化" in report


def test_editor_written_script_is_absorbed_by_the_script_copy(tmp_path, workspace,
                                                              trace_factory, tool_entry):
    trace = trace_factory([
        tool_entry("a1", "", tool="write",
                   args={"path": "scripts/clean.py", "content": "print('from the editor')"}),
        tool_entry("a2", "python3 scripts/clean.py data/raw.csv > out/clean.csv"),
    ], "s1")
    alignment = align_traces([trace])
    package = build_package(
        tmp_path / "package", alignment=alignment, slice_=reverse_slice(alignment),
        traces=[trace], label="写脚本", workspace=workspace)
    assert [node.id for node in package.nodes] == ["clean"]
    assert package.absorbed == ((0, "脚本创建步骤已由脚本搬运吸收"),)
    assert package.scripts == (("scripts/clean.py", "scripts/clean.py"),)
    assert (package.directory / "scripts" / "clean.py").read_text() == "print('clean')\n"


def test_missing_script_fails_loudly(tmp_path, workspace, trace_factory, tool_entry):
    trace = trace_factory(
        [tool_entry("a1", "python3 scripts/ghost.py data/raw.csv > out/ghost.csv")], "s1")
    alignment = align_traces([trace])
    with pytest.raises(EmitError, match="脚本不存在"):
        build_package(tmp_path / "package", alignment=alignment,
                      slice_=reverse_slice(alignment), traces=[trace], label="缺脚本",
                      workspace=workspace)


def test_copied_script_brings_its_runtime_siblings(tmp_path, workspace, trace_factory,
                                                   tool_entry):
    """A CLI that shells out to sibling helpers must not be frozen alone."""
    (workspace / "scripts/runner.py").write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "here = Path(__file__).parent\n"
        "subprocess.run([sys.executable, here / 'fingerprint.py'])\n"
        "subprocess.run([sys.executable, here / 'report.py'])\n"
        "subprocess.run([sys.executable, here / 'optional.py'])\n",
        encoding="utf-8")
    (workspace / "scripts/fingerprint.py").write_text(
        "from report import write\nwrite()\n", encoding="utf-8")
    (workspace / "scripts/report.py").write_text("def write(): pass\n", encoding="utf-8")
    trace = trace_factory(
        [tool_entry("a1", "python3 scripts/runner.py data/raw.csv > out/clean.csv")], "s1")
    alignment = align_traces([trace])
    package = build_package(
        tmp_path / "package", alignment=alignment, slice_=reverse_slice(alignment),
        traces=[trace], label="运行目录", workspace=workspace)
    assert package.scripts == (
        ("scripts/fingerprint.py", "scripts/fingerprint.py"),
        ("scripts/report.py", "scripts/report.py"),
        ("scripts/runner.py", "scripts/runner.py"),
    )
    for name in ("runner.py", "fingerprint.py", "report.py"):
        assert (package.directory / "scripts" / name).is_file()
    assert not (package.directory / "scripts/optional.py").exists()


def test_runtime_sibling_probe_survives_a_remote_read_error(tmp_path, workspace,
                                                            trace_factory, tool_entry):
    """A name a script merely mentions is not a sibling, and a remote host says so
    with its own error type: `dataflow/serving/__init__.py` in a comment and
    `serving/api_llm_serving_request.py` in a string must not break the copy."""
    (workspace / "scripts" / "filter_generate.py").write_text(
        "# 绕过 dataflow/serving/__init__.py 的注册\n"
        "REQUEST = 'serving/api_llm_serving_request.py'\n"
        "print(REQUEST)\n", encoding="utf-8")
    trace = trace_factory(
        [tool_entry("a1", "python3 scripts/filter_generate.py data/raw.csv "
                          "> out/clean.csv")], "s1")
    alignment = align_traces([trace])
    package = build_package(
        tmp_path / "package", alignment=alignment, slice_=reverse_slice(alignment),
        traces=[trace], label="远程读取", workspace=RemoteSource(workspace))
    assert package.scripts == (
        ("scripts/filter_generate.py", "scripts/filter_generate.py"),
    )
    assert (package.directory / "scripts" / "filter_generate.py").is_file()


def test_script_copy_and_sibling_probes_share_one_batch_per_wave(
        tmp_path, workspace, trace_factory, tool_entry):
    """A remote host answers a whole wave of reads per round trip."""
    (workspace / "scripts/runner.py").write_text(
        "subprocess.run(['fingerprint.py'])\n"
        "subprocess.run(['report.py'])\n"
        "subprocess.run(['optional.py'])\n", encoding="utf-8")
    (workspace / "scripts/fingerprint.py").write_text(
        "from report import write\nwrite()\n", encoding="utf-8")
    (workspace / "scripts/report.py").write_text("def write(): pass\n", encoding="utf-8")
    trace = trace_factory(
        [tool_entry("a1", "python3 scripts/runner.py data/raw.csv > out/clean.csv")], "s1")
    alignment = align_traces([trace])
    source = BatchedRemoteSource(workspace)
    package = build_package(
        tmp_path / "package", alignment=alignment, slice_=reverse_slice(alignment),
        traces=[trace], label="批量读取", workspace=source)
    assert package.scripts == (
        ("scripts/fingerprint.py", "scripts/fingerprint.py"),
        ("scripts/report.py", "scripts/report.py"),
        ("scripts/runner.py", "scripts/runner.py"),
    )
    assert source.calls == [
        ["scripts/runner.py"],
        ["scripts/fingerprint.py", "scripts/optional.py", "scripts/report.py"],
        ["scripts/fingerprint.py", "scripts/report.py"],
        ["scripts/fingerprint.py", "scripts/report.py", "scripts/runner.py"],
    ]
    for name in ("runner.py", "fingerprint.py", "report.py"):
        assert (package.directory / "scripts" / name).is_file()


def test_in_place_read_write_is_rejected(tmp_path, workspace, trace_factory, tool_entry):
    trace = trace_factory(
        [tool_entry("a1", "python3 scripts/clean.py out/clean.csv > out/clean.csv")], "s1")
    alignment = align_traces([trace])
    with pytest.raises(EmitError, match="原地读写同一路径"):
        build_package(tmp_path / "package", alignment=alignment,
                      slice_=reverse_slice(alignment), traces=[trace], label="原地改",
                      workspace=workspace)


def test_same_traces_produce_the_same_pin(capture, tmp_path):
    first = capture(directory=tmp_path / "a")
    second = capture(directory=tmp_path / "b")
    assert first.graph.pin == second.graph.pin
    assert package_payload(first)["nodes"] == package_payload(second)["nodes"]


def test_labels_without_ascii_still_get_a_usable_asset_id(capture):
    package = capture(label="预打标到评测")
    assert package.label == "预打标到评测"
    assert package.asset_id.startswith("capture-")
    assert package.root_id.startswith("capture-")
    assert package.root_id == package.asset_id
