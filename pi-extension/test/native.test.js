import assert from "node:assert/strict";
import test from "node:test";
import agentGenome, { createBridgeHost, createGenomeExtension } from "../index.js";
import { nativeHost, deliveredRunIds, progressLines } from "../native-extension.js";

const delay = (ms) => new Promise((done) => setTimeout(done, ms));
async function until(predicate) {
  for (let n = 0; n < 150; n++) { if (predicate()) return; await delay(20); }
  assert.fail("condition not reached");
}

function harness(t, { records = [], history = [], delivered = true, snapshot, onSend = () => {} } = {}) {
  const hooks = new Map(), calls = [], messages = [], widgets = [];
  const pi = {
    on: (name, callback) => hooks.set(name, callback), registerCommand() {}, registerTool() {},
    appendEntry: (customType, data) => history.push({ type: "custom", customType, data }),
    sendMessage: (message, options) => {
      onSend();
      messages.push({ message, options });
      if (delivered) history.push({ type: "custom_message", ...message });
    },
  };
  const ctx = { cwd: "/tmp", hasUI: true, ui: { setWidget: (_key, lines) => widgets.push(lines) },
    sessionManager: { getSessionFile: () => "/tmp/pi-session", getSessionId: () => "session", getEntries: () => history } };
  const client = { close() {}, async request(action, args) {
    calls.push({ action, args });
    if (action === "run") return { id: "r1", status: "queued" };
    if (action === "snapshot") return snapshot ? snapshot() : { runs: [...records] };
    if (action === "claim") return { token: "lease" };
    if (action === "ack") records.splice(records.findIndex((run) => run.id === args.run_id), 1);
    return {};
  } };
  const host = nativeHost(pi, { clientFactory: () => client });
  hooks.get("session_start")({}, ctx);
  t.after(() => hooks.get("session_shutdown")());
  return { hooks, calls, messages, widgets, host, ctx, history, records };
}

test("default entry loads standalone; SDK factory registers no native lifecycle", () => {
  const hooks = [], tools = [], commands = [];
  const pi = { events: { emit() {} }, on: (name) => hooks.push(name), registerTool: (tool) => tools.push(tool.name), registerCommand: (name) => commands.push(name) };
  agentGenome(pi);
  assert.equal(tools.length, 5);
  assert.deepEqual(commands, ["genome"]);
  assert.ok(hooks.includes("session_start"));
  hooks.length = tools.length = commands.length = 0;
  createGenomeExtension(createBridgeHost(async () => {}))(pi);
  assert.equal(tools.length, 5);
  assert.deepEqual(hooks, ["before_agent_start"]);
  assert.equal(commands.length, 0);
});

test("SDK bridge owns wire conversion and propagates failures without local fallback", async () => {
  const calls = [];
  const host = createBridgeHost(async (...args) => { calls.push(args); throw new Error("SDK disconnected"); });
  await assert.rejects(host.invoke("run", { a: "b" }, "call"), /SDK disconnected/);
  assert.deepEqual(calls[0].slice(0, 2), ["workflow.invoke", { action: "run", arguments: { a: "b" }, request_id: "call" }]);
});

test("submission returns before start; agent_end starts once and success resumes with a custom message", async (t) => {
  const h = harness(t);
  assert.equal((await h.host.invoke("run", {}, "call", undefined, h.ctx)).id, "r1");
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

test("busy agent notification is acknowledged only after custom_message is persisted", async (t) => {
  const h = harness(t, { delivered: false });
  await h.host.invoke("run", {}, "call", undefined, h.ctx);
  h.records.push({ id: "r1", status: "failed" });
  await until(() => h.messages.length === 1);
  await until(() => h.calls.some((call) => call.action === "renew"));
  assert.equal(h.calls.some((call) => call.action === "ack"), false);
  assert.equal(h.messages.length, 1);
  h.history.push({ type: "custom_message", ...h.messages[0].message });
  await until(() => h.calls.some((call) => call.action === "ack"));
  assert.match(h.messages[0].message.content, /不要自动重复/);
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
  await h.host.invoke("run", {}, "call", undefined, h.ctx);
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
