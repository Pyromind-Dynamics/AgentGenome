import assert from "node:assert/strict";
import test from "node:test";
import agentGenome, { createBridgeHost, createGenomeExtension } from "../index.js";
import { nativeHost, deliveredRunIds, progressLines } from "../native-extension.js";

const delay = (ms) => new Promise((done) => setTimeout(done, ms));

test("session restoration keeps its registry when the launch environment changes", async () => {
  const hooks = new Map(), history = [], options = [];
  let selected = "/tmp/registry-first";
  const pi = { on: (key, fn) => hooks.set(key, fn), registerCommand() {},
    appendEntry: (customType, data) => history.push({ type: "custom", customType, data }) };
  const ctx = { cwd: "/tmp/work", hasUI: false,
    sessionManager: { getEntries: () => history, getSessionFile: () => "/tmp/session" } };
  const host = nativeHost(pi, { registryHome: () => selected, clientFactory: args => {
    options.push(args);
    return { close() {}, request: async () => ({ assets: [] }) };
  } });
  hooks.get("session_start")({}, ctx);
  const first = await host.invoke("list", {}, "1", undefined, ctx);
  assert.equal(first.registry.home, selected);
  hooks.get("session_shutdown")();
  selected = "/tmp/registry-other";
  hooks.get("session_start")({}, ctx);
  const second = await host.invoke("distill", { plan_id: "existing" }, "2", undefined, ctx);
  assert.equal(second.registry.home, "/tmp/registry-first");
  assert.equal(options[1].home, "/tmp/registry-first");
  assert.equal(history.filter(e => e.customType === "agentgenome.registry").length, 1);
  hooks.get("session_shutdown")();
});

test("a failed connection does not pin an unusable registry", async () => {
  const hooks = new Map(), history = [];
  const pi = { on: (key, fn) => hooks.set(key, fn), registerCommand() {},
    appendEntry: (customType, data) => history.push({ type: "custom", customType, data }) };
  const ctx = { cwd: "/tmp", hasUI: false, sessionManager: {
    getSessionFile: () => "/tmp/session", getEntries: () => history } };
  const host = nativeHost(pi, { registryHome: () => "/tmp/unavailable",
    clientFactory: () => ({ close() {}, async request() { throw new Error("offline"); } }) });
  hooks.get("session_start")({}, ctx);
  await assert.rejects(host.invoke("list", {}, "1", undefined, ctx), /offline/);
  assert.equal(history.length, 0);
  hooks.get("session_shutdown")();
});
async function until(predicate) {
  for (let n = 0; n < 150; n++) { if (predicate()) return; await delay(20); }
  assert.fail("condition not reached");
}

