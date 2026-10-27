import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DEFAULT_MAX_STEPS,
  MAX_TRACE_BYTES,
  MAX_COMPLETE_TRACE_BYTES,
  TRACE_DIRECTORY,
  buildTrace,
  exportTraces,
  fitTrace,
  listSessions,
  parseSessionLog,
  projectText,
  traceFileName,
  traceSteps,
} from "../session-trace.js";

function user(id, text, timestamp = "2026-01-01T00:00:00Z") {
  return { type: "message", id, parentId: null, timestamp,
    message: { role: "user", content: [{ type: "text", text }], timestamp } };
}

function assistant(id, content, timestamp = "2026-01-01T00:00:01Z") {
  return { type: "message", id, parentId: null, timestamp,
    message: { role: "assistant", content, timestamp } };
}

function call(id, name, args) {
  return { type: "toolCall", id, name, arguments: args };
}

function result(id, toolCallId, text, { isError = false, timestamp = "2026-01-01T00:00:02Z" } = {}) {
  return { type: "message", id, parentId: null, timestamp,
    message: { role: "toolResult", toolCallId, toolName: "bash",
      content: [{ type: "text", text }], isError, timestamp } };
}

test("writer task provenance records observed inputs and omits thinking and literal output", () => {
  const entries = [user("u", "Summarize data/source.txt"),
    assistant("a", [call("read", "read", { path: "data/source.txt" })]),
    result("r", "read", "input contents"),
    assistant("b", [{ type: "thinking", thinking: "private reasoning" },
      { type: "text", text: "I will summarize the document." },
      call("write", "write", { path: "out/summary.md", content: "LITERAL ANSWER" })]),
    result("w", "write", "written")];
  const step = traceSteps(entries, 400, { complete: true }).steps.at(-1);
  assert.deepEqual(step.agent_context.inputs, ["data/source.txt"]);
  assert.equal(step.agent_context.complete, true);
  assert.equal(step.agent_context.instruction, "Summarize data/source.txt");
  assert.equal(JSON.stringify(step.agent_context).includes("private reasoning"), false);
  assert.equal(JSON.stringify(step.agent_context).includes("LITERAL ANSWER"), false);
  const changedTask = [...entries, user("new", "Create something else"),
    assistant("c", [{ type: "text", text: "New task." }, call("next", "write", { path: "out/next.md" })])];
  assert.deepEqual(traceSteps(changedTask).steps.at(-1).agent_context.inputs, []);
});

function writeStep(instruction, content, extra = []) {
  const entries = [user("u", instruction),
    ...extra,
    assistant("b", [{ type: "text", text: "Record the task result." },
      call("write", "write", { path: "out/decisions.json", content })]),
    result("w", "write", "written")];
  return traceSteps(entries, 400, { complete: true }).steps.at(-1);
}

test("writer content never determines whether the user or model decided it", () => {
  const cases = [
    ["For records 101 and 102 choose keep or reject", '[{"id":101,"decision":"keep"}]'],
    ["For every row my decision is approve", '[{"id":101,"decision":"approve"}]'],
    ["I approve; record my decision", '["approve"]'],
    ["Keep item 1 and classify the rest", '[{"id":1,"decision":"keep"},{"summary":"spam"}]'],
  ];
  for (const [instruction, content] of cases) {
    const step = writeStep(instruction, content);
    assert.equal(step.agent_context.instruction, instruction);
    assert.equal(step.agent_context.origin, undefined);
    assert.equal(JSON.stringify(step.agent_context).includes(content), false);
  }
});

test("writer context retains clipping and missing-content evidence", () => {
  const clipped = writeStep(`${"review ".repeat(500)}keep item 1`, '{"1": "keep"}');
  assert.equal(clipped.agent_context.complete, false);
  assert.equal(clipped.agent_context.origin, undefined);
  const entries = [user("u", "Save my decisions to out/decisions.json."),
    assistant("b", [{ type: "text", text: "Record them." },
      call("write", "write", { path: "out/decisions.json" })]),
    result("w", "write", "written")];
  assert.equal(traceSteps(entries, 400, { complete: true }).steps.at(-1).agent_context.origin, undefined);
  const empty = writeStep("Save my decisions to out/decisions.json.", "");
  assert.equal(empty.agent_context.origin, undefined);
});

