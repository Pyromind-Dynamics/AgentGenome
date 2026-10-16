import json

import pytest

from agentgenome.capture.align import (
    ROLE_BRANCH,
    ROLE_GARBAGE,
    ROLE_SKELETON,
    align,
    align_traces,
    alignment_payload,
    classify,
    render_alignment,
    required_support,
    write_alignment,
)

PRELABEL = "python3 scripts/prelabel.py data/raw.csv rules.json > out/prelabeled.jsonl"
CLEAN = "python3 scripts/clean.py out/review.jsonl > out/clean.csv"
TRAIN = "python3 scripts/train.py out/clean.csv --output models/model.pt"
NORMALIZE = "python3 scripts/normalize.py out/prelabeled.jsonl > out/review.jsonl"


@pytest.fixture
def abc_trace(trace_factory, tool_entry, user_entry):
    def build(trace_id, *, prefix="a"):
        return trace_factory(
            [user_entry("u1", "先给数据预打标"),
             tool_entry(f"{prefix}1", PRELABEL),
             tool_entry(f"{prefix}2", CLEAN),
             tool_entry(f"{prefix}3", TRAIN)],
            trace_id,
        )

    return build


def test_required_support_scales_with_trace_count():
    assert required_support(1) == 1
    assert required_support(2) == 2
    assert required_support(3) == 2
    assert required_support(5) == 3
    assert required_support(5, threshold=1.0) == 5
    assert required_support(4, threshold=0.5) == 2


def test_align_prefers_matches_over_mismatches():
    left = [("shell", "A", (), ()), ("shell", "B", (), ())]
    right = [("shell", "A", (), ()), ("shell", "X", (), ()), ("shell", "B", (), ())]
    assert align(left, right) == [(0, 0), (None, 1), (1, 2)]
    # 顺序本身就是语义：交换过的两步宁可各让一个 gap，也不当成匹配。
    assert align(left, [("shell", "B", (), ()), ("shell", "A", (), ())]) == [
        (None, 0), (0, 1), (1, None)]


def test_three_traces_keep_the_common_steps_and_record_the_branch(
        abc_trace, trace_factory, tool_entry):
    traces = [abc_trace("s1", prefix="a"), abc_trace("s2", prefix="b"),
              trace_factory([tool_entry("c1", PRELABEL), tool_entry("c2", NORMALIZE),
                             tool_entry("c3", CLEAN), tool_entry("c4", TRAIN)], "s3")]
    alignment = align_traces(traces)

    assert alignment.required_support == 2
    assert alignment.support_total == 3
    assert not alignment.single_trace
    assert alignment.uniform_support
    assert [slot.role for slot in alignment.slots] == [
        ROLE_SKELETON, ROLE_BRANCH, ROLE_SKELETON, ROLE_SKELETON]
    assert [slot.command_shape for slot in alignment.slots] == [
        "python3 <script:prelabel> <path> <path> > <path>",
        "python3 <script:normalize> <path> > <path>",
        "python3 <script:clean> <path> > <path>",
        "python3 <script:train> <path> --output <path>",
    ]
    assert [slot.support for slot in alignment.slots] == [3, 1, 3, 3]
    assert alignment.roles(ROLE_SKELETON) == (0, 2, 3)
    assert alignment.roles(ROLE_BRANCH) == (1,)
    assert alignment.roles(ROLE_GARBAGE) == ()
    assert alignment.slots[0].trace_ids == ("s1", "s2", "s3")
    assert alignment.slots[1].evidence == (("s3", 1),)


def test_parameter_differences_are_recorded_per_slot(trace_factory, tool_entry):
    traces = [
        trace_factory([tool_entry("a1", "python3 scripts/clean.py data/one.csv > out/one.csv")],
                      "s1"),
        trace_factory([tool_entry("b1", "python3 scripts/clean.py data/two.csv > out/two.csv")],
                      "s2"),
    ]
    alignment = align_traces(traces)
    slot = alignment.slots[0]
    assert slot.support == 2
    assert slot.read_paths == ("data/one.csv", "data/two.csv", "scripts/clean.py")
    assert slot.write_paths == ("out/one.csv", "out/two.csv")
    assert slot.varies

    same = align_traces([
        traces[0],
        trace_factory([tool_entry("c1", "python3 scripts/clean.py data/one.csv > out/one.csv")],
                      "s3"),
    ])
    assert not same.slots[0].varies