function harness(t, { records = [], stages = [], history = [], delivered = true, snapshot, capture, judges = [], judge, judgeTimeout, toolRequests = [], onSend = () => {} } = {}) {
  const hooks = new Map(), calls = [], messages = [], widgets = [], aborts = [];
  const pi = {
    on: (name, callback) => hooks.set(name, callback), registerCommand() {}, registerTool() {},
    appendEntry: (customType, data) => history.push({ type: "custom", customType, data }),
    sendMessage: (message, options) => {
      onSend();
      messages.push({ message, options });
      if (delivered) history.push({ type: "custom_message", ...message });
    },
  };
  const ctx = { cwd: "/tmp", hasUI: true, abort: () => aborts.push("abort"),
    ui: { setWidget: (_key, lines) => widgets.push(lines) },
    sessionManager: { getSessionFile: () => "/tmp/pi-session", getSessionId: () => "session", getEntries: () => history } };
  const client = { close() {}, async request(action, args) {
    calls.push({ action, args });
    if (action === "prepare_run" || action === "run") {
      return { plan_id: "plan-1", status: "planned", requires_confirmation: true };
    }
    if (action === "confirm_run") {
      return {
        plan: { plan_id: args.plan_id, status: "confirmed", run_id: "r1" },
        run: { id: "r1", status: "queued" },
      };
    }
    if (action === "resume") return { id: args.run_id, status: "queued" };
    if (action === "replay_requeue") return { id: "retry-1", status: "queued" };
    if (action === "capture") return capture ?? { capture_id: "c1", run_id: "r2", reused: false };
    if (action === "snapshot") return snapshot ? snapshot() : { runs: [...records], stages, judges: judges.filter(j => !j.claimed), tools: toolRequests.filter(t => !t.done) };
    if (action === "tool_claim") {
      const tool = toolRequests.find(item => item.execution_id === args.execution_id);
      if (tool.claimed) return { claimed: false };
      tool.claimed = true;
      return { claimed: true, request: tool };
    }
    if (action === "tool_result") toolRequests.find(item => item.execution_id === args.execution_id).done = true;
    if (action === "judge_claim") {
      const pending = judges.find(j => j.request_id === args.request_id);
      pending.claimed = true;
      return { claimed: true, request: pending };
    }
    if (action === "stage_claim") {
      const stage = stages.find((s) => s.request.request_id === args.request_id);
      stage.claimed = true;
      return { claimed: true, execution_id: "round-1", request: stage.request };
    }
    if (action === "step_result") return { accepted: true };
    if (action === "claim") return { token: "lease" };
    if (action === "ack") records.splice(records.findIndex((run) => run.id === args.run_id), 1);
    return {};
  } };
  const host = nativeHost(pi, { clientFactory: () => client, ...(judge ? { judge } : {}), ...(judgeTimeout ? { judgeTimeout } : {}) });
  hooks.get("session_start")({}, ctx);
  t.after(() => hooks.get("session_shutdown")());
  return { hooks, calls, messages, widgets, aborts, host, ctx, history, records };
}

async function confirmed(h) {
  const planned = await h.host.invoke(
    "prepare_run", {}, "prepare", undefined, h.ctx);
  assert.equal(planned.run_id, undefined);
  assert.equal(h.history.some((entry) => entry.customType === "agentgenome.run"), false);
  return h.host.invoke(
    "confirm_run", { plan_id: planned.plan_id }, "confirm", undefined, h.ctx);
}

test("default entry loads standalone; SDK factory registers no native lifecycle", () => {
  const hooks = [], tools = [], commands = [];
  const pi = { events: { emit() {} }, on: (name) => hooks.push(name), registerTool: (tool) => tools.push(tool.name), registerCommand: (name) => commands.push(name) };
  agentGenome(pi);
  assert.equal(tools.length, 17);
  assert.deepEqual(commands, ["genome"]);
  assert.ok(hooks.includes("session_start"));
  assert.equal(hooks.includes("context"), false);
  hooks.length = tools.length = commands.length = 0;
  createGenomeExtension(createBridgeHost(async () => {}))(pi);
  assert.equal(tools.length, 17);
  assert.deepEqual(hooks, ["before_agent_start"]);
  assert.equal(commands.length, 0);
});