function session(id, timestamp = "2026-01-01T00:00:00Z") {
  return { type: "session", id, timestamp, cwd: "/workspace" };
}

async function workspace() {
  return mkdtemp(join(tmpdir(), "agentgenome-trace-"));
}

test("traceSteps keeps user turns in order and pairs each tool call with its result", () => {
  const entries = [
    session("s1"),
    user("u1", "先给数据预打标"),
    assistant("a1", [
      { type: "text", text: "好的" },
      call("c1", "bash", { command: "python label.py --input data/raw" }),
    ]),
    result("r1", "c1", "labeled 120 rows"),
    user("u2", "再清洗一下"),
    assistant("a2", [call("c2", "bash", { command: "python clean.py" })]),
    result("r2", "c2", "boom", { isError: true }),
  ];
  const { steps, dropped } = traceSteps(entries);
  assert.equal(dropped, 0);
  assert.deepEqual(steps.map((step) => step.seq), [0, 1, 2, 3]);
  assert.deepEqual(steps.map((step) => step.kind), ["user", "tool", "user", "tool"]);
  assert.equal(steps[0].text, "先给数据预打标");
  assert.equal(steps[1].tool, "bash");
  assert.equal(steps[1].call_id, "c1");
  assert.deepEqual(steps[1].args, { command: "python label.py --input data/raw" });
  assert.deepEqual(steps[1].result, { is_error: false, text: "labeled 120 rows" });
  assert.equal(steps[2].text, "再清洗一下");
  assert.deepEqual(steps[3].result, { is_error: true, text: "boom" });
});

test("traceSteps records a missing result as null and ignores non-message entries", () => {
  const entries = [
    session("s1"),
    { type: "custom", id: "x1", timestamp: "2026-01-01T00:00:00Z", customType: "agentgenome.run", data: {} },
    assistant("a1", [call("c1", "read", { path: "data.csv" })]),
  ];
  const { steps } = traceSteps(entries);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].result, null);
  assert.equal(steps[0].entry_id, "a1");
});

test("traceSteps clips oversized text and arguments", () => {
  const long = "x".repeat(2500);
  const { steps } = traceSteps([
    user("u1", long),
    assistant("a1", [call("c1", "write", { path: "out.txt", command: long,
      content: long, rows: Array.from({ length: 30 }, (_, n) => `row-${n}`) })]),
    result("r1", "c1", long),
  ]);
  assert.equal(steps[0].text, `${"x".repeat(2000)}…[+500 chars]`);
  assert.deepEqual(steps[0].clipped_fields, ["text"]);
  assert.equal(steps[1].args.content, `${"x".repeat(2000)}…[+500 chars]`);
  assert.equal(steps[1].args.rows.length, 20);
  assert.equal(steps[1].args.rows[0], "row-0");
  assert.deepEqual(steps[1].clipped_fields,
    ["args.command", "args.content", "args.rows", "result.text"]);
});

test("traceSteps keeps the newest steps when the history exceeds the cap", () => {
  const entries = Array.from({ length: 12 }, (_, n) => user(`u${n}`, `step ${n}`));
  const { steps, dropped } = traceSteps(entries, 5);
  assert.equal(dropped, 7);
  assert.equal(steps.length, 5);
  assert.deepEqual(steps.map((step) => step.seq), [0, 1, 2, 3, 4]);
  assert.equal(steps[0].text, "step 7");
  assert.equal(DEFAULT_MAX_STEPS, 400);
});

test("parseSessionLog tolerates a half-written trailing line", () => {
  const text = [
    JSON.stringify(session("s1")),
    JSON.stringify(user("u1", "hi")),
    "",
    '{"type":"message","id":"trunc',
  ].join("\n");
  const entries = parseSessionLog(text);
  assert.equal(entries.length, 2);
  assert.equal(entries[1].id, "u1");
});

