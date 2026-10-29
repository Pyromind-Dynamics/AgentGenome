import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createGenomeExtension } from "../index.js";

test("plugin owns tool schemas and delegates using the original call id", async () => {
  const registered = [], calls = [], hooks = new Map();
  createGenomeExtension({ async invoke(...args) { calls.push(args); return { id: "run-1", status: "queued" }; } })({ registerTool(tool) { registered.push(tool); }, on(name, handler) { hooks.set(name, handler); } });
  assert.deepEqual(registered.map((t) => t.name), ["genome_list", "genome_trace", "genome_capture", "genome_distill", "genome_confirm_distill", "genome_capture_status", "genome_get", "genome_prepare_run", "genome_confirm_run", "genome_run", "genome_resume", "genome_replay_requeue", "genome_prepare_revision", "genome_takeover", "genome_step_result", "genome_status", "genome_cancel"]);
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
  assert.match(prompt, /genome_confirm_distill/);
  assert.match(prompt, /不要为了回忆重读整段会话/);
  assert.match(prompt, /重跑历史命令/);
  assert.match(prompt, /第一次调用必须省略 steps/);
  assert.match(prompt, /机械生成候选链路/);
  assert.match(prompt, /ambiguity/);
  assert.match(prompt, /llm 节点的 prompt 必须写明原会话使用的 skill、命令形态和输入输出路径/);
  assert.ok(prompt.length < 6000);
  assert.ok(registered.every((tool) => tool.label.startsWith("历史经验")));
  const distill = registered.find((t) => t.name === "genome_distill");
  assert.deepEqual(distill.parameters.required, ["label", "source_traces"]);
  assert.equal(distill.parameters.additionalProperties, false);
  assert.match(distill.description, /First call it .* no steps/);
  assert.match(distill.description, /mechanically builds a candidate chain/);
  assert.match(distill.parameters.properties.steps.description, /第一次沉淀不要提交 steps/);
  assert.equal(distill.parameters.properties.steps.items.properties.await.type, "string");
  assert.match(distill.parameters.properties.steps.description, /前序异步工具的回执路径/);
  assert.equal(distill.parameters.properties.steps.items.properties.label, undefined);
  assert.equal(distill.parameters.properties.input_contract.type, "object");
  assert.match(distill.parameters.properties.input_contract.description, /type.*number/);
  assert.ok(distill.parameters.properties.determinism.enum.includes("human-gated"));
  assert.equal(distill.parameters.properties.review_confirmed.type, "boolean");
  assert.match(distill.parameters.properties.review_reason.description, /用户确认过的边界/);
  const capture = registered.find((t) => t.name === "genome_capture");
  assert.equal(capture.parameters.properties.review_confirmed, undefined);
  const confirmDistill = registered.find((t) => t.name === "genome_confirm_distill");
  assert.deepEqual(confirmDistill.parameters.required, ["plan_id"]);
  assert.match(confirmDistill.description, /creates the capture/);
  assert.match(confirmDistill.description, /starts the replay/);
  assert.match(confirmDistill.description, /published only after that replay succeeds/);
  assert.match(prompt, /链路范围需要用户复核/);
  const list = registered.find((t) => t.name === "genome_list");
  assert.match(list.description, /has_samples/);
  const prepare = registered.find((t) => t.name === "genome_prepare_run");
  const confirm = registered.find((t) => t.name === "genome_confirm_run");
  assert.match(prepare.description, /without starting a run/);
  assert.match(prepare.description, /wait for explicit confirmation/);
  assert.deepEqual(confirm.parameters.required, ["plan_id"]);
  assert.match(confirm.description, /Never call this before the user confirms/);
  const run = registered.find((t) => t.name === "genome_run");
  assert.match(run.description, /Compatibility alias for genome_prepare_run/);
  assert.match(run.description, /does not start execution/);
  assert.match(run.description, /returns an execution plan/);
  assert.match(run.description, /With revision_id it returns a run ID immediately/);
  const args = { asset_id: "data-cleaning", version: "1.0.0", params: { data_file: "storage/data.csv" } };
  const result = await run.execute("call-1", args);
  assert.equal(calls[0][0], "run");
  assert.equal(calls[0][1], args);
  assert.equal(calls[0][2], "call-1");
  assert.equal(JSON.parse(result.content[0].text).status, "queued");
});