test("native stage binds receipt and completion to the delivered model round", async (t) => {
  const request = { request_id: "stage-1", run_id: "r1", node_id: "flow.llm", prompt: "Write report", expected_outputs: ["/tmp/report"] };
  const h = harness(t, { stages: [{ request, claimed: false }] });
  await confirmed(h);
  await until(() => h.messages.some((m) => m.message.customType === "agentgenome.stage"));
  const message = h.messages.find((m) => m.message.customType === "agentgenome.stage").message;
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "stop" }] });
  assert.equal(h.calls.some((c) => c.action === "stage_finish"), false);
  await assert.rejects(h.host.invoke("step_result", { request_id: "stage-1", outcome: "completed" }, "early", undefined, h.ctx), /active llm stage/);
  await h.hooks.get("message_start")({ message: { role: "custom", ...message } });
  await assert.rejects(h.host.invoke("run", {}, "nested", undefined, h.ctx), /cannot start/);
  await assert.rejects(h.host.invoke("confirm_run", { plan_id: "nested" }, "nested-confirm", undefined, h.ctx), /cannot start/);
  await assert.rejects(h.host.invoke("confirm_distill", { plan_id: "nested" }, "nested-distill", undefined, h.ctx), /cannot start/);
  await assert.rejects(h.host.invoke("replay_requeue", { run_id: "nested" }, "nested-retry", undefined, h.ctx), /cannot start/);
  await assert.rejects(h.host.invoke("step_result", { request_id: "other", outcome: "completed" }, "wrong", undefined, h.ctx), /active llm stage/);
  await h.host.invoke("step_result", { request_id: "stage-1", outcome: "completed", summary: "done" }, "receipt", undefined, h.ctx);
  const receipt = h.calls.find((c) => c.action === "step_result");
  assert.equal(receipt.args.execution_id, "round-1");
  assert.equal(h.calls.some((c) => c.action === "stage_finish"), false);
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "stop" }] });
  assert.deepEqual(h.calls.find((c) => c.action === "stage_finish").args,
    { request_id: "stage-1", execution_id: "round-1", status: "completed" });
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "stop" }] });
  assert.equal(h.calls.filter((c) => c.action === "stage_finish").length, 1);
});

test("an accepted final stage receipt survives a provider error, but not later tool calls", async (t) => {
  const request = { request_id: "stage-receipt", run_id: "r1", node_id: "flow.llm", prompt: "Write report" };
  const h = harness(t, { stages: [{ request, claimed: false }] });
  await confirmed(h);
  await until(() => h.messages.some((m) => m.message.customType === "agentgenome.stage"));
  const message = h.messages.find((m) => m.message.customType === "agentgenome.stage").message;
  await h.hooks.get("message_start")({ message: { role: "custom", ...message } });
  await h.host.invoke("step_result", { request_id: request.request_id, outcome: "completed", summary: "done" }, "receipt", undefined, h.ctx);
  await h.hooks.get("agent_end")({ messages: [
    { role: "assistant", content: [{ type: "toolCall", id: "receipt", name: "genome_step_result" }] },
    { role: "assistant", stopReason: "error", errorMessage: "Connection error." },
  ] });
  assert.equal(h.calls.find((c) => c.action === "stage_finish").args.status, "completed");

  const h2 = harness(t, { stages: [{ request, claimed: false }] });
  await confirmed(h2);
  await until(() => h2.messages.some((m) => m.message.customType === "agentgenome.stage"));
  const message2 = h2.messages.find((m) => m.message.customType === "agentgenome.stage").message;
  await h2.hooks.get("message_start")({ message: { role: "custom", ...message2 } });
  await h2.host.invoke("step_result", { request_id: request.request_id, outcome: "completed", summary: "done" }, "receipt", undefined, h2.ctx);
  await h2.hooks.get("agent_end")({ messages: [
    { role: "assistant", content: [{ type: "toolCall", id: "receipt", name: "genome_step_result" }] },
    { role: "assistant", content: [{ type: "toolCall", id: "after", name: "bash" }] },
    { role: "assistant", stopReason: "error", errorMessage: "Connection error." },
  ] });
  assert.equal(h2.calls.find((c) => c.action === "stage_finish").args.status, "failed");
});

test("stages wait for an idle session and stale callbacks cannot complete a switched session", async (t) => {
  const request = { request_id: "s1", run_id: "r1", prompt: "Task" };
  const h = harness(t, { stages: [{ request, claimed: false }] });
  let idle = false;
  h.ctx.isIdle = () => idle;
  await confirmed(h);
  await until(() => h.calls.some((c) => c.action === "snapshot"));
  assert.equal(h.messages.length, 0);
  idle = true;
  await until(() => h.messages.length === 1);
  const message = h.messages[0].message;
  h.hooks.get("session_shutdown")();
  h.hooks.get("session_start")({}, h.ctx);
  await h.hooks.get("message_start")({ message: { role: "custom", ...message } });
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "stop" }] });
  assert.equal(h.calls.some((c) => c.action === "stage_finish"), false);
});

