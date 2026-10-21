import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createServer } from "node:net";
import { createInterface } from "node:readline";
import { NativeClient, socketPath, VERSION, PROTOCOL } from "../native-client.js";

for (const fingerprint of [undefined, "0".repeat(64)]) {
  test(`same-version ${fingerprint ? "different" : "legacy"} service is rejected without replacing it`, {
    skip: !process.env.AGENTGENOME_TEST_PYTHON,
  }, async (t) => {
    const home = await mkdtemp(join(tmpdir(), "genome-build-check-"));
    const path = await socketPath(home);
    const calls = [], sockets = [];
    const server = createServer((socket) => {
      sockets.push(socket);
      const lines = createInterface({ input: socket });
      lines.on("line", (line) => {
        const message = JSON.parse(line);
        calls.push(message.action);
        socket.write(JSON.stringify({ id: message.id, result: {
          protocol: PROTOCOL, version: VERSION,
          ...(fingerprint ? { runtime_fingerprint: fingerprint } : {}),
        } }) + "\n");
      });
    });
    await new Promise((done) => server.listen(path, done));
    const client = new NativeClient({ home, scope: "test", cwd: home,
      python: process.env.AGENTGENOME_TEST_PYTHON });
    t.after(async () => {
      client.close();
      for (const socket of sockets) socket.destroy();
      await new Promise((done) => server.close(done));
    });
    await assert.rejects(client.request("list"), /service build mismatch/);
    assert.deepEqual(calls, ["hello"]);
    assert.equal(server.listening, true);
  });
}

// Run against an installed wheel, not source imports or a mocked transport.
test("installed Python service auto-starts for concurrent Pi clients and preserves request IDs", {
  skip: !process.env.AGENTGENOME_TEST_PYTHON,
}, async (t) => {
  const root = await mkdtemp(join(tmpdir(), "genome-client-"));
  const home = join(root, "catalog");
  const first = new NativeClient({ home, scope: "one", cwd: root, python: process.env.AGENTGENOME_TEST_PYTHON });
  const second = new NativeClient({ home, scope: "two", cwd: root, python: process.env.AGENTGENOME_TEST_PYTHON });
  t.after(() => { first.close(); second.close(); });
  await Promise.all([first.connect(), second.connect()]);
  assert.deepEqual(await second.request("list"), { assets: [] });
  await first.request("import", { source: resolve("../templates/data-cleaning") });
  await writeFile(join(root, "input.csv"), "name,age\nAda,36\nAda,36\nBob,\nCy,12\n");
  const args = { asset_id: "data-cleaning", version: "1.0.0", params: { data_file: "input.csv" } };
  const run = await first.request("validate", args, "idempotent");
  assert.equal((await first.request("validate", args, "idempotent")).id, run.id);
  await assert.rejects(second.request("status", { run_id: run.id }), /not found/);
  await first.request("start", { run_id: run.id });
  let state;
  for (let n = 0; n < 200; n++) {
    state = await first.request("status", { run_id: run.id });
    if (!["queued", "running"].includes(state.status)) break;
    await new Promise((done) => setTimeout(done, 25));
  }
  assert.equal(state.status, "succeeded");
  const report = JSON.parse(await readFile(join(home, "runs", run.id, "work/artifacts/clean-sop--summarize.out"), "utf8"));
  assert.equal(report.rows, 2);
  // A transport reconnect only queries state; it must never replay start/run.
  first.socket.destroy();
  await new Promise((done) => setTimeout(done, 20));
  assert.equal((await first.request("status", { run_id: run.id })).status, "succeeded");
  assert.equal(await socketPath(home), await socketPath(resolve(home)));
  // Leave the registry until its detached service has exited after the idle timeout.
});