test("buildTrace carries the trace version and drops the raw entries", () => {
  const trace = buildTrace([session("s1"), user("u1", "hello")], { traceId: "s1", sessionName: "预打标" });
  assert.equal(trace.trace_version, 1);
  assert.equal(trace.trace_id, "s1");
  assert.equal(trace.session_id, "s1");
  assert.equal(trace.session_name, "预打标");
  assert.equal(trace.step_count, 1);
  assert.equal(trace.dropped_steps, 0);
  assert.equal(trace.steps[0].kind, "user");
  assert.equal(trace.entries, undefined);
});

test("traceFileName keeps ids filesystem safe", () => {
  assert.equal(traceFileName("run-1_abc"), "run-1_abc.json");
  assert.equal(traceFileName("a/b c:d"), "a_b_c_d.json");
  assert.equal(traceFileName(""), "trace.json");
  assert.equal(traceFileName("x".repeat(200)).length, 125);
});

test("fitTrace drops the oldest steps until the export fits one host read", () => {
  const entries = Array.from({ length: 200 }, (_, n) => user(`u${n}`, `step ${n} ${"y".repeat(200)}`));
  const trace = buildTrace(entries, { traceId: "long" });
  assert.ok(Buffer.byteLength(JSON.stringify(trace)) > 4096);
  const fitted = fitTrace(trace, 4096);
  assert.ok(Buffer.byteLength(`${JSON.stringify(fitted, null, 2)}\n`) <= 4096);
  assert.equal(fitted.step_count, fitted.steps.length);
  assert.ok(fitted.dropped_steps > 0);
  assert.equal(fitted.dropped_steps + fitted.step_count, trace.step_count);
  assert.match(fitted.steps.at(-1).text, /^step 199 /);
  assert.deepEqual(fitted.steps.map((step) => step.seq).slice(0, 3), [0, 1, 2]);
  assert.equal(MAX_TRACE_BYTES, 245760);
});

test("fitTrace leaves a small trace untouched", () => {
  const trace = buildTrace([user("u1", "hi")], { traceId: "small" });
  assert.deepEqual(fitTrace(trace), trace);
});

test("listSessions puts the live session first and enumerates recent files", async () => {
  const dir = await workspace();
  const currentFile = join(dir, "current.jsonl");
  await writeFile(join(dir, "older.jsonl"), [
    JSON.stringify(session("older", "2025-12-01T00:00:00Z")),
    JSON.stringify(user("u1", "旧会话")),
    JSON.stringify(assistant("a1", [call("c1", "bash", { command: "python train.py" })])),
  ].join("\n"));
  await writeFile(currentFile, "");
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "current",
    getSessionFile: () => currentFile,
    getSessionDir: () => dir,
    getSessionName: () => "当前会话",
    getEntries: () => [session("current"), user("u2", "预打标"), assistant("a2", [call("c2", "bash", { command: "python label.py" })])],
  } };
  const sessions = await listSessions(ctx);
  assert.equal(sessions.length, 2);
  assert.deepEqual(sessions[0], {
    trace_id: "current", session_id: "current", session_name: "当前会话",
    current: true, step_count: 2, preview: "bash",
  });
  assert.equal(sessions[1].trace_id, "older");
  assert.equal(sessions[1].current, false);
  assert.equal(sessions[1].started_at, "2025-12-01T00:00:00Z");
  assert.equal(sessions[1].step_count, 2);
  assert.equal(sessions[1].preview, "bash");
});

test("listSessions still returns the live session when the session directory is unreadable", async () => {
  const dir = await workspace();
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "current", getSessionFile: () => join(dir, "missing.jsonl"),
    getSessionDir: () => join(dir, "nope"), getSessionName: () => undefined,
    getEntries: () => [user("u1", "hi")],
  } };
  const sessions = await listSessions(ctx);
  assert.equal(sessions.length, 1);
  assert.equal(sessions[0].current, true);
  assert.equal(sessions[0].step_count, 1);
});

