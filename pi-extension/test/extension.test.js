import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createGenomeExtension } from "../index.js";

test("plugin owns tool schemas and delegates using the original call id", async () => {
  const registered = [], calls = [], hooks = new Map();
  createGenomeExtension({ async invoke(...args) { calls.push(args); return { id: "run-1", status: "queued" }; } })({ registerTool(tool) { registered.push(tool); }, on(name, handler) { hooks.set(name, handler); } });
  assert.deepEqual(registered.map((t) => t.name), ["genome_list", "genome_trace", "genome_capture", "genome_distill", "genome_capture_status", "genome_get", "genome_run", "genome_resume", "genome_prepare_revision", "genome_takeover", "genome_step_result", "genome_status", "genome_cancel"]);
  const prompt = hooks.get("before_agent_start")({ systemPrompt: "Host policy" }).systemPrompt;
  assert.ok(prompt.startsWith("Host policy"));
  assert.match(prompt, /用户自己 storage/);
  assert.match(prompt, /先 genome_list/);
  assert.match(prompt, /applies_when/);
  assert.match(prompt, /只收完整固化/);
  assert.match(prompt, /genome_resume/);
  assert.match(prompt, /闲聊或无需执行/);
  assert.match(prompt, /沉淀一下/);
  assert.match(prompt, /genome_distill/);
  assert.match(prompt, /llm 节点的 prompt 必须把这一步的原始执行方式写进去/);
  assert.ok(registered.every((tool) => tool.label.startsWith("历史经验")));
  const distill = registered.find((t) => t.name === "genome_distill");
  assert.deepEqual(distill.parameters.required, ["steps", "label"]);
  assert.equal(distill.parameters.additionalProperties, false);
  assert.match(distill.description, /必须保留这一步的原始执行方式/);
  assert.match(distill.parameters.properties.steps.description, /必须写明该步骤原本由哪个 skill/);
  assert.equal(distill.parameters.properties.input_contract.type, "object");
  assert.ok(distill.parameters.properties.determinism.enum.includes("human-gated"));
  assert.equal(distill.parameters.properties.review_confirmed.type, "boolean");
  assert.match(distill.parameters.properties.review_reason.description, /用户确认过的边界/);
  const capture = registered.find((t) => t.name === "genome_capture");
  assert.equal(capture.parameters.properties.review_confirmed, undefined);
  assert.match(prompt, /链路范围需要用户复核/);
  const list = registered.find((t) => t.name === "genome_list");
  assert.match(list.description, /has_samples/);
  const run = registered.find((t) => t.name === "genome_run");
  const args = { asset_id: "data-cleaning", version: "1.0.0", params: { data_file: "storage/data.csv" } };
  const result = await run.execute("call-1", args);
  assert.equal(calls[0][0], "run");
  assert.equal(calls[0][1], args);
  assert.equal(calls[0][2], "call-1");
  assert.equal(JSON.parse(result.content[0].text).status, "queued");
});


test("prompt and schemas keep chains fully solidified instead of gap-shaped", async () => {
  const registered = [], hooks = new Map();
  createGenomeExtension({ async invoke() { return {}; } })({
    registerTool(tool) { registered.push(tool); }, on(name, handler) { hooks.set(name, handler); },
  });
  const prompt = hooks.get("before_agent_start")({ systemPrompt: "Host policy" }).systemPrompt;
  assert.match(prompt, /固定命令→run/);
  assert.match(prompt, /固定宿主工具调用→tool/);
  assert.match(prompt, /人工门禁→human/);
  assert.match(prompt, /agent 裁量→llm/);
  assert.match(prompt, /列一张步骤表/);
  assert.match(prompt, /重跑一次/);
  assert.match(prompt, /旁支/);
  assert.match(prompt, /只收完整固化的链路[\s\S]*\n[\s\S]*沉淀时把适用范围/);
  assert.doesNotMatch(prompt, /gaps/);
  assert.doesNotMatch(prompt, /references/);
  assert.doesNotMatch(prompt, /reference_assets/);
  assert.doesNotMatch(prompt, /input_mapping/);
  const list = registered.find((t) => t.name === "genome_list");
  assert.doesNotMatch(list.description, /has_gaps/);
  assert.doesNotMatch(list.description, /has_references/);
  const get = registered.find((t) => t.name === "genome_get");
  assert.doesNotMatch(get.description, /gaps/);
  assert.doesNotMatch(get.description, /references/);
  const distill = registered.find((t) => t.name === "genome_distill");
  assert.equal(distill.parameters.properties.gaps, undefined);
  assert.equal(distill.parameters.properties.references, undefined);
  assert.match(distill.parameters.properties.steps.description, /只收完整固化/);
  const capture = registered.find((t) => t.name === "genome_capture");
  assert.equal(capture.parameters.properties.gaps, undefined);
  assert.equal(capture.parameters.properties.references, undefined);
});