test("guidance teaches keep, async wait steps, batch validation and the data-flow scope rule", async () => {
  const registered = [], hooks = new Map();
  createGenomeExtension({ async invoke() { return {}; } })({
    registerTool(tool) { registered.push(tool); }, on(name, handler) { hooks.set(name, handler); },
  });
  const prompt = hooks.get("before_agent_start")({ systemPrompt: "Host policy" }).systemPrompt;
  assert.match(prompt, /keep: true/);
  assert.match(prompt, /本地试跑小样/);
  assert.match(prompt, /试跑与正式执行共用脚本不算相连/);
  assert.match(prompt, /轮询脚本/);
  assert.match(prompt, /一次全列出来/);
  assert.match(prompt, /机械生成候选链路/);
  assert.match(prompt, /不发布、不回放的临时包预检/);
  assert.match(prompt, /genome_confirm_distill 只做一次机械 preflight/);
  assert.match(prompt, /预检失败时不会创建 capture/);
  assert.match(prompt, /本回合必须修正并立即重新调用对应工具/);
  assert.match(prompt, /不能只说「准备重新提交」就结束/);
  assert.match(prompt, /只有需要用户决策时才停/);
  assert.match(prompt, /链路范围按数据流判断/);
  assert.match(prompt, /范围确实有歧义/);
  assert.match(prompt, /先用一句具体问题让用户选两个范围/);
  assert.match(prompt, /长链路/);
  assert.doesNotMatch(prompt, /先把步骤表原样拿给用户确认/);
  const distill = registered.find((t) => t.name === "genome_distill");
  const keep = distill.parameters.properties.steps.items.properties.keep;
  assert.equal(keep.type, "boolean");
  assert.match(keep.description, /不加 keep/);
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
  assert.match(prompt, /只读取已有 trace/);
  assert.match(prompt, /机械生成候选链路/);
  assert.match(prompt, /旁支/);
  assert.match(prompt, /整条链路必须完整固化[\s\S]*\n[\s\S]*同时声明 applies_when/);
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

test("human gates hand the user's export back and replay failures get a requeue path", async () => {
  const registered = [], calls = [], hooks = new Map();
  createGenomeExtension({ async invoke(...args) { calls.push(args); return { status: "queued" }; } })({
    registerTool(tool) { registered.push(tool); }, on(name, handler) { hooks.set(name, handler); },
  });
  const prompt = hooks.get("before_agent_start")({ systemPrompt: "Host policy" }).systemPrompt;
  assert.match(prompt, /genome_replay_requeue/);
  assert.match(prompt, /宿主\/基础设施失败用 genome_replay_requeue/);
  assert.match(prompt, /图内脚本错误则修好后重新 genome_distill/);
  assert.match(prompt, /waiting、queued、running 不算失败/);
  assert.match(prompt, /open_url/);
  assert.match(prompt, /图内 artifact 只作为回执位置/);
  const resume = registered.find((t) => t.name === "genome_resume");
  assert.deepEqual(resume.parameters.required, ["run_id"]);
  assert.equal(resume.parameters.properties.artifact.type, "string");
  assert.match(resume.parameters.properties.artifact.description, /落到图声明的 artifact/);
  assert.match(resume.description, /artifact/);
  const requeue = registered.find((t) => t.name === "genome_replay_requeue");
  assert.deepEqual(requeue.parameters.required, ["run_id"]);
  assert.match(requeue.description, /仅用于失败与图里的脚本无关的情况/);
  const args = { run_id: "run-1", artifact: "storage/export/label_studio_export.json" };
  const result = await requeue.execute("call-1", args);
  assert.equal(calls[0][0], "replay_requeue");
  assert.equal(calls[0][1], args);
  assert.equal(JSON.parse(result.content[0].text).status, "queued");
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
    return { plan_id: "plan-2", digest: "digest-2", status: "planned",
      distilled: { trace_id: "distilled-abc" } };
  } })({ registerTool(tool) { registered.push(tool); }, on() {} });
  const distill = registered.find((t) => t.name === "genome_distill");
  assert.match(distill.description, /structural distillation plan/);
  assert.match(distill.description, /may read scripts and compile a temporary graph/);
  assert.match(distill.description, /one complete preflight/);
  assert.match(distill.description, /queues only the approved bounded sample\/subset replay/);
  assert.match(distill.description, /Publication waits for replay success/);
  const args = { label: "预打标", final_artifact: "out/sample.jsonl" };
  const result = await distill.execute("call-2", args);
  assert.equal(result.details.plan_id, "plan-2");
  assert.equal(result.details.status, "planned");
  assert.deepEqual(calls, [["distill", args, "call-2", undefined, undefined]]);
});

