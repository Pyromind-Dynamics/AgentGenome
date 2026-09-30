import assert from "node:assert/strict";
import test from "node:test";
import { EventEmitter } from "node:events";
import agentGenome, { provideGenomeHost } from "../index.js";

function surface() {
  const emitter = new EventEmitter(), tools = [], hooks = [], commands = [];
  const events = {
    emit: (name, data) => emitter.emit(name, data),
    on: (name, handler) => { emitter.on(name, handler); return () => emitter.off(name, handler); },
  };
  return { tools, hooks, commands, events, pi: { events,
    on: (name) => hooks.push(name), registerTool: (tool) => tools.push(tool),
    registerCommand: (name) => commands.push(name) } };
}

test("same default entry discovers a host without native execution or notifications", async () => {
  const h = surface(), requests = [];
  const binding = provideGenomeHost(h.events, async (...args) => { requests.push(args); return { id: "hosted", status: "queued" }; });
  agentGenome(h.pi);
  assert.equal(binding.discovered, true);
  assert.equal(h.tools.length, 5);
  assert.deepEqual(h.hooks, ["before_agent_start"]);
  assert.deepEqual(h.commands, []);
  await h.tools.find((tool) => tool.name === "genome_run").execute("call", { params: {} });
  assert.deepEqual(requests[0].slice(0, 2), ["workflow.invoke", { action: "run", arguments: { params: {} }, request_id: "call" }]);
  binding.dispose();
});

test("broken host fails instead of selecting local execution; buses do not leak across sessions", async () => {
  const a = surface(), b = surface();
  provideGenomeHost(a.events, async () => { throw new Error("SDK unavailable"); });
  agentGenome(a.pi);
  agentGenome(b.pi);
  await assert.rejects(a.tools[0].execute("call", {}), /SDK unavailable/);
  assert.equal(a.hooks.includes("session_start"), false);
  assert.equal(b.hooks.includes("session_start"), true);
});

test("ambiguous hosts stop extension loading", () => {
  const h = surface();
  provideGenomeHost(h.events, async () => {});
  provideGenomeHost(h.events, async () => {});
  assert.throws(() => agentGenome(h.pi), /Multiple/);
  assert.equal(h.tools.length, 0);
});