test("complete export retains every step and full command without fitting or clipping", async () => {
  const dir = await workspace();
  const command = `python3 scripts/convert.py --input data/raw.csv --note ${"x".repeat(2500)}`;
  const entries = Array.from({ length: 430 }, (_, n) => user(`u${n}`, `step ${n}`));
  entries.push(assistant("a1", [call("c1", "bash", { command })]));
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "current", getSessionFile: () => join(dir, "current.jsonl"),
    getSessionDir: () => dir, getSessionName: () => "full",
    getEntries: () => entries,
  } };
  assert.equal((await listSessions(ctx))[0].step_count, DEFAULT_MAX_STEPS);
  const [ref] = await exportTraces(ctx, ["current"], { complete: true });
  const trace = JSON.parse(await readFile(join(dir, ref.path), "utf8"));
  assert.equal(ref.step_count, 431);
  assert.equal(ref.dropped_steps, 0);
  assert.equal(trace.steps[0].text, "step 0");
  assert.equal(trace.steps.at(-1).args.command, command);
  assert.equal(trace.steps.at(-1).clipped_fields, undefined);
  assert.equal(trace.steps.at(-1).seq, 430);
});

test("complete export projects tool output to the lines that carry evidence", async () => {
  const dir = await workspace();
  const noise = Array.from({ length: 40000 }, (_, n) => `progress ${n} ${"x".repeat(60)}`);
  const text = `${noise.join("\n")}\nwrote 1200 rows -> public_data/out/cleaned.jsonl\n`;
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "noisy", getSessionFile: () => join(dir, "noisy.jsonl"),
    getSessionDir: () => dir, getSessionName: () => "noisy",
    getEntries: () => [
      assistant("a1", [call("c1", "bash", { command: "python clean.py" })]),
      result("r1", "c1", text),
    ],
  } };
  const [ref] = await exportTraces(ctx, ["noisy"], { complete: true });
  assert.equal(ref.step_count, 1);
  assert.equal(ref.dropped_steps, 0);
  assert.equal(ref.excerpted_steps, 1);
  const trace = JSON.parse(await readFile(join(dir, ref.path), "utf8"));
  const step = trace.steps[0];
  // The product line is what `_reported_writes` reads; dropping it would lose the node.
  assert.match(step.result.text, /-> public_data\/out\/cleaned\.jsonl/);
  assert.deepEqual(step.result.excerpt.kept_lines, 40);
  assert.equal(step.result.excerpt.total_chars, text.length);
  assert.ok(Buffer.byteLength(step.result.text) < 16 * 1024);
  assert.ok((await stat(join(dir, ref.path))).size < text.length / 50);
});

test("complete export leaves a short result byte-identical", async () => {
  const dir = await workspace();
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "small", getSessionFile: () => join(dir, "small.jsonl"),
    getSessionDir: () => dir, getSessionName: () => "small",
    getEntries: () => [
      assistant("a1", [call("c1", "bash", { command: "python clean.py" })]),
      result("r1", "c1", "wrote 12 rows -> out.jsonl"),
    ],
  } };
  const [ref] = await exportTraces(ctx, ["small"], { complete: true });
  assert.equal(ref.excerpted_steps, 0);
  const trace = JSON.parse(await readFile(join(dir, ref.path), "utf8"));
  assert.deepEqual(trace.steps[0].result, { is_error: false, text: "wrote 12 rows -> out.jsonl" });
});

test("projectText keeps a product arrow at the end of one very long line", () => {
  const text = `wrote [${"{'id': 'sample', 'text': 'value'}, ".repeat(600)}] rows -> storage/datasets/review/sft.jsonl`;
  const { text: projected, excerpt } = projectText(text);
  // The arrow sits after the dropped middle; `_reported_writes` reads it from the tail.
  assert.match(projected, /-> storage\/datasets\/review\/sft\.jsonl$/);
  assert.ok(projected.length < 5000);
  assert.equal(excerpt.total_lines, 1);
  assert.equal(excerpt.total_chars, text.length);
});

test("projectText leaves a short text untouched", () => {
  assert.deepEqual(projectText("wrote 3 rows -> out.jsonl"),
    { text: "wrote 3 rows -> out.jsonl", excerpt: null });
});