test("genome_distill exports the source sessions so the chain can be checked", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) {
    calls.push(args);
    return { plan_id: "plan-3", digest: "digest-3", status: "planned" };
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
    params: { input_jsonl: "data/manifest.jsonl" },
  }, undefined, undefined, ctx);
  assert.equal(result.details.status, "planned");
  const [action, args, callId] = calls[0];
  assert.equal(action, "distill");
  assert.equal(callId, "call-3");
  assert.deepEqual(args.source_traces, ["session-1"]);
  assert.equal(args.steps, undefined);
  assert.deepEqual(args.params, { input_jsonl: "data/manifest.jsonl" });
  assert.equal(args.trace_refs.length, 1);
  assert.equal(args.trace_refs[0].trace_id, "session-1");
  assert.equal(args.trace_refs[0].step_count, 1);
});

test("capture and distill export full source sessions while trace listing stays bounded", async () => {
  const registered = [], calls = [];
  createGenomeExtension({ async invoke(...args) { calls.push(args); return { capture_id: "saved" }; } })({
    registerTool(tool) { registered.push(tool); }, on() {},
  });
  const cwd = await mkdtemp(join(tmpdir(), "agentgenome-complete-"));
  const command = `python3 scripts/process.py --input data/raw.csv --note ${"x".repeat(2500)}`;
  const entries = Array.from({ length: 430 }, (_, n) => ({
    type: "message", id: `u${n}`, message: { role: "user", content: [{ type: "text", text: `step ${n}` }] },
  }));
  entries.push({ type: "message", id: "a1", message: { role: "assistant",
    content: [{ type: "toolCall", id: "c1", name: "bash", arguments: { command } }] } });
  const ctx = { cwd, sessionManager: {
    getSessionId: () => "session-1", getSessionFile: () => join(cwd, "session-1.jsonl"),
    getSessionDir: () => cwd, getSessionName: () => "session",
    getEntries: () => entries,
  } };
  const registeredTool = (name) => registered.find((tool) => tool.name === name);
  const listed = await registeredTool("genome_trace").execute("list", {}, undefined, undefined, ctx);
  assert.equal(listed.details.sessions[0].step_count, 400);
  await registeredTool("genome_capture").execute("capture", {
    label: "complete", trace_ids: ["session-1"],
  }, undefined, undefined, ctx);
  await registeredTool("genome_distill").execute("distill", {
    label: "complete", source_traces: ["session-1"],
  }, undefined, undefined, ctx);
  assert.deepEqual(calls.map(([action]) => action), ["capture", "distill"]);
  for (const [, args] of calls) {
    assert.equal(args.trace_refs[0].step_count, 431);
    assert.equal(args.trace_refs[0].dropped_steps, 0);
    const trace = JSON.parse(await readFile(join(cwd, args.trace_refs[0].path), "utf8"));
    assert.equal(trace.steps[0].text, "step 0");
    assert.equal(trace.steps.at(-1).args.command, command);
    assert.equal(trace.steps.at(-1).clipped_fields, undefined);
  }
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
