import pytest

from agentgenome.capture.align import ROLE_SKELETON, align_traces
from agentgenome.capture.slice import (
    SliceError,
    producer_map,
    render_slice,
    reverse_slice,
    slice_payload,
)


@pytest.fixture
def aligned(pipeline_trace):
    traces = [pipeline_trace("s1"), pipeline_trace("s2", prefix="b")]
    return align_traces(traces)


def test_producer_map_keeps_the_last_writer(pipeline_trace, tool_entry):
    traces = [pipeline_trace("s1")]
    alignment = align_traces(traces)
    producers = producer_map(alignment.slots)
    assert producers["out/prelabeled.jsonl"] == 0
    assert producers["out/clean.csv"] == 1
    assert producers["models/model.pt"] == 2
    assert producers["reports/metrics.json"] == 3


def test_full_slice_keeps_every_skeleton_step(aligned):
    result = reverse_slice(aligned)
    assert result.full
    assert result.nodes == (0, 1, 2, 3)
    assert result.auxiliary == ()
    assert result.final_artifact is None
    assert "未声明最终产物，使用全量骨架" in render_slice(result, aligned.slots)


def test_reverse_slice_stops_at_the_human_boundary(aligned):
    result = reverse_slice(aligned, "reports/metrics.json")
    assert result.nodes == (1, 2, 3)
    assert result.auxiliary == (0,)
    assert result.final_artifact == "reports/metrics.json"
    report = render_slice(result, aligned.slots)
    assert "最终产物：`reports/metrics.json`" in report
    assert "入图 3 步 / 辅助 1 步" in report
    assert "| 0 | `python3 <script:prelabel> <path> <path> > <path>` | out/prelabeled.jsonl |" in report
    payload = slice_payload(result, aligned.slots)
    assert payload["full"] is False
    assert [item["order"] for item in payload["nodes"]] == [1, 2, 3]
    assert [item["order"] for item in payload["auxiliary"]] == [0]


def test_reverse_slice_rejects_an_unknown_artifact(aligned):
    with pytest.raises(SliceError, match="不在任何骨架步骤的产物里"):
        reverse_slice(aligned, "reports/unknown.json")


def test_reverse_slice_rejects_an_alignment_without_skeleton(trace_factory, tool_entry):
    traces = [
        trace_factory([tool_entry("a1", "python3 scripts/alpha.py > out/a.csv")], "s1"),
        trace_factory([tool_entry("b1", "python3 scripts/beta.py > out/b.csv")], "s2"),
        trace_factory([tool_entry("c1", "python3 scripts/gamma.py > out/c.csv")], "s3"),
    ]
    alignment = align_traces(traces)
    assert alignment.roles(ROLE_SKELETON) == ()
    with pytest.raises(SliceError, match="没有骨架步骤"):
        reverse_slice(alignment)
