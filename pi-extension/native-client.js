import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { createConnection } from "node:net";
import { createInterface } from "node:readline";
import { mkdir, lstat, readFile, readdir, access, open, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const VERSION = "0.3.0";
export const PROTOCOL = 1;
const packageRoot = dirname(fileURLToPath(import.meta.url));
const delay = (ms) => new Promise((done) => setTimeout(done, ms));

export function defaultHome() {
  return resolve(process.env.AGENTGENOME_HOME || join(homedir(), ".agentgenome"));
}

export async function socketPath(home) {
  if (!["darwin", "linux"].includes(process.platform)) throw new Error("AgentGenome currently supports macOS/Linux");
  const directory = `/tmp/agentgenome-${process.getuid()}`;
  await mkdir(directory, { mode: 0o700, recursive: true });
  const info = await lstat(directory);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)) {
    throw new Error("AgentGenome socket directory must be owned by you with mode 0700");
  }
  // Canonicalize root paths (including symlinks) identically to Python Path.resolve().
  await mkdir(home, { recursive: true });
  const { realpath } = await import("node:fs/promises");
  const canonical = await realpath(home);
  return join(directory, createHash("sha256").update(canonical).digest("hex").slice(0, 24) + ".sock");
}

async function runtimeInfo() {
  const directory = join(packageRoot, "python");
  const wheel = (await readdir(directory)).find((name) => name === `agentgenome-${VERSION}-py3-none-any.whl`);
  if (!wheel) throw new Error("Missing bundled AgentGenome wheel; rebuild or reinstall the plugin");
  const wheelPath = join(directory, wheel);
  const requirements = join(directory, "requirements.txt");
  const digest = createHash("sha256").update(await readFile(wheelPath)).update(await readFile(requirements)).digest("hex").slice(0, 16);
  const root = join(homedir(), ".cache", "agentgenome", `${VERSION}-${digest}`);
  return { root, wheelPath, requirements, python: join(root, "bin", "python") };
}

async function command(binary, args) {
  await new Promise((done, reject) => {
    const child = spawn(binary, args, { stdio: ["ignore", "pipe", "pipe"] });
    let tail = "";
    for (const stream of [child.stdout, child.stderr]) stream.on("data", (data) => { tail = (tail + data).slice(-8000); });
    child.on("error", (error) => reject(new Error(`${binary}: ${error.message}. Install uv, then run /genome setup.`)));
    child.on("exit", (code) => code === 0 ? done() : reject(new Error(`${binary} failed (${code}): ${tail}`)));
  });
}

let setupInFlight;
export async function setupRuntime() {
  if (!setupInFlight) setupInFlight = (async () => {
    const info = await runtimeInfo();
    await mkdir(dirname(info.root), { recursive: true });
    // Do not modify a runtime already used by another window/service.
    try { await access(join(info.root, ".ready")); await access(info.python); return info.python; } catch {}
    const lock = `${info.root}.setup-lock`;
    try { await mkdir(lock); }
    catch (error) {
      if (error.code === "EEXIST") throw new Error(`Another setup may be running. Retry when it finishes; if it crashed, remove ${lock}.`);
      throw error;
    }
    try {
      await command("uv", ["venv", "--allow-existing", "--python", "3.11", info.root]);
      await command("uv", ["pip", "install", "--python", info.python, "--no-deps", "-r", info.requirements, info.wheelPath]);
      await command(info.python, ["-I", "-c", `from agentgenome.local_service import VERSION; assert VERSION == '${VERSION}'`]);
      await writeFile(join(info.root, ".ready"), VERSION);
    } finally { await rm(lock, { recursive: true }); }
    return info.python;
  })().finally(() => { setupInFlight = undefined; });
  return setupInFlight;
}

async function pythonPath() {
  if (process.env.AGENTGENOME_PYTHON) return resolve(process.env.AGENTGENOME_PYTHON);
  const info = await runtimeInfo();
  try { await access(join(info.root, ".ready")); await access(info.python); }
  catch { throw new Error("AgentGenome Python environment is not ready. Run /genome setup once (requires uv)."); }
  return info.python;
}