test("a stage message whose round was cancelled is intercepted before the model acts", async (t) => {
  const request = { request_id: "s1", run_id: "r1", prompt: "Task" };
  const records = [{ id: "r1", status: "running" }];
  const h = harness(t, { records, stages: [{ request, claimed: false }] });
  await confirmed(h);
  await until(() => h.messages.some((m) => m.message.customType === "agentgenome.stage"));
  const message = h.messages.find((m) => m.message.customType === "agentgenome.stage").message;
  records[0].status = "cancelled";
  await until(() => h.widgets.some((lines) => lines.some((line) => line.includes("cancelled"))));
  assert.equal(h.aborts.length, 0);
  await h.hooks.get("message_start")({ message: { role: "custom", ...message } });
  assert.equal(h.aborts.length, 1);
  assert.equal(h.calls.some((c) => c.action === "stage_begin"), false);
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "aborted" }] });
  assert.equal(h.calls.some((c) => c.action === "stage_finish"), false);
});

test("SDK bridge owns wire conversion and propagates failures without local fallback", async () => {
  const calls = [];
  const host = createBridgeHost(async (...args) => { calls.push(args); throw new Error("SDK disconnected"); });
  await assert.rejects(host.invoke("run", { a: "b" }, "call"), /SDK disconnected/);
  assert.deepEqual(calls[0].slice(0, 2), ["workflow.invoke", { action: "run", arguments: { a: "b" }, request_id: "call" }]);
});

test("submission returns before start; agent_end starts once and success resumes with a custom message", async (t) => {
  const h = harness(t);
  assert.equal((await confirmed(h)).run.id, "r1");
  assert.equal(h.calls.some((call) => call.action === "start"), false);
  await h.hooks.get("agent_end")();
  await h.hooks.get("agent_end")();
  assert.equal(h.calls.filter((call) => call.action === "start").length, 1);
  h.records.push({ id: "r1", status: "succeeded", asset_id: "clean", version: "1" });
  await until(() => h.messages.length === 1);
  await until(() => h.calls.some((call) => call.action === "ack"));
  assert.deepEqual(h.messages[0].options, { triggerTurn: true, deliverAs: "followUp" });
  assert.match(h.messages[0].message.content, /实际产物/);
});

test("replay requeue tracks and starts the returned run after the turn", async (t) => {
  const h = harness(t);
  const result = await h.host.invoke("replay_requeue", { run_id: "failed-1" }, "retry", undefined, h.ctx);
  assert.equal(result.id, "retry-1");
  assert.equal(h.calls.some(call => call.action === "start"), false);
  await h.hooks.get("agent_end")();
  assert.deepEqual(h.calls.filter(call => call.action === "start").map(call => call.args.run_id), ["retry-1"]);
  assert.ok(h.history.some(entry => entry.customType === "agentgenome.run" && entry.data.run_id === "retry-1"));
});

test("busy agent notification is acknowledged only after custom_message is persisted", async (t) => {
  const h = harness(t, { delivered: false });
  await confirmed(h);
  h.records.push({ id: "r1", status: "failed" });
  await until(() => h.messages.length === 1);
  await until(() => h.calls.some((call) => call.action === "renew"));
  assert.equal(h.calls.some((call) => call.action === "ack"), false);
  assert.equal(h.messages.length, 1);
  h.history.push({ type: "custom_message", ...h.messages[0].message });
  await until(() => h.calls.some((call) => call.action === "ack"));
  assert.match(h.messages[0].message.content, /不要自动重复/);
});