test("complete export rejects a trace whose command alone exceeds the cap", async () => {
  const dir = await workspace();
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "big", getSessionFile: () => join(dir, "big.jsonl"),
    getSessionDir: () => dir, getSessionName: () => "big",
    getEntries: () => [
      assistant("a1", [call("c1", "bash", { command: "x".repeat(MAX_COMPLETE_TRACE_BYTES + 1024) })]),
    ],
  } };
  assert.equal(MAX_COMPLETE_TRACE_BYTES, 8 * 1024 * 1024);
  await assert.rejects(() => exportTraces(ctx, ["big"], { complete: true }),
    /轨迹 big 完整导出 \d+ 字节，超过 8388608 字节（8 MiB）上限；工具输出已按产物行投影，仍然超出说明命令或参数本身过大：bash@0/);
  await assert.rejects(() => stat(join(dir, TRACE_DIRECTORY, "big.json")), { code: "ENOENT" });
});

test("exportTraces writes each trace under the workspace and returns file references", async () => {
  const dir = await workspace();
  await writeFile(join(dir, "picked.jsonl"), [
    JSON.stringify(session("picked")),
    JSON.stringify(user("u1", "预打标")),
    JSON.stringify(assistant("a1", [call("c1", "bash", { command: "python label.py" })])),
    JSON.stringify(result("r1", "c1", "ok")),
  ].join("\n"));
  const ctx = { cwd: dir, sessionManager: {
    getSessionId: () => "current", getSessionFile: () => join(dir, "current.jsonl"),
    getSessionDir: () => dir, getSessionName: () => "当前会话",
    getEntries: () => [session("current"), user("u2", "清洗")],
  } };
  const refs = await exportTraces(ctx, ["picked", "current"]);
  assert.equal(refs.length, 2);
  assert.equal(refs[0].trace_id, "picked");
  assert.equal(refs[0].path, `${TRACE_DIRECTORY}/picked.json`);
  assert.equal(refs[0].step_count, 2);
  assert.equal(refs[1].trace_id, "current");
  assert.equal(refs[1].step_count, 1);
  const written = JSON.parse(await readFile(join(dir, refs[0].path), "utf8"));
  assert.equal(written.trace_id, "picked");
  assert.equal(written.steps[1].tool, "bash");
  assert.equal((await stat(join(dir, refs[0].path))).mode & 0o777, 0o600);
});

function otherSession(dir) {
  return { cwd: dir, sessionManager: {
    getSessionId: () => "current", getSessionFile: () => join(dir, "current.jsonl"),
    getSessionDir: () => dir, getSessionName: () => "当前会话",
    getEntries: () => [session("current"), user("u2", "清洗")],
  } };
}

test("exportTraces resolves a session id that Pi stored with a timestamp prefix", async () => {
  const dir = await workspace();
  const stored = join(dir, "2026-10-26T14-34-44-946Z_task-a.jsonl");
  await writeFile(stored, [
    JSON.stringify(session("task-a")),
    JSON.stringify(user("u1", "预打标")),
    JSON.stringify(assistant("a1", [call("c1", "bash", { command: "python label.py" })])),
    JSON.stringify(result("r1", "c1", "ok")),
  ].join("\n"));
  const ctx = otherSession(dir);
  const sessions = await listSessions(ctx);
  assert.equal(sessions[1].trace_id, "task-a");
  const [ref] = await exportTraces(ctx, ["task-a"], { complete: true });
  assert.equal(ref.trace_id, "task-a");
  assert.equal(ref.step_count, 2);
  const trace = JSON.parse(await readFile(join(dir, ref.path), "utf8"));
  assert.equal(trace.steps[1].args.command, "python label.py");
});

test("exportTraces falls back to the session header when the file name carries no id", async () => {
  const dir = await workspace();
  await writeFile(join(dir, "renamed.jsonl"), [
    JSON.stringify(session("archived")),
    JSON.stringify(user("u1", "清洗")),
  ].join("\n"));
  const [ref] = await exportTraces(otherSession(dir), ["archived"]);
  assert.equal(ref.trace_id, "archived");
  assert.equal(ref.step_count, 1);
});

test("exportTraces names the available sessions instead of raising ENOENT", async () => {
  const dir = await workspace();
  await writeFile(join(dir, "2026-10-26T14-34-44-946Z_task-a.jsonl"),
    JSON.stringify(session("task-a")));
  await assert.rejects(() => exportTraces(otherSession(dir), ["typo"]),
    /找不到会话 typo：.*可用会话：2026-10-26T14-34-44-946Z_task-a/);
});
