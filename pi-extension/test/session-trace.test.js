import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DEFAULT_MAX_STEPS,
  MAX_TRACE_BYTES,
  TRACE_DIRECTORY,
  buildTrace,
  exportTraces,
  fitTrace,
  listSessions,
  parseSessionLog,
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
    assistant("a1", [call("c1", "write", { path: "out.txt", content: long, rows: Array.from({ length: 30 }, (_, n) => `row-${n}`) })]),
  ]);
  assert.equal(steps[0].text, `${"x".repeat(2000)}…[+500 chars]`);
  assert.equal(steps[1].args.content, `${"x".repeat(2000)}…[+500 chars]`);
  assert.equal(steps[1].args.rows.length, 20);
  assert.equal(steps[1].args.rows[0], "row-0");
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
