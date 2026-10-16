import pytest

from agentgenome.dispatch import invoke


def capture(service, host, refs, **arguments):
    return invoke(service, "conv-1", "capture",
                  {"trace_refs": refs, "label": "预打标到评测",
                   "asset_id": "prelabel-train-eval", **arguments},
                  "call-1", host=host)


def test_capture_returns_a_plan_and_queues_the_replay(
        service, host, pipeline_workspace, trace_refs):
    plan = capture(service, host, trace_refs({"trace_id": "s1"}, {"trace_id": "s2", "prefix": "b"}),
                   new_params=pipeline_workspace.params(raw="data/raw2.csv",
                                                       review="out/review2.jsonl"))
    assert plan["asset_id"] == "prelabel-train-eval"
    assert plan["version"] == "0.1.0"
    assert plan["reused"] is False
    assert plan["generalized"] is None
    assert "换参数回放将在回放 A 成功后由服务执行" in plan["notes"]

    state = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert state["result"]["capture"]["generalized"] is True

    status = invoke(service, "conv-1", "capture_status", {"capture_id": plan["capture_id"]},
                    "call-2")
    assert status["stage"] == "published"
    assert status["generalized"] is True
    assert status["run"]["status"] == "succeeded"
    assert service.get(plan["asset_id"], plan["version"])["name"] == "预打标到评测"


def test_capture_falls_back_to_a_hashed_id_for_a_non_ascii_label(
        service, host, trace_refs):
    plan = invoke(service, "conv-1", "capture",
                  {"trace_refs": trace_refs({"trace_id": "s1"}), "label": "预打标到评测"},
                  "call-1", host=host)
    assert plan["asset_id"].startswith("capture-")
    with pytest.raises(ValueError, match="资产 id"):
        invoke(service, "conv-1", "capture",
               {"trace_refs": trace_refs({"trace_id": "s1"}), "label": "预打标到评测",
                "asset_id": "预打标"}, "call-5", host=host)


def test_capture_reuses_the_package_for_the_same_traces(
        service, host, pipeline_workspace, trace_refs):
    refs = trace_refs({"trace_id": "s1"}, {"trace_id": "s2", "prefix": "b"})
    first = capture(service, host, refs)
    service.execute("conv-1", first["run_id"], host, lambda event: None)

    second = capture(service, host, refs)
    assert second["reused"] is True
    assert second["run_id"] is None
    assert second["pin"] == first["pin"]
    assert len(service.versions(first["asset_id"])) == 1


def test_capture_reads_the_trace_files_through_the_host(
        service, pipeline_workspace, trace_refs):
    class NoReader:
        pass

    refs = trace_refs({"trace_id": "s1"})
    with pytest.raises(ValueError, match="cannot read|cannot|host"):
        capture(service, NoReader(), refs)


def test_capture_rejects_missing_arguments(service, host, trace_refs):
    refs = trace_refs({"trace_id": "s1"})
    with pytest.raises(ValueError, match="trace_refs"):
        capture(service, host, [])
    with pytest.raises(ValueError, match="label"):
        invoke(service, "conv-1", "capture", {"trace_refs": refs}, "call-3", host=host)
    with pytest.raises(ValueError, match="new_params"):
        capture(service, host, refs, new_params=["data/raw2.csv"])
    with pytest.raises(ValueError, match="final_artifact"):
        capture(service, host, refs, final_artifact=7)


def test_capture_reports_a_missing_trace_file(service, host, trace_refs):
    refs = trace_refs({"trace_id": "s1"})
    refs[0]["path"] = "public_data/agentgenome-traces/ghost.json"
    with pytest.raises(FileNotFoundError):
        capture(service, host, refs)


def test_capture_status_rejects_an_unknown_id(service):
    with pytest.raises(ValueError, match="capture not found"):
        invoke(service, "conv-1", "capture_status", {"capture_id": "nope"}, "call-4")