test("revision guidance and reason are delivered through the plugin tool", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) { calls.push(args); return { revision_id: "revision" }; } })({
    registerTool(tool) { registered.push(tool); }, on() {},
  });
  const tool = registered.find((t) => t.name === "genome_prepare_revision");
  assert.match(tool.description, /不要修改验收脚本/);
  assert.match(tool.description, /reason is required/);
  assert.deepEqual(tool.parameters.required, ["run_id"]);
  assert.equal(tool.parameters.properties.reason.type, "string");
  const args = { run_id: "run", reason: "results do not meet requirements" };
  await tool.execute("call", args);
  assert.equal(calls[0][1], args);
});

test("genome_trace reads the live session locally instead of calling the host", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) { calls.push(args); return {}; } })({
    registerTool(tool) { registered.push(tool); }, on() {},
  });
  const cwd = await mkdtemp(join(tmpdir(), "agentgenome-extension-"));
  const entries = [
    { type: "session", id: "session-1", timestamp: "2026-01-01T00:00:00Z", cwd },
    { type: "message", id: "u1", parentId: null, timestamp: "2026-01-01T00:00:01Z",
      message: { role: "user", content: [{ type: "text", text: "预打标" }] } },
    { type: "message", id: "a1", parentId: null, timestamp: "2026-01-01T00:00:02Z",
      message: { role: "assistant", content: [{ type: "toolCall", id: "c1", name: "bash", arguments: { command: "python label.py" } }] } },
  ];
  const ctx = { cwd, sessionManager: {
    getSessionId: () => "session-1", getSessionFile: () => join(cwd, "session-1.jsonl"),
    getSessionDir: () => cwd, getSessionName: () => undefined, getEntries: () => entries,
  } };
  const trace = registered.find((t) => t.name === "genome_trace");
  const listed = await trace.execute("call", {}, undefined, undefined, ctx);
  assert.equal(listed.details.sessions[0].current, true);
  assert.equal(listed.details.sessions[0].step_count, 2);
  assert.deepEqual(calls, []);
});

test("genome_capture exports the chosen session and hands the host file references", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) {
    calls.push(args);
    return { capture_id: "capture-1", run_id: "run-1", reused: false };
  } })({ registerTool(tool) { registered.push(tool); }, on() {} });
  const cwd = await mkdtemp(join(tmpdir(), "agentgenome-capture-"));
  const entries = [
    { type: "session", id: "session-1", timestamp: "2026-01-01T00:00:00Z", cwd },
    { type: "message", id: "a1", parentId: null, timestamp: "2026-01-01T00:00:02Z",
      message: { role: "assistant", content: [{ type: "toolCall", id: "c1", name: "bash", arguments: { command: "python label.py" } }] } },
  ];
  const ctx = { cwd, sessionManager: {
    getSessionId: () => "session-1", getSessionFile: () => join(cwd, "session-1.jsonl"),
    getSessionDir: () => cwd, getSessionName: () => undefined, getEntries: () => entries,
  } };
  const capture = registered.find((t) => t.name === "genome_capture");
  assert.deepEqual(capture.parameters.required, ["trace_ids", "label"]);
  assert.match(capture.description, /沉淀/);
  const result = await capture.execute("call-1", { trace_ids: ["session-1"], label: "预打标到清洗" },
    undefined, undefined, ctx);
  assert.equal(result.details.run_id, "run-1");
  const [action, args, callId] = calls[0];
  assert.equal(action, "capture");
  assert.equal(callId, "call-1");
  assert.equal(args.label, "预打标到清洗");
  assert.equal(args.trace_ids, undefined);
  assert.equal(args.trace_refs.length, 1);
  assert.equal(args.trace_refs[0].trace_id, "session-1");
  assert.equal(args.trace_refs[0].step_count, 1);
  const written = JSON.parse(await readFile(join(cwd, args.trace_refs[0].path), "utf8"));
  assert.equal(written.trace_version, 1);
  assert.equal(written.steps.length, 1);
});

