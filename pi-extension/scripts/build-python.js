import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { VERSION } from "../native-client.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const npmVersion = JSON.parse(readFileSync(resolve(root, "pi-extension/package.json"), "utf8")).version;
const pythonVersion = readFileSync(resolve(root, "pyproject.toml"), "utf8").match(/^version\s*=\s*"([^"]+)"/m)?.[1];
if (npmVersion !== VERSION || pythonVersion !== VERSION) throw new Error("Plugin, client and Python release versions must match");
const result = spawnSync("uv", ["build", "--wheel", "--out-dir", "pi-extension/python"], {
  cwd: root, stdio: "inherit",
  env: { ...process.env, SOURCE_DATE_EPOCH: process.env.SOURCE_DATE_EPOCH || "315532800" },
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
