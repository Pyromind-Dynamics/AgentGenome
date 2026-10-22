import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { VERSION } from "../native-client.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const npmVersion = JSON.parse(readFileSync(resolve(root, "pi-extension/package.json"), "utf8")).version;
const pythonVersion = readFileSync(resolve(root, "pyproject.toml"), "utf8").match(/^version\s*=\s*"([^"]+)"/m)?.[1];
const serviceVersion = readFileSync(resolve(root, "agentgenome/local_service.py"), "utf8").match(/^VERSION\s*=\s*"([^"]+)"/m)?.[1];
if (npmVersion !== VERSION || pythonVersion !== VERSION || serviceVersion !== VERSION) throw new Error("Plugin, client and Python release versions must match");
// The npm tarball ships exactly one wheel; stale releases from earlier packs
// must not ride along, or npm would carry a wheel whose version nobody can use.
const wheelDir = resolve(root, "pi-extension/python");
const wheelName = `agentgenome-${VERSION}-py3-none-any.whl`;
if (existsSync(wheelDir)) {
  for (const name of readdirSync(wheelDir)) {
    if (name.endsWith(".whl") && name !== wheelName) rmSync(resolve(wheelDir, name));
  }
}
// setuptools reuses build/ between runs; a stale <pkg>-<old-version>.dist-info
// left there would be packed into the next wheel alongside the new one.
rmSync(resolve(root, "build"), { recursive: true, force: true });
const result = spawnSync("uv", ["build", "--wheel", "--out-dir", "pi-extension/python"], {
  cwd: root, stdio: "inherit",
  env: { ...process.env, SOURCE_DATE_EPOCH: process.env.SOURCE_DATE_EPOCH || "315532800" },
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
