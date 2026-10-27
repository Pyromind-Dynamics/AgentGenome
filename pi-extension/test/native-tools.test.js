import assert from 'node:assert/strict';
import test from 'node:test';
import { findPackageJSON } from 'node:module';
import { pathToFileURL } from 'node:url';
import { executeNativeTool } from '../native-tools.js';

const request = { name: 'business_write', arguments: { path: '/tmp/output' } };

test('fixed tool call uses the exact name, arguments and cancellation signal', async () => {
  const controller = new AbortController();
  let calls = 0;
  const response = await executeNativeTool({ names: () => ['business_write'], async execute(name, args, options) {
    calls++;
    assert.equal(name, request.name);
    assert.equal(args, request.arguments);
    assert.equal(options.signal, controller.signal);
    return { isError: false, result: { content: [{ type: 'text', text: 'Created' }], structuredContent: { receipt: 'actual' } } };
  } }, request, controller.signal);
  assert.equal(calls, 1);
  assert.deepEqual(response, { is_error: false, result: { receipt: 'actual' }, text: 'Created' });
});

for (const name of ['missing', 'genome_confirm_run']) {
  test(`unknown or control tool is not executed: ${name}`, async () => {
    const response = await executeNativeTool({ names: () => ['genome_confirm_run'], execute() { assert.fail('Must not execute'); } }, { ...request, name }, new AbortController().signal);
    assert.equal(response.is_error, true);
  });
}

test('pre-dispatch pipeline rejection remains a known failure', async () => {
  const response = await executeNativeTool({ names: () => [request.name], async execute() {
    return { isError: true, result: { content: [{ type: 'text', text: 'Blocked by tool_call hook' }], details: {} } };
  } }, request, new AbortController().signal);
  assert.equal(response.is_error, true);
  assert.match(response.text, /Blocked/);
});

test('throwing tools have unknown outcomes and are never invoked twice', async () => {
  let calls = 0;
  await assert.rejects(executeNativeTool({ names: () => [request.name], execute() {
    calls++;
    throw new Error('Lost response after side effect');
  } }, request, new AbortController().signal), /Lost response/);
  assert.equal(calls, 1);
});

for (const failure of ['throw', 'error_result', 'result_hook']) {
  test(`real Pi pipeline keeps side effects unknown after ${failure}`, {
    skip: !process.env.AGENTGENOME_TEST_PI_MODULE,
  }, async () => {
    const { runToolCall } = await import(new URL('./dist/agent-loop.js',
      pathToFileURL(findPackageJSON('@earendil-works/pi-agent-core',
        pathToFileURL(process.env.AGENTGENOME_TEST_PI_MODULE)))).href);
    let sideEffects = 0;
    const tool = { name: request.name, label: 'Probe', description: 'Side effect probe',
      parameters: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'] },
      async execute() {
        sideEffects++;
        if (failure === 'throw') throw new Error('Response lost after side effect');
        return { content: [{ type: 'text', text: 'Tool already ran' }], details: {},
          isError: failure === 'error_result' };
      } };
    const executor = { names: () => [tool.name], async execute(name, args, options) {
      const toolCall = { type: 'toolCall', id: 'probe', name, arguments: args };
      const outcome = await runToolCall(toolCall, { tools: [tool],
        assistantMessage: { role: 'assistant', content: [toolCall] },
        context: { messages: [], tools: [tool] }, signal: options.signal,
        ...(failure === 'result_hook' ? { afterToolCall() { throw new Error('Result hook failed'); } } : {}) });
      assert.equal(outcome.isError, true);
      assert.equal(typeof outcome.durationMs, 'number');
      return outcome;
    } };
    await assert.rejects(executeNativeTool(executor, request, new AbortController().signal), /outcome unknown/);
    assert.equal(sideEffects, 1);
  });
}

test('real Pi permission rejection before execution stays a known failure', {
  skip: !process.env.AGENTGENOME_TEST_PI_MODULE,
}, async () => {
  const { runToolCall } = await import(new URL('./dist/agent-loop.js',
    pathToFileURL(findPackageJSON('@earendil-works/pi-agent-core',
      pathToFileURL(process.env.AGENTGENOME_TEST_PI_MODULE)))).href);
  const tool = { name: request.name, label: 'Probe', description: 'Permission probe',
    parameters: { type: 'object', properties: { path: { type: 'string' } } },
    execute() { assert.fail('Blocked tool must not execute'); } };
  const response = await executeNativeTool({ names: () => [tool.name], async execute(name, args, options) {
    const toolCall = { type: 'toolCall', id: 'blocked', name, arguments: args };
    const outcome = await runToolCall(toolCall, { tools: [tool],
      assistantMessage: { role: 'assistant', content: [toolCall] },
      context: { messages: [], tools: [tool] }, signal: options.signal,
      beforeToolCall: () => ({ block: true, reason: 'Permission denied' }) });
    assert.equal(outcome.durationMs, undefined);
    return outcome;
  } }, request, new AbortController().signal);
  assert.equal(response.is_error, true);
  assert.match(response.text, /Permission denied/);
});
