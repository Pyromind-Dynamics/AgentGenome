import threading

import pytest

from agentgenome.capture.align import align_traces
from agentgenome.capture.emit import build_package
from agentgenome.capture.replay import (
    ReplayError,
    probe_replay,
    queue_replay,
    read_case,
    values,
)
from agentgenome.capture.slice import reverse_slice
from core.ports import Ports


@pytest.fixture
def package(tmp_path, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    alignment = align_traces(traces)
    return build_package(tmp_path / "package", alignment=alignment,
                         slice_=reverse_slice(alignment), traces=traces,
                         label="预打标到评测", workspace=pipeline_workspace.root)


def test_probe_replay_runs_the_package_with_new_parameters(
        tmp_path, host, package, pipeline_workspace):
    case = probe_replay(host, package,
                        pipeline_workspace.params(raw="data/raw2.csv",
                                                  review="out/review2.jsonl"),
                        run_id="probe-1", run_dir=tmp_path / "probe")
    assert case.passed
    assert case.run_id is None
    assert case.failure is None
    assert case.params[0][1] == "data/raw2.csv"
    assert (tmp_path / "probe" / "ledger.jsonl").is_file()


def test_probe_replay_fails_loudly_when_the_new_data_does_not_fit(
        tmp_path, host, package, pipeline_workspace):
    pipeline_workspace.write("out/review3.jsonl", '{"text": "p-three"}\n')
    case = probe_replay(host, package,
                        pipeline_workspace.params(raw="data/raw2.csv",
                                                  review="out/review3.jsonl"),
                        run_id="probe-2", run_dir=tmp_path / "probe")
    assert not case.passed
    assert case.failure["node"].endswith(".clean")


def test_probe_replay_requires_an_artifact_port(tmp_path, package, pipeline_workspace):
    class Bare:
        def prepare(self, run_id, package_dir, params, paths, cancel, emit):
            return Ports(shell=None), params

    with pytest.raises(ReplayError, match="artifact"):
        probe_replay(Bare(), package, pipeline_workspace.params(),
                     run_id="probe-3", run_dir=tmp_path / "probe")


def test_values_rejects_parameters_the_graph_does_not_declare(package):
    with pytest.raises(ReplayError, match="参数不匹配"):
        values(package, {"raw": "data/raw.csv"})


def test_queue_and_read_replay_a_through_the_service(
        service, host, package, pipeline_workspace):
    case = queue_replay(service, package, dict(package.params),
                        scope="conv-1", request_id="capture-1")
    assert case.status == "queued"
    assert case.run_id
    assert service.versions(package.asset_id) == [
        {"version": package.version, "pin": package.pin, "published": False}]

    service.execute("conv-1", case.run_id, host, lambda event: None)
    finished = read_case(service, "conv-1", case)
    assert finished.passed
    assert finished.outputs["metrics"]["path"].endswith("--evaluate.json")
    assert any(item.endswith("ledger.jsonl") for item in finished.evidence)
