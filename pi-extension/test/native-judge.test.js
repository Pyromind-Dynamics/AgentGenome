import assert from "node:assert/strict";
import test from "node:test";
import { modelJudge, supportsModelJudge } from "../native-judge.js";

for (const passed of [true, false]) {
  test(`judge uses fresh tool-free model context and preserves verdict ${passed}`, async () => {
    const controller = new AbortController();
    let invocation;
    const ctx = { model: { id: "test" }, modelRegistry: { streamSimple(model, context, options) {
      invocation = { model, context, options };
      return { result: async () => ({ stopReason: "stop", content: [{ type: "text", text: JSON.stringify({ passed, reason: "Fact comparison" }) }] }) };
    } } };
    assert.equal(supportsModelJudge(ctx), true);
    assert.deepEqual(await modelJudge(ctx, { criterion: "Fixed standard", evidence: "Report", chat_history: "MUST_NOT_SEND" }, controller.signal), { passed, reason: "Fact comparison" });
    assert.equal(invocation.context.messages.length, 1);
    assert.deepEqual(JSON.parse(invocation.context.messages[0].content), { criterion: "Fixed standard", evidence: "Report" });
    assert.equal(invocation.context.tools, undefined);
    assert.equal(invocation.options.signal, controller.signal);
  });
}

for (const [stopReason, text] of [["error", ""], ["aborted", ""], ["length", "{}"], ["stop", '{"passed":"true","reason":"bad"}'], ["stop", '{"passed":true,"reason":""}'], ["stop", "not JSON"]]) {
  test(`invalid judge response fails closed: ${stopReason}/${text}`, async () => {
    const ctx = { model: {}, modelRegistry: { streamSimple() {
      return { result: async () => ({ stopReason, content: [{ type: "text", text }] }) };
    } } };
    await assert.rejects(modelJudge(ctx, { criterion: "Fixed", evidence: "Report" }, new AbortController().signal));
  });
}
