import json

import pytest

from agentgenome.capture.trace import (
    KIND_HUMAN,
    KIND_LLM,
    KIND_READ,
    KIND_SHELL,
    KIND_TOOL,
    KIND_WRITE,
    ROLE_MODEL,
    ROLE_OUTPUT,
    ROLE_REPORT,
    ROLE_RULES,
    ROLE_SCRIPT,
    ROLE_SOURCE,
    capture_trace,
    parse_command,
    placeholder_key,
    render_report,
    role,
    steps_payload,
    write_steps,
)


def user(entry_id, text):
    return {"kind": "user", "entry_id": entry_id, "timestamp": "2026-01-01T00:00:00Z",
            "text": text}


def tool(entry_id, name, args, text="ok", is_error=False):
    return {"kind": "tool", "entry_id": entry_id, "timestamp": "2026-01-01T00:00:01Z",
            "tool": name, "call_id": entry_id, "args": args,
            "result": {"is_error": is_error, "text": text}}


def document(steps, trace_id="session-1"):
    return json.dumps({"trace_version": 1, "trace_id": trace_id, "session_id": trace_id,
                       "session_name": "预打标到评测", "step_count": len(steps), "steps": steps})


def pipeline():
    return [
        user("u1", "先给数据预打标"),
        tool("a1", "bash", {"command": "python3 scripts/prelabel.py data/raw.csv rules.json > out/prelabeled.jsonl"}),
        tool("a2", "bash", {"command": "cat out/prelabeled.jsonl | head -20"}),
        tool("a3", "read", {"path": "out/prelabeled.jsonl"}),
        user("u2", "我去 Label Studio 复核完了，继续清洗"),
        tool("a4", "write", {"path": "scripts/clean.py", "content": "print('clean')"}),
        tool("a5", "bash", {"command": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"},
             text="ModuleNotFoundError: pandas", is_error=True),
        tool("a6", "bash", {"command": "python3 scripts/clean.py out/review.jsonl > out/clean.csv"}),
        tool("a7", "bash", {"command": "python3 -c \"import pandas; print(pandas.__version__)\""}),
        tool("a8", "bash", {"command": "rm -rf out/raw"}, text="User denied terminal command", is_error=True),
        user("u3", "开始训练"),
        tool("a9", "bash", {"command": "python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt"}),
        user("u4", "评测一下"),
        tool("a10", "bash", {"command": "python3 scripts/evaluate.py models/model.pt out/clean.csv > reports/metrics.json"}),
        tool("a11", "task", {"prompt": "summarize the report"}),
    ]


def test_parse_command_keeps_structure_and_hides_values():
    command = parse_command("python3 scripts/clean.py /data/raw.csv rules.json /out/clean.csv")
    assert command.shape == "python3 <script:clean> <path> <path> <path>"
    assert command.reads == (ROLE_RULES, ROLE_SCRIPT, ROLE_SOURCE)
    assert command.writes == ()


def test_parse_command_reads_redirections_and_flags():
    command = parse_command("python3 scripts/train.py out/clean.csv --epochs 3 --output models/model.pt")
    assert command.shape == "python3 <script:train> <path> --epochs <num> --output <path>"
    assert command.read_paths == ("scripts/train.py", "out/clean.csv")
    assert command.write_paths == ("models/model.pt",)
    assert command.writes == (ROLE_MODEL,)


def test_parse_command_marks_inline_code_and_heredocs():
    assert parse_command("python3 -c \"print(1)\"").shape == f"python3 -c <inline>"
    heredoc = parse_command("cat > scripts/clean.py <<'PY'\nprint('clean')\nPY")
    assert heredoc.shape == "cat > <path> << <inline>"
    assert heredoc.write_paths == ("scripts/clean.py",)
    assert heredoc.writes == (ROLE_SCRIPT,)


def test_parse_command_keeps_subcommands_and_module_names():
    assert parse_command("git commit -m 'clean data'").shape == "git commit -m <str>"
    assert parse_command("python3 -m pytest tests -q").shape == "python3 -m pytest tests -q"


def test_parse_command_ignores_paths_inside_command_substitutions():
    command = parse_command(
        "python3 scripts/ls_to_sft.py out/review.jsonl out/sft.jsonl "
        "--sft-root $(dirname out/sft.jsonl)"
    )
    assert command.read_paths == (
        "scripts/ls_to_sft.py", "out/review.jsonl", "out/sft.jsonl")
    assert "out/sft.jsonl)" not in command.read_paths
    assert command.literals == (("--sft-root", "<command-substitution>"),)


def test_parse_command_ignores_nested_quoted_command_substitutions():
    command = parse_command(
        'python3 scripts/run.py input.json --root "$(python3 -c '
        '\'print("out/nested")\')"'
    )
    assert command.read_paths == ("scripts/run.py", "input.json")
    assert command.literals == (("--root", "<command-substitution>"),)


def test_parse_command_handles_script_heads_and_file_moves():
    direct = parse_command("./scripts/run.sh data/raw.csv")
    assert direct.shape == "<script:run> <path>"
    assert direct.read_paths == ("data/raw.csv",)
    move = parse_command("cp data/raw.csv out/raw-copy.csv")
    assert move.shape == "cp <path> <path>"
    assert move.read_paths == ("data/raw.csv",)
    assert move.write_paths == ("out/raw-copy.csv",)


def test_parse_command_ignores_device_redirections():
    probe = parse_command('echo "=== storage ===" && ls storage/ 2>/dev/null | head -50')
    assert probe.write_paths == ()
    assert probe.writes == ()
    assert parse_command("python3 scripts/clean.py data/raw.csv > /dev/null").write_paths == ()


def test_parse_command_gives_every_line_its_own_head():
    command = parse_command(
        "rm -f out/filtered.csv out/report.json\n"
        "timeout 900 python3 .agents/skills/data/run.py \\\n"
        "  --pipeline scripts/filter.py \\\n"
        "  --input data/raw.csv \\\n"
        "  --output out/filtered.csv 2>&1 | tail -25")
    assert command.shape == (
        "rm -f <path> <path> ; timeout <num> python3 <path> --pipeline <path> "
        "--input <path> --output <path> <num> >& <num> | tail -25")
    assert command.read_paths == (".agents/skills/data/run.py", "scripts/filter.py",
                                  "data/raw.csv")
    # `rm` 只清理了 out/filtered.csv，真正产出它的是后面那条命令。
    assert command.write_paths == ("out/report.json", "out/filtered.csv")
    assert command.name == "rm"
    # 换行与续行只是排版：同一命令折行后必须落到同一个形状上。
    assert parse_command(
        "rm -f out/filtered.csv out/report.json; timeout 900 python3 "
        ".agents/skills/data/run.py --pipeline scripts/filter.py --input data/raw.csv "
        "--output out/filtered.csv 2>&1 | tail -25").shape == command.shape


def test_parse_command_treats_brace_tokens_as_parameters():
    command = parse_command("python3 scripts/to_sft.py {input_jsonl} > {output_jsonl}")
    assert command.read_paths == ("scripts/to_sft.py", "{input_jsonl}")
    assert command.write_paths == ("{output_jsonl}",)
    assert command.shape == "python3 <script:to_sft> <path> > <path>"
    assert placeholder_key("{input_jsonl}") == "input_jsonl"
    assert placeholder_key("data/raw.csv") is None
    assert parse_command("python3 scripts/clean.py {1bad} > out/clean.csv").read_paths == (
        "scripts/clean.py",)


def test_capture_trace_drops_probes_that_only_silenced_their_output():
    trace = capture_trace(document([
        tool("a1", "bash", {"command": 'echo "=== find ===" && find storage/ -iname "*test*" 2>/dev/null'}),
    ]))
    assert trace.steps == ()
    assert [item.reason for item in trace.dropped] == ["probe"]


def test_role_classifies_by_extension_and_direction():
    assert role("scripts/clean.py", writing=False) == ROLE_SCRIPT
    assert role("rules.json", writing=False) == ROLE_RULES
    assert role("data/raw.csv", writing=False) == ROLE_SOURCE
    assert role("models/model.pt", writing=True) == ROLE_MODEL
    assert role("reports/metrics.json", writing=True) == ROLE_REPORT
    assert role("out/clean.csv", writing=True) == ROLE_OUTPUT


def test_parse_command_keeps_flag_values_it_cannot_classify():
    command = parse_command(
        "python3 scripts/x.py data/raw.csv --out out/x.csv --images-root storage")
    assert command.literals == (("--images-root", "storage"),)


def test_parse_command_keeps_a_positional_word_that_may_be_a_path():
    assert parse_command("python3 scripts/x.py data/raw.csv storage").literals == \
        (("", "storage"),)


def test_capture_trace_keeps_the_step_numbers_the_export_wrote():
    trace = capture_trace(document([
        {**tool("a1", "bash", {"command": "ls data/"}), "seq": 7},
        {**tool("a2", "bash",
                {"command": "python3 scripts/clean.py data/raw.csv > out/clean.csv"}),
         "seq": 9},
    ]))
    assert [step.seq for step in trace.steps] == [9]
    assert [item.seq for item in trace.dropped] == [7]


def test_capture_trace_normalizes_the_five_step_pipeline():
    trace = capture_trace(document(pipeline()), source="public_data/agentgenome-traces/session-1.json")
    assert trace.trace_id == "session-1"
    assert trace.session_name == "预打标到评测"
    assert [step.seq for step in trace.steps] == list(range(len(trace.steps)))
    assert [step.kind for step in trace.steps] == [
        KIND_HUMAN, KIND_SHELL, KIND_HUMAN, KIND_WRITE, KIND_SHELL, KIND_SHELL, KIND_SHELL,
        KIND_HUMAN, KIND_SHELL, KIND_HUMAN, KIND_SHELL, KIND_LLM,
    ]
    assert [item.reason for item in trace.dropped] == ["probe", "probe", "probe"]
    assert [item.raw_ref for item in trace.dropped] == ["a2", "a3", "a7"]


def test_capture_trace_marks_failed_and_refused_steps():
    trace = capture_trace(document(pipeline()))
    failed, retry = trace.steps[4], trace.steps[5]
    assert failed.command_shape == retry.command_shape
    assert failed.success is False
    assert retry.success is True
    refused = trace.steps[6]
    assert refused.rejected is True
    assert refused.write_paths == ("out/raw",)


def test_capture_trace_keeps_signature_stable_across_parameter_changes():
    first = capture_trace(document([tool("a1", "bash",
                                        {"command": "python3 scripts/clean.py data/one.csv > out/one.csv"})]))
    second = capture_trace(document([tool("b1", "bash",
                                         {"command": "python3 scripts/clean.py data/two.csv > out/two.csv"})]))
    assert first.steps[0].signature == second.steps[0].signature
    assert first.steps[0].read_paths != second.steps[0].read_paths


def test_capture_trace_rejects_unknown_shapes():
    with pytest.raises(ValueError, match="unsupported trace version"):
        capture_trace(json.dumps({"trace_version": 99, "steps": []}))
    with pytest.raises(ValueError, match="no steps"):
        capture_trace(json.dumps({"trace_version": 1}))
    with pytest.raises(ValueError, match="JSON object"):
        capture_trace("[]")


def test_capture_trace_rejects_clipped_tool_arguments():
    entry = tool("a1", "bash", {"command": "python3 scripts/clean.py data.csv > out.csv"})
    entry["clipped_fields"] = ["args.command"]
    with pytest.raises(ValueError, match="命令/工具参数被裁剪（clipped_fields）"):
        capture_trace(document([entry]))
    entry["clipped_fields"] = ["result.text"]
    assert len(capture_trace(document([entry])).steps) == 1


def test_capture_trace_keeps_products_reported_by_successful_tools():
    trace = capture_trace(document([
        tool(
            "a1",
            "bash",
            {"command": "python3 scripts/ls_to_sft.py"},
            text=(
                "wrote 3 rows -> storage/datasets/review/sft.jsonl\n"
                "saved to storage/datasets/review/manifest.json"
            ),
        ),
    ]))

    step = trace.steps[0]
    assert step.write_paths == ()
    assert step.reported_writes == (
        "storage/datasets/review/sft.jsonl",
        "storage/datasets/review/manifest.json",
    )


def test_capture_trace_reads_products_from_a_projected_result_excerpt():
    """The exporter drops progress lines to keep a long session readable.

    It keeps every line that can name a write, so the product survives projection;
    this is the contract between `session-trace.js: projectText` and `_reported_writes`.
    """
    text = "\n".join(
        [f"progress {n} {'x' * 60}" for n in range(20)]
        + ["…[39960 行省略]…"]
        + [f"progress {n} {'x' * 60}" for n in range(39980, 40000)]
        + ["wrote 1200 rows -> public_data/out/cleaned.jsonl", ""]
    )
    entry = tool("a1", "bash", {"command": "python3 scripts/clean.py"}, text=text)
    entry["result"]["excerpt"] = {
        "kept_lines": 40, "total_lines": 40002, "total_chars": 3_000_000,
    }

    trace = capture_trace(document([entry]))

    assert trace.steps[0].write_paths == ()
    assert trace.steps[0].reported_writes == ("public_data/out/cleaned.jsonl",)


def test_capture_trace_keeps_a_product_after_long_success_output():
    trace = capture_trace(document([
        tool(
            "a1",
            "bash",
            {"command": "python3 scripts/ls_to_sft.py"},
            text=(
                "wrote ["
                + ("{'id': 'sample', 'text': 'value'}, " * 600)
                + "] rows -> storage/datasets/review/sft.jsonl\r\n"
            ),
        ),
    ]))

    assert trace.steps[0].write_paths == ()
    assert trace.steps[0].reported_writes == ("storage/datasets/review/sft.jsonl",)


def test_capture_trace_ignores_reported_products_from_failed_or_probe_tools():
    trace = capture_trace(document([
        tool(
            "a1",
            "bash",
            {"command": "python3 scripts/ls_to_sft.py"},
            text="output: storage/datasets/review/sft.jsonl",
            is_error=True,
        ),
        tool(
            "a2",
            "bash",
            {"command": "echo done"},
            text="wrote 3 rows -> storage/datasets/review/fake.jsonl",
        ),
    ]))

    assert trace.steps[0].reported_writes == ()
    assert len(trace.steps) == 1
    assert trace.dropped[0].reason == "probe"


def test_export_truncation_is_preserved_separately_from_filtered_probes():
    exported = json.loads(document([tool("a1", "bash", {"command": "ls data/"})]))
    exported["dropped_steps"] = 30
    trace = capture_trace(json.dumps(exported))
    assert trace.export_dropped_steps == 30
    assert len(trace.dropped) == 1
    assert steps_payload(trace)["export_dropped_steps"] == 30
    assert "导出截断 30 条" in render_report(trace)
    for invalid in (-1, True, "30"):
        exported["dropped_steps"] = invalid
        with pytest.raises(ValueError, match="dropped_steps"):
            capture_trace(json.dumps(exported))


def test_payload_and_report_describe_the_capture(tmp_path):
    trace = capture_trace(document(pipeline()), source="public_data/agentgenome-traces/session-1.json")
    payload = steps_payload(trace)
    assert payload["step_count"] == len(trace.steps)
    assert payload["dropped_count"] == len(trace.dropped)
    assert payload["steps"][1]["signature"] == ["shell", "python3 <script:prelabel> <path> <path> > <path>",
                                                ["rules", "script", "source"], ["output"]]
    assert payload["steps"][3]["writes"] == ["script"]
    target = write_steps(tmp_path / "traces" / "session-1", trace)
    assert json.loads(target.read_text())["trace_id"] == "session-1"
    report = render_report(trace)
    assert "python3 <script:clean> <path> > <path>" in report
    assert "| 3 | write | write | `write <path>` | - | script | ok |" in report
    assert "`a7` bash (probe)" in report


def test_unknown_tools_become_fixed_tool_steps():
    """A tool the exporter does not know is a fixed call, not an agent task.

    It stays out of the graph as captured because the session did not declare where
    its result lands; a distilled tool step supplies that declaration.
    """
    trace = capture_trace(document([tool("a1", "webfetch", {"url": "https://example.com"})]))
    assert trace.steps[0].kind == KIND_TOOL
    assert trace.steps[0].command_shape == "<tool:webfetch>"


def test_unknown_tools_carry_the_paths_their_args_name():
    """宿主工具调用的 args 是它唯一声明输入的地方，路径要进 reads。

    「提交平台执行」这类调用没有命令行可解析，args 里的路径如果不进 reads，这一步
    在链路连通性上就是黑洞：本地试跑与平台提交之间看不出任何关系。
    """
    trace = capture_trace(document([tool("a1", "df_submit_pipeline", {
        "input_path": "datasets/raw.csv", "script_path": "public_data/pipeline.py",
        "model_profile": "vision"})]))
    step = trace.steps[0]
    assert step.kind == KIND_TOOL
    assert step.command_shape == "<tool:df_submit_pipeline>"
    assert step.read_paths == ("datasets/raw.csv", "public_data/pipeline.py")
    assert step.write_paths == ()
