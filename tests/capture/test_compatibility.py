import json

import pytest

from agentgenome.capture.sample import compatibility
from agentgenome.dispatch import invoke


def captured(*entries):
    return {"files": list(entries)}


def observed(**kwargs):
    return [("raw", {"path": "data/raw.csv", **kwargs})]


def test_compatibility_stops_on_a_format_mismatch():
    report = compatibility(
        captured({"key": "raw", "format": "csv", "schema": {"columns": ["text"]}, "rows": 2}),
        {"raw": {"format": "csv"}},
        observed(format="jsonl", schema={"fields": ["text"]}, rows=3))
    assert report["problems"] == ["raw：期望 csv，实际 jsonl（data/raw.csv）"]
    assert report["warnings"] == []


def test_compatibility_requires_the_declared_fields():
    report = compatibility(
        captured({"key": "raw", "format": "csv", "schema": {"columns": ["text", "label"]}, "rows": 2}),
        {"raw": {"format": "csv", "required_fields": ["text", "qid"],
                 "fields": ["text", "qid", "label"]}},
        observed(format="csv", schema={"columns": ["content", "label"]}, rows=2))
    assert report["problems"] == ["raw：缺少必需字段 ['text', 'qid']"]
    assert report["warnings"] == ["raw：本次输入没有出现声明字段 ['text', 'qid']",
                                  "raw：本次输入没有出现采样字段 ['text']"]


def test_compatibility_warns_about_unreadable_and_empty_inputs():
    index = captured({"key": "raw", "format": "csv", "schema": {"columns": ["text"]}, "rows": 2})
    missing = compatibility(index, {"raw": {"format": "csv"}},
                            observed(format="unavailable", note="读取失败：FileNotFoundError"))
    assert missing["problems"] == []
    assert "未做契约校验" in missing["warnings"][0]

    empty = compatibility(index, {}, observed(format="csv", schema={"columns": ["text"]}, rows=0))
    assert empty["warnings"] == ["raw：输入没有记录（只有表头或空文件）"]


def publish(service, host, trace_refs, **declarations):
    refs = trace_refs({"trace_id": "s1"}, {"trace_id": "s2", "prefix": "b"})
    plan = invoke(service, "conv-1", "capture",
                  {"trace_refs": refs, "label": "预打标到评测",
                   "asset_id": "prelabel-train-eval", **declarations},
                  "capture-1", host=host)
    executed = service.execute("conv-1", plan["run_id"], host, lambda event: None)
    assert executed["status"] == "succeeded"
    return plan


CONTRACT = {"input_contract": {"raw": {"format": "csv", "required_fields": ["text"]}}}


def reuse(service, host, plan, request_id, **params):
    return invoke(service, "conv-1", "run",
                  {"asset_id": plan["asset_id"], "version": plan["version"],
                   "params": {"raw": "data/raw.csv", "review": "out/review.jsonl",
                              "rules": "rules.json", **params}},
                  request_id, host=host)


def test_reuse_gate_accepts_an_input_that_keeps_the_contract(
        service, host, pipeline_workspace, trace_refs):
    pipeline_workspace.write("data/other.csv", "text,label\nnine,i\n")
    plan = publish(service, host, trace_refs, **CONTRACT)

    state = reuse(service, host, plan, "run-1", raw="data/other.csv")
    assert state["status"] == "queued"
    assert "compatibility" not in state


def test_reuse_gate_stops_a_broken_input_and_reports_it(
        service, host, pipeline_workspace, trace_refs):
    pipeline_workspace.write("data/raw.jsonl", json.dumps({"text": "one"}) + "\n")
    pipeline_workspace.write("data/other.csv", "content,label\none,a\n")
    plan = publish(service, host, trace_refs, **CONTRACT)

    with pytest.raises(ValueError, match="期望 csv，实际 jsonl"):
        reuse(service, host, plan, "run-1", raw="data/raw.jsonl")
    with pytest.raises(ValueError, match="缺少必需字段"):
        reuse(service, host, plan, "run-2", raw="data/other.csv")

    state = invoke(service, "conv-1", "run",
                   {"asset_id": plan["asset_id"], "version": plan["version"],
                    "params": {"raw": "data/other.csv", "review": "out/review.jsonl",
                               "rules": "rules.json"},
                    "allow_mismatch": True}, "run-3", host=host)
    assert state["status"] == "queued"
    assert state["compatibility"]["forced"] is True
    assert state["compatibility"]["problems"] == ["raw：缺少必需字段 ['text']"]