export class NativeClient {
  constructor({ home = defaultHome(), scope, cwd, python } = {}) {
    this.home = resolve(home); this.scope = scope; this.cwd = cwd; this.python = python;
    this.pending = new Map(); this.closed = false;
  }

  async connect() {
    if (this.closed) throw new Error("AgentGenome session has closed");
    if (this.socket && !this.socket.destroyed && this.ready) return;
    if (!this.connecting) this.connecting = this.open().finally(() => { this.connecting = undefined; });
    await this.connecting;
  }

  async open() {
    const path = await socketPath(this.home);
    try { await this.openSocket(path); }
    catch (error) {
      if (this.closed || !["ENOENT", "ECONNREFUSED"].includes(error.code)) throw error;
      const python = this.python || await pythonPath();
      if (this.closed) throw new Error("AgentGenome session has closed");
      const log = await open(join(this.home, "service.log"), "a", 0o600);
      let spawnError;
      try {
        const child = spawn(python, ["-I", "-m", "agentgenome.local_service", "--home", this.home], {
          cwd: this.home, detached: true, stdio: ["ignore", log.fd, log.fd],
          env: { ...process.env, PATH: `${dirname(python)}:${process.env.PATH || ""}` },
        });
        child.on("error", (error) => { spawnError = error; });
        child.unref();
      } finally { await log.close(); }
      let connected = false;
      for (let attempt = 0; attempt < 100 && !this.closed; attempt++) {
        if (spawnError) throw spawnError;
        await delay(100);
        try { await this.openSocket(path); connected = true; break; }
        catch (error) { if (!["ENOENT", "ECONNREFUSED"].includes(error.code)) throw error; }
      }
      if (!connected) throw new Error(`Cannot start AgentGenome; check ${join(this.home, "service.log")}. Another SDK host may own this registry.`);
    }
    try {
      await this.send("hello", { protocol: PROTOCOL, version: VERSION, scope: this.scope, cwd: this.cwd });
      this.ready = true;
    } catch (error) { this.socket?.destroy(); throw error; }
  }

  async openSocket(path) {
    if (this.closed) throw new Error("AgentGenome session has closed");
    const socket = createConnection(path);
    await new Promise((done, reject) => { socket.once("connect", done); socket.once("error", reject); });
    if (this.closed) { socket.destroy(); throw new Error("AgentGenome session has closed"); }
    this.socket = socket;
    const lines = createInterface({ input: socket });
    lines.on("line", (line) => {
      try {
        const message = JSON.parse(line), waiting = this.pending.get(message.id);
        if (!waiting) return;
        this.pending.delete(message.id); clearTimeout(waiting.timer);
        message.error ? waiting.reject(new Error(message.error)) : waiting.resolve(message.result);
      } catch { socket.destroy(new Error("Invalid AgentGenome response")); }
    });
    socket.on("error", () => {});
    socket.on("close", () => {
      lines.close();
      if (this.socket !== socket) return;
      this.ready = false;
      for (const waiting of this.pending.values()) {
        clearTimeout(waiting.timer); waiting.reject(new Error("AgentGenome connection closed; query status before retrying a run"));
      }
      this.pending.clear();
    });
  }

  send(action, args = {}, requestId) {
    return new Promise((resolve, reject) => {
      if (this.closed || !this.socket || this.socket.destroyed) {
        reject(new Error("AgentGenome connection is closed")); return;
      }
      const id = randomUUID();
      const timer = setTimeout(() => {
        this.pending.delete(id); reject(new Error(`AgentGenome ${action} timed out; query status before retrying a run`));
      }, 30000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.write(JSON.stringify({ id, action, arguments: args, request_id: requestId || id }) + "\n");
    });
  }

  async request(action, args = {}, requestId) {
    await this.connect();
    return this.send(action, args, requestId);
  }

  close() { this.closed = true; this.socket?.destroy(); }
}