test("interrupted stage waits for abort to settle before delivering its completion", async (t) => {
  const request = { request_id: "s1", run_id: "r1", prompt: "Task" };
  const records = [{ id: "r1", status: "running" }];
  const h = harness(t, { records, stages: [{ request, claimed: false }] });
  let idle = true;
  h.ctx.isIdle = () => idle;
  await confirmed(h);
  await until(() => h.messages.some(m => m.message.customType === "agentgenome.stage"));
  await h.hooks.get("message_start")({ message: { role: "custom", ...h.messages[0].message } });
  idle = false;
  // The service recovers an interrupted run while the model still has a tool running.
  records[0].status = "interrupted";
  await until(() => h.aborts.length === 1);
  await delay(1100);
  assert.equal(h.messages.filter(m => m.message.customType === "agentgenome.completed").length, 0);
  assert.equal(h.calls.some(c => c.action === "claim" || c.action === "renew"), false);
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "aborted" }] });
  idle = true;
  await until(() => h.calls.some(c => c.action === "ack"));
  const completions = h.messages.filter(m => m.message.customType === "agentgenome.completed");
  assert.equal(completions.length, 1);
  assert.match(completions[0].message.content, /interrupted/);
  assert.equal(h.calls.filter(c => c.action === "claim").length, 1);
  assert.equal(h.calls.some(c => c.action === "stage_finish"), false);
});

test("cancelling an active stage does not block a later explicit handoff", async (t) => {
  const request = { request_id: "s1", run_id: "r1", prompt: "Task" };
  const stages = [{ request, claimed: false }];
  const records = [{ id: "r1", status: "running" }];
  const h = harness(t, { records, stages });
  let idle = true;
  h.ctx.isIdle = () => idle;
  await confirmed(h);
  await until(() => h.messages.length === 1);
  await h.hooks.get("message_start")({ message: { role: "custom", ...h.messages[0].message } });
  idle = false;
  records[0].status = "cancelled";
  await until(() => h.aborts.length === 1);
  assert.equal(h.messages.length, 1);
  await h.hooks.get("agent_end")({ messages: [{ role: "assistant", stopReason: "aborted" }] });
  stages.push({ request: { ...request, request_id: "s2", run_id: "r2" }, claimed: false });
  await delay(1100);
  assert.equal(h.messages.length, 1);
  idle = true;
  await until(() => h.messages.length === 2);
  assert.equal(h.messages[1].message.details.request_id, "s2");
  assert.equal(h.calls.some(c => c.action === "claim"), false);
});

test("a parked run reports the human gate once and resumes the same run", async (t) => {
  const h = harness(t);
  await confirmed(h);
  const parked = { id: "r1", status: "waiting", asset_id: "prelabel-clean", version: "0.1.0",
    result: { waiting: { node: "review", question: "请到 Label Studio 复核 prelabeled.csv",
      artifact: "/runs/r1/artifacts/review.out" } } };
  assert.match(progressLines(parked).join("\n"), /Label Studio/);
  h.records.push(parked);
  await until(() => h.messages.length === 1);
  await delay(1100);
  assert.equal(h.messages.length, 1);
  assert.equal(h.calls.some((call) => call.action === "claim"), false);
  assert.equal(h.calls.some((call) => call.action === "ack"), false);
  assert.match(h.messages[0].message.content, /genome_resume/);
  assert.match(h.messages[0].message.content, /review\.out/);
  h.records[0] = { ...parked, status: "queued", result: {} };
  assert.equal((await h.host.invoke("resume", { run_id: "r1" }, "call2", undefined, h.ctx)).status, "queued");
  await h.hooks.get("agent_end")();
  assert.deepEqual(h.calls.filter((call) => call.action === "start").map((call) => call.args),
    [{ run_id: "r1" }]);
  h.records[0] = { ...parked, result: { waiting: { node: "export", question: "导出复核结果",
    artifact: "/runs/r1/artifacts/export.out" } } };
  await until(() => h.messages.length === 2);
  assert.match(h.messages[1].message.content, /export\.out/);
});