def test_failed_and_rejected_attempts_land_in_garbage(trace_factory, tool_entry):
    traces = [
        trace_factory([tool_entry("a1", PRELABEL),
                       tool_entry("a2", "python3 scripts/broken.py out/x.jsonl > out/y.jsonl",
                                  text="Traceback", is_error=True)], "s1"),
        trace_factory([tool_entry("b1", PRELABEL),
                       tool_entry("b2", "rm -rf out/raw",
                                  text="User denied terminal command", is_error=True)], "s2"),
    ]
    alignment = align_traces(traces)
    assert alignment.roles(ROLE_SKELETON) == (0,)
    assert alignment.roles(ROLE_GARBAGE) == (1, 2)
    failed, rejected = alignment.slots[1], alignment.slots[2]
    assert failed.support == 0 and failed.attempts == 1 and failed.trace_ids == ()
    assert failed.read_paths == () and failed.write_paths == ()
    assert rejected.support == 0 and rejected.attempts == 1


def test_a_failure_then_a_success_on_the_same_signature_is_a_branch(trace_factory, tool_entry):
    traces = [
        trace_factory([tool_entry("a1", PRELABEL),
                       tool_entry("a2", NORMALIZE, text="ModuleNotFoundError",
                                  is_error=True)], "s1"),
        trace_factory([tool_entry("b1", PRELABEL), tool_entry("b2", NORMALIZE)], "s2"),
    ]
    alignment = align_traces(traces)
    normalize = alignment.slots[1]
    assert normalize.role == ROLE_BRANCH
    assert normalize.support == 1
    assert normalize.attempts == 2
    assert normalize.trace_ids == ("s2",)


def test_single_trace_is_flagged_and_keeps_every_step_as_a_candidate(abc_trace):
    alignment = align_traces([abc_trace("solo")])
    assert alignment.single_trace
    assert alignment.support_total == 1
    assert alignment.uniform_support
    assert alignment.roles(ROLE_SKELETON) == (0, 1, 2)
    assert "单轨迹" in render_alignment(alignment)


def test_traces_without_tool_steps_are_skipped_from_the_denominator(
        abc_trace, trace_factory, user_entry):
    alignment = align_traces([abc_trace("s1"), abc_trace("s2", prefix="b"),
                              trace_factory([user_entry("u1", "只有对话，没有工具调用")], "s3")])
    assert alignment.skipped == ("s3",)
    assert alignment.support_total == 2
    assert alignment.required_support == 2
    assert alignment.uniform_support
    assert "跳过（无工具步骤）：s3" in render_alignment(alignment)


def test_align_traces_rejects_input_without_alignable_steps(
        trace_factory, user_entry):
    with pytest.raises(ValueError, match="at least one trace"):
        align_traces([])
    with pytest.raises(ValueError, match="no tool steps"):
        align_traces([trace_factory([user_entry("u1", "闲聊")], "s1")])


def test_payload_and_report_describe_the_alignment(
        tmp_path, abc_trace, trace_factory, tool_entry):
    alignment = align_traces([abc_trace("s1"), abc_trace("s2", prefix="b"),
                              trace_factory([tool_entry("c1", PRELABEL), tool_entry("c2", NORMALIZE),
                                             tool_entry("c3", CLEAN), tool_entry("c4", TRAIN)], "s3")])
    payload = alignment_payload(alignment)
    assert payload["trace_count"] == 3
    assert payload["support_total"] == 3
    assert payload["skeleton"] == [0, 2, 3]
    assert payload["branches"] == [1]
    assert payload["garbage"] == []
    assert payload["slots"][1]["varies"] is False

    target = write_alignment(tmp_path / "capture", alignment)
    written = json.loads(target.read_text())
    assert written["slots"][1]["command_shape"] == "python3 <script:normalize> <path> > <path>"
    assert written["slots"][1]["role"] == ROLE_BRANCH

    report = render_alignment(alignment)
    assert "骨架要求 support >= 2" in report
    assert "| 1 | branch | `python3 <script:normalize> <path> > <path>` | 1/3 | 否 |" in report
    assert "骨架 3 步 / 分支候选 1 步 / 垃圾 0 步" in report


def test_classify_keeps_the_slot_order(abc_trace):
    alignment = align_traces([abc_trace("s1"), abc_trace("s2", prefix="b")])
    assert classify(alignment.slots, required=2) == alignment.slots
