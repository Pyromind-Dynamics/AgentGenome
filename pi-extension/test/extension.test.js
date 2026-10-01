import assert from "node:assert/strict";
import test from "node:test";
import { createGenomeExtension } from "../index.js";

test("plugin owns tool schemas and delegates using the original call id", async () => {
  const registered = [], calls = [], hooks = new Map();
  createGenomeExtension({ async invoke(...args) { calls.push(args); return { id: "run-1", status: "queued" }; } })({ registerTool(tool) { registered.push(tool); }, on(name, handler) { hooks.set(name, handler); } });
  assert.deepEqual(registered.map((t) => t.name), ["genome_list", "genome_get", "genome_run", "genome_prepare_revision", "genome_takeover", "genome_step_result", "genome_status", "genome_cancel"]);
  const prompt = hooks.get("before_agent_start")({ systemPrompt: "Host policy" }).systemPrompt;
  assert.ok(prompt.startsWith("Host policy"));
  assert.match(prompt, /先用 genome_list/);
  assert.match(prompt, /否则再选择 Skill/);
  assert.match(prompt, /闲聊或无需执行/);
  assert.ok(registered.every((tool) => tool.label.startsWith("历史经验")));
  const run = registered.find((t) => t.name === "genome_run");
  const args = { asset_id: "data-cleaning", version: "1.0.0", params: { data_file: "storage/data.csv" } };
  const result = await run.execute("call-1", args);
  assert.equal(calls[0][0], "run");
  assert.equal(calls[0][1], args);
  assert.equal(calls[0][2], "call-1");
  assert.equal(JSON.parse(result.content[0].text).status, "queued");
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