test("restoring a session does not wake the agent for an already delivered human gate", async (t) => {
  const waiting = { node: "review", question: "Review", artifact: "/runs/r1/review.out" };
  const record = { id: "r1", status: "waiting", result: { waiting } };
  // Legacy persisted notifications must also survive upgrades without replaying.
  const history = [
    { type: "custom", customType: "agentgenome.run", data: { run_id: "r1" } },
    { type: "custom_message", customType: "agentgenome.waiting", details: { run_id: "r1" },
      content: `历史经验停在人工节点：${JSON.stringify({ run_id: "r1", ...waiting })}\n请等待用户。` },
  ];
  const h = harness(t, { records: [record], history });
  await until(() => h.calls.some((call) => call.action === "snapshot"));
  assert.equal(h.messages.length, 0);
  // A distinct artifact is a new work order, even with the same node/question.
  h.records[0] = { ...record, result: { waiting: { ...waiting, artifact: "/runs/r1/new.out" } } };
  await until(() => h.messages.length === 1);
  assert.ok(h.messages[0].message.details.gate_key);
  h.hooks.get("session_shutdown")();
  h.hooks.get("session_start")({}, h.ctx);
  await delay(100);
  assert.equal(h.messages.length, 1);
});

test("capture queues its replay run and reports the sink result when it finishes", async (t) => {
  const h = harness(t);
  const result = await h.host.invoke("capture", { trace_refs: [], label: "预打标到清洗" }, "call", undefined, h.ctx);
  assert.equal(result.capture_id, "c1");
  assert.equal(h.calls.some((call) => call.action === "start"), false);
  await h.hooks.get("agent_end")();
  assert.deepEqual(h.calls.filter((call) => call.action === "start").map((call) => call.args),
    [{ run_id: "r2" }]);
  h.records.push({ id: "r2", status: "succeeded", asset_id: "prelabel-clean", version: "0.1.0",
    result: { capture: { stage: "published", report: "/home/captures/c1/report.md" } } });
  await until(() => h.messages.length === 1);
  assert.match(h.messages[0].message.content, /轨迹沉淀/);
  assert.match(h.messages[0].message.content, /report\.md/);
  await until(() => h.calls.some((call) => call.action === "ack"));
});

test("a reused capture without a queued run does not wake the agent", async (t) => {
  const h = harness(t, { capture: { capture_id: "c2", run_id: null, reused: true } });
  const result = await h.host.invoke("capture", { trace_refs: [], label: "x" }, "call", undefined, h.ctx);
  assert.equal(result.run_id, null);
  await h.hooks.get("agent_end")();
  assert.equal(h.calls.some((call) => call.action === "start"), false);
  assert.equal(h.history.some((entry) => entry.customType === "agentgenome.run"), false);
  assert.equal(h.messages.length, 0);
});

test("restart recognizes real Pi receipts and cancellation never wakes the model", async (t) => {
  const receipt = { type: "custom_message", customType: "agentgenome.completed", details: { run_id: "old" } };
  assert.ok(deliveredRunIds([receipt]).has("old"));
  const h = harness(t, { history: [receipt], records: [{ id: "old", status: "succeeded" }, { id: "cancel", status: "cancelled" }] });
  await h.host.invoke("cancel", {}, "call", undefined, h.ctx);
  await until(() => h.calls.filter((call) => call.action === "ack").length === 2);
  assert.equal(h.messages.length, 0);
});

test("session shutdown prevents a late response from notifying another session", async (t) => {
  let finish;
  const h = harness(t, { snapshot: () => new Promise((done) => { finish = done; }) });
  await confirmed(h);
  await until(() => finish);
  h.hooks.get("session_shutdown")();
  finish({ runs: [{ id: "r1", status: "succeeded" }] });
  await delay(30);
  assert.equal(h.messages.length, 0);
  assert.equal(h.calls.some((call) => call.action === "claim"), false);
});