test("genome_distill forwards the normalized chain without touching the session", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) {
    calls.push(args);
    return { capture_id: "capture-2", run_id: "run-2", distilled: { trace_id: "distilled-abc" } };
  } })({ registerTool(tool) { registered.push(tool); }, on() {} });
  const distill = registered.find((t) => t.name === "genome_distill");
  assert.match(distill.description, /内联代码/);
  assert.match(distill.description, /脚本必须已经存在/);
  const args = { label: "预打标", final_artifact: "out/sample.jsonl",
    steps: [{ run: "python3 scripts/prep.py data/manifest.jsonl > out/sample.jsonl", from: "s1:23" }] };
  const result = await distill.execute("call-2", args);
  assert.equal(result.details.run_id, "run-2");
  assert.deepEqual(calls, [["distill", args, "call-2", undefined, undefined]]);
});

test("genome_distill exports the source sessions so the chain can be checked", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) {
    calls.push(args);
    return { capture_id: "capture-3", run_id: "run-3" };
  } })({ registerTool(tool) { registered.push(tool); }, on() {} });
  const cwd = await mkdtemp(join(tmpdir(), "agentgenome-distill-"));
  const entries = [
    { type: "session", id: "session-1", timestamp: "2026-01-01T00:00:00Z", cwd },
    { type: "message", id: "a1", parentId: null, timestamp: "2026-01-01T00:00:02Z",
      message: { role: "assistant", content: [{ type: "toolCall", id: "c1", name: "bash", arguments: { command: "python label.py" } }] } },
  ];
  const ctx = { cwd, sessionManager: {
    getSessionId: () => "session-1", getSessionFile: () => join(cwd, "session-1.jsonl"),
    getSessionDir: () => cwd, getSessionName: () => undefined, getEntries: () => entries,
  } };
  const distill = registered.find((t) => t.name === "genome_distill");
  const result = await distill.execute("call-3", {
    label: "预打标", source_traces: ["session-1"],
    steps: [{ run: "python3 scripts/prep.py {input_jsonl} > out/sample.jsonl" }],
    params: { input_jsonl: "data/manifest.jsonl" },
  }, undefined, undefined, ctx);
  assert.equal(result.details.run_id, "run-3");
  const [action, args, callId] = calls[0];
  assert.equal(action, "distill");
  assert.equal(callId, "call-3");
  assert.deepEqual(args.source_traces, ["session-1"]);
  assert.deepEqual(args.params, { input_jsonl: "data/manifest.jsonl" });
  assert.equal(args.trace_refs.length, 1);
  assert.equal(args.trace_refs[0].trace_id, "session-1");
  assert.equal(args.trace_refs[0].step_count, 1);
});

test("genome_capture refuses to guess a session when none is given", async () => {
  const registered = [];
  createGenomeExtension({ async invoke() { return {}; } })({
    registerTool(tool) { registered.push(tool); }, on() {},
  });
  const ctx = { cwd: await mkdtemp(join(tmpdir(), "agentgenome-empty-")), sessionManager: {
    getSessionId: () => "session-1", getSessionFile: () => undefined,
    getSessionDir: () => "/nonexistent", getSessionName: () => undefined, getEntries: () => [],
  } };
  const capture = registered.find((t) => t.name === "genome_capture");
  await assert.rejects(
    () => capture.execute("call", { trace_ids: [], label: "x" }, undefined, undefined, ctx),
    /needs trace ids/);
});
