import assert from "node:assert/strict";
import test from "node:test";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

test("standard Pi loader discovers the installed plugin without extensionFactories or SDK", {
  skip: !process.env.AGENTGENOME_TEST_PI_MODULE,
}, async (t) => {
  const { DefaultResourceLoader, SettingsManager } = await import(pathToFileURL(process.env.AGENTGENOME_TEST_PI_MODULE).href);
  const root = await mkdtemp(join(tmpdir(), "genome-pi-loader-"));
  const extensionPath = resolve(process.env.AGENTGENOME_TEST_EXTENSION_PATH || ".");
  await access(join(extensionPath, "python/agentgenome-0.2.0-py3-none-any.whl"));
  await access(join(extensionPath, "python/requirements.txt"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const loader = new DefaultResourceLoader({ cwd: root, agentDir: join(root, "agent"),
    settingsManager: SettingsManager.inMemory({}),
    additionalExtensionPaths: [extensionPath],
    noSkills: true, noPromptTemplates: true, noThemes: true });
  await loader.reload();
  const result = loader.getExtensions();
  assert.deepEqual(result.errors, []);
  assert.equal(result.extensions.length, 1);
  const extension = result.extensions[0];
  assert.deepEqual([...extension.tools.keys()], ["genome_list", "genome_get", "genome_run", "genome_status", "genome_cancel"]);
  assert.ok(extension.commands.has("genome"));
  assert.ok(extension.handlers.has("session_start"));
  assert.ok(extension.handlers.has("session_shutdown"));
});