test("stdout-free nodes still render phase and command", () => {
  assert.match(progressLines({ status: "running", progress: { node: "clean", event: "command_started", command: "python3 clean.py" } }).join("\n"), /clean.*command_started[\s\S]*python3 clean.py/);
});

test("pending completion restores without starting scripts and failed delivery retries", async (t) => {
  let attempts = 0;
  const h = harness(t, {
    history: [{ type: "custom", customType: "agentgenome.run", data: { run_id: "restored" } }],
    records: [{ id: "restored", status: "failed" }],
    onSend: () => { if (++attempts === 1) throw new Error("temporarily unavailable"); },
  });
  await until(() => h.messages.length === 1);
  assert.equal(attempts, 2);
  assert.ok(h.calls.some((call) => call.action === "release"));
  assert.equal(h.calls.some((call) => ["run", "start"].includes(call.action)), false);
});


test("model evaluation runs independently while the chat is busy", async t => {
  const judges = [{ request_id: "judge-1", run_id: "r1", criterion: "Fixed facts", evidence: "Report" }];
  const h = harness(t, { records: [{ id: "r1", status: "running" }], judges,
    judge: async (_ctx, request) => { assert.equal(request.evidence, "Report"); return { passed: false, reason: "Missing facts" }; } });
  h.ctx.isIdle = () => false;
  await confirmed(h);
  await until(() => h.calls.some(c => c.action === "judge_result"));
  assert.deepEqual(h.calls.find(c => c.action === "judge_result").args.response,
    { verdict: { passed: false, reason: "Missing facts" } });
  assert.equal(h.messages.length, 0);
  assert.equal(h.calls.some(c => c.action.startsWith("stage_")), false);
});

test("judge timeout returns a call error even if the provider never settles", async t => {
  const judges = [{ request_id: "judge-1", run_id: "r1" }];
  const h = harness(t, { records: [{ id: "r1", status: "running" }], judges,
    judge: () => new Promise(() => {}), judgeTimeout: 20 });
  await confirmed(h);
  await until(() => h.calls.some(c => c.action === "judge_result"));
  assert.match(h.calls.find(c => c.action === "judge_result").args.response.error, /timed out/);
});

test("closing a judge's session aborts its independent call without writing to another session", async t => {
  let signal;
  const h = harness(t, { records: [{ id: "r1", status: "running" }],
    judges: [{ request_id: "judge-1", run_id: "r1" }],
    judge: async (_ctx, _request, active) => { signal = active; return new Promise(() => {}); } });
  await confirmed(h);
  await until(() => signal !== undefined);
  h.hooks.get("session_shutdown")();
  h.hooks.get("session_start")({}, h.ctx);
  await delay(30);
  assert.equal(signal.aborted, true);
  assert.equal(h.calls.some(c => c.action === "judge_result"), false);
});


test("native tool dispatch uses the bound caller context once and preserves its result", async t => {
  const toolRequests = [{ execution_id: "write-1", run_id: "r1", name: "business_write", arguments: { value: 7 } }];
  const h = harness(t, { records: [{ id: "r1", status: "running" }], toolRequests });
  let executions = 0;
  h.ctx.tools = [{ name: "business_write" }];
  h.ctx.executeTool = async (name, args) => {
    executions++;
    assert.equal(name, "business_write"); assert.deepEqual(args, { value: 7 });
    return { isError: false, result: { structuredContent: { actual: 7 }, content: [] } };
  };
  await confirmed(h);
  await until(() => h.calls.some(c => c.action === "tool_result"));
  await delay(1100);
  assert.equal(executions, 1);
  assert.deepEqual(h.calls.find(c => c.action === "tool_result").args.response,
    { is_error: false, result: { actual: 7 }, text: "" });
  assert.equal(h.history.filter(e => e.customType === "agentgenome.tool").length, 1);
});

