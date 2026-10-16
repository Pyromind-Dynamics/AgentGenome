import json
from pathlib import Path

from agentgenome.capture.emit import LocalSource
from agentgenome.capture.pipeline import prepare_capture
from agentgenome.capture.sample import SAMPLE_BYTES, inspect, sample_inputs
from agentgenome.dispatch import invoke


def write(root: Path, name: str, text: str) -> str:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return name


def test_csv_sample_keeps_the_header_and_the_first_rows(tmp_path):
    write(tmp_path, "data/raw.csv", "text,label\none,a\ntwo,b\n")
    index = sample_inputs(LocalSource(tmp_path), {"raw": "data/raw.csv"},
                          tmp_path / "samples")
    entry = index["files"][0]
    assert entry["key"] == "raw"
    assert entry["format"] == "csv"
    assert entry["schema"] == {"columns": ["text", "label"], "delimiter": ","}
    assert entry["rows"] == 2
    assert entry["truncated"] is False
    assert entry["bytes"] == len("text,label\none,a\ntwo,b\n")
    assert (tmp_path / "samples" / entry["name"]).read_text() == "text,label\none,a\ntwo,b\n"
    written = json.loads((tmp_path / "samples" / "index.json").read_text())
    assert written["files"] == [entry]
    assert written["budget"]["used"] == entry["bytes"]


def test_large_input_is_truncated_to_a_whole_line_head(tmp_path):
    rows = "".join(f"row{index},value{index}\n" for index in range(5000))
    write(tmp_path, "big.csv", "name,value\n" + rows)
    entry = inspect(LocalSource(tmp_path), "big.csv")
    assert entry["truncated"] is True
    assert entry["bytes"] is None
    assert entry["rows"] == 20
    assert len(entry["text"].encode("utf-8")) <= SAMPLE_BYTES
    assert entry["text"].endswith("\n")
    assert entry["note"] == "只保留文件头部"


def test_jsonl_sample_reports_the_fields_it_saw(tmp_path):
    write(tmp_path, "out/review.jsonl",
          json.dumps({"text": "a", "label": "x"}) + "\n"
          + json.dumps({"text": "b", "extra": 1}) + "\n")
    entry = inspect(LocalSource(tmp_path), "out/review.jsonl")
    assert entry["format"] == "jsonl"
    assert entry["schema"] == {"fields": ["text", "label", "extra"]}
    assert entry["rows"] == 2


def test_binary_input_keeps_metadata_only(tmp_path):
    (tmp_path / "weights.bin").write_bytes(b"\x00\x01\x02\x03")
    entry = inspect(LocalSource(tmp_path), "weights.bin")
    assert entry["format"] == "binary"
    assert entry["text"] is None
    assert entry["bytes"] == 4
    assert entry["sha256"]


def test_missing_input_is_recorded_not_raised(tmp_path):
    entry = inspect(LocalSource(tmp_path), "gone.csv")
    assert entry["format"] == "unavailable"
    assert entry["text"] is None
    assert "读取失败" in entry["note"]


def test_sampling_records_the_budget_it_could_not_spend(tmp_path):
    write(tmp_path, "data/raw.csv", "text,label\none,a\n")
    index = sample_inputs(LocalSource(tmp_path), {"raw": "data/raw.csv"},
                          tmp_path / "samples", budget=0)
    entry = index["files"][0]
    assert entry["name"] is None
    assert entry["format"] == "unavailable"
    assert entry["note"] == "采样预算已用完"
    assert not (tmp_path / "samples" / "raw.csv").exists()


def capture(service, workspace, traces, **kwargs):
    return prepare_capture(service, traces=traces, label="预打标到评测",
                           workspace=workspace.root, scope="conv-1", **kwargs)


def test_capture_samples_the_root_inputs_and_reports_them(
        service, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    plan = capture(service, pipeline_workspace, traces)

    index = json.loads(Path(plan.samples["index"]).read_text(encoding="utf-8"))
    entries = {entry["key"]: entry for entry in index["files"]}
    assert set(entries) == {"raw", "review", "rules"}
    assert entries["raw"]["format"] == "csv"
    assert entries["review"]["format"] == "jsonl"
    assert entries["rules"]["format"] == "json"

    report = plan.report_path.read_text(encoding="utf-8")
    assert "### 输入采样" in report
    assert "`raw` ← `data/raw.csv`" in report
    assert "字段 `text`、`label`" in report
    assert f"采样索引：`{plan.samples['index']}`" in report
    assert service.captures.status(plan.capture_id)["samples"]["index"] == plan.samples["index"]


def test_reused_capture_keeps_the_original_samples(
        service, pipeline_workspace, pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    first = capture(service, pipeline_workspace, traces)
    second = capture(service, pipeline_workspace, traces)

    assert second.reused is True
    assert second.samples["index"] == first.samples["index"]
    assert service.captures.status(second.capture_id)["samples"]["index"] \
        == first.samples["index"]


def test_genome_get_and_list_expose_the_samples(
        service, host, pipeline_workspace, trace_refs):
    refs = trace_refs({"trace_id": "s1"}, {"trace_id": "s2", "prefix": "b"})
    plan = invoke(service, "conv-1", "capture",
                  {"trace_refs": refs, "label": "预打标到评测",
                   "asset_id": "prelabel-train-eval"}, "call-1", host=host)
    assert plan["samples"].endswith("samples/index.json")
    service.execute("conv-1", plan["run_id"], host, lambda event: None)

    asset_id, version = plan["asset_id"], plan["version"]
    fetched = invoke(service, "conv-1", "get",
                     {"asset_id": asset_id, "version": version}, "call-2", host=host)
    samples = fetched["input_samples"]
    assert [entry["key"] for entry in samples["files"]] == ["raw", "review", "rules"]
    assert samples["files"][0]["preview"] == "text,label\none,a\ntwo,b\n"
    assert samples["files"][0]["schema"]["columns"] == ["text", "label"]
    assert samples["index"] == plan["samples"]

    listed = invoke(service, "conv-1", "list", {}, "call-3", host=host)
    assert [asset["has_samples"] for asset in listed["assets"]] == [True]