test("native tool cancellation waits for the executor's terminal outcome", async t => {
  const toolRequests = [{ execution_id: "hold-1", run_id: "r1", name: "hold", arguments: {} }];
  const h = harness(t, { records: [{ id: "r1", status: "running" }], toolRequests });
  let signal;
  h.ctx.tools = [{ name: "hold" }];
  h.ctx.executeTool = (_name, _args, options) => {
    signal = options.signal;
    return new Promise(resolve => signal.addEventListener("abort", () => resolve({
      isError: true, result: { content: [{ type: "text", text: "Stopped" }] },
    }), { once: true }));
  };
  await confirmed(h);
  await until(() => signal !== undefined);
  assert.equal(h.calls.some(c => c.action === "tool_result"), false);
  toolRequests[0].cancel_requested = true;
  await until(() => h.calls.some(c => c.action === "tool_result"));
  assert.equal(signal.aborted, true);
  assert.equal(h.calls.find(c => c.action === "tool_result").args.response.is_error, true);
});

test("an unconfirmed uncooperative tool does not keep the notification watcher polling forever", async t => {
  const toolRequests = [{ execution_id: "hung-1", run_id: "r1", name: "hung", arguments: {} }];
  const records = [{ id: "r1", status: "running" }];
  const h = harness(t, { records, toolRequests });
  let signal;
  h.ctx.tools = [{ name: "hung" }];
  h.ctx.executeTool = (_name, _args, options) => {
    signal = options.signal;
    return new Promise(() => {}); // Intentionally ignores cancellation.
  };
  await confirmed(h);
  await until(() => signal !== undefined);
  records[0].status = "interrupted";
  toolRequests[0].done = true; // Service timed out with an unknown receipt.
  await until(() => h.calls.some(c => c.action === "ack"));
  assert.equal(signal.aborted, true);
  await delay(1100);
  const polls = h.calls.filter(c => c.action === "snapshot").length;
  await delay(1100);
  assert.equal(h.calls.filter(c => c.action === "snapshot").length, polls);
  assert.equal(h.calls.some(c => c.action === "tool_result"), false);
});

test("external await shows machine waiting without a human work order or model turn", async t => {
  const records = [{id: 'r1', status: 'waiting', result: {waiting: {
    kind: 'external', node: 'flow.wait', task_id: 'task-1', deadline: 2000000000,
  }}}];
  const h = harness(t, {records});
  await confirmed(h);
  await until(() => h.widgets.some(lines => lines.some(line => line.includes('task-1'))));
  assert.equal(h.messages.length, 0);
  assert.equal(h.calls.some(call => call.action === 'resume'), false);
  records[0] = {id: 'r1', status: 'succeeded'};
  await until(() => h.messages.some(message => message.message.customType === 'agentgenome.completed'));
  assert.equal(h.messages.some(message => message.message.customType === 'agentgenome.waiting'), false);
});

test("restored await uses a freshly bound executor for its downstream tool", async t => {
  const toolRequests = [{execution_id: 'after-await', run_id: 'restored', name: 'business_write', arguments: {value: 7}}];
  const h = harness(t, {records: [{id: 'restored', status: 'running'}], toolRequests,
    history: [{type: 'custom', customType: 'agentgenome.run', data: {run_id: 'restored'}}]});
  let calls = 0;
  h.ctx.tools = [{name: 'business_write'}];
  h.ctx.executeTool = async () => {
    calls++;
    return {isError: false, result: {content: [], details: {actual: true}}};
  };
  await h.host.invoke('list', {}, 'rebind', undefined, h.ctx);
  await until(() => h.calls.some(call => call.action === 'tool_result'));
  assert.equal(calls, 1);
  assert.deepEqual(h.calls.find(call => call.action === 'tool_result').args.response.result, {actual: true});
});
