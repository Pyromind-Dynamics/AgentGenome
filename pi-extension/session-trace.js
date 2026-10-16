// Trace export: clip Pi session history into compact JSON for AgentGenome capture.
// Uses only ctx.sessionManager and Node built-ins, so every Pi host works the same way.

import { mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, join } from "node:path";

export const TRACE_VERSION = 1;
export const TRACE_DIRECTORY = "public_data/agentgenome-traces";
export const DEFAULT_MAX_STEPS = 400;
// Host file APIs cap a single read (256 KiB on the SDK bridge), so the export must fit.
export const MAX_TRACE_BYTES = 240 * 1024;
const TEXT_LIMIT = 2000;
const ARRAY_LIMIT = 20;
const SESSION_LIMIT = 20;

function clip(value, limit = TEXT_LIMIT) {
  if (typeof value !== "string" || value.length <= limit) return value;
  return `${value.slice(0, limit)}…[+${value.length - limit} chars]`;
}

function textOf(content) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n");
}

function clipArguments(args) {
  if (!args || typeof args !== "object" || Array.isArray(args)) return args;
  const clipped = {};
  for (const [key, value] of Object.entries(args)) {
    if (typeof value === "string") clipped[key] = clip(value);
    else if (Array.isArray(value)) clipped[key] = value.slice(0, ARRAY_LIMIT).map((item) => clip(item));
    else clipped[key] = value;
  }
  return clipped;
}

// Pair assistant tool calls with their results; user turns stay in the sequence so the
// captured graph can tell an instruction from an action.
export function traceSteps(entries, maxSteps = DEFAULT_MAX_STEPS) {
  const results = new Map();
  for (const entry of entries) {
    const message = entry?.type === "message" ? entry.message : null;
    if (message?.role === "toolResult" && typeof message.toolCallId === "string") {
      results.set(message.toolCallId, message);
    }
  }
  const steps = [];
  for (const entry of entries) {
    if (entry?.type !== "message") continue;
    const message = entry.message;
    if (message?.role === "user") {
      steps.push({ kind: "user", entry_id: entry.id, timestamp: entry.timestamp,
        text: clip(textOf(message.content)) });
      continue;
    }
    if (message?.role !== "assistant") continue;
    for (const block of message.content ?? []) {
      if (block?.type !== "toolCall") continue;
      const result = results.get(block.id);
      steps.push({ kind: "tool", entry_id: entry.id, timestamp: entry.timestamp,
        tool: block.name, call_id: block.id, args: clipArguments(block.arguments),
        result: result
          ? { is_error: result.isError === true, text: clip(textOf(result.content)) }
          : null });
    }
  }
  const dropped = Math.max(0, steps.length - maxSteps);
  return { steps: steps.slice(dropped).map((step, seq) => ({ seq, ...step })), dropped };
}

export function parseSessionLog(text) {
  const entries = [];
  for (const line of String(text).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      entries.push(JSON.parse(trimmed));
    } catch {
      // Pi can leave a half-written trailing line; one bad line must not void the trace.
    }
  }
  return entries;
}

export function buildTrace(entries, { traceId, sessionId, sessionName, maxSteps } = {}) {
  const { steps, dropped } = traceSteps(entries, maxSteps);
  return {
    trace_version: TRACE_VERSION,
    trace_id: String(traceId ?? "trace"),
    session_id: sessionId === undefined ? String(traceId ?? "trace") : sessionId,
    session_name: sessionName,
    step_count: steps.length,
    dropped_steps: dropped,
    steps,
  };
}

export function traceFileName(traceId) {
  const safe = String(traceId).replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120);
  return `${safe || "trace"}.json`;
}

function serialized(trace) {
  return `${JSON.stringify(trace, null, 2)}\n`;
}

// Keep the newest steps so the export stays readable through one host read.
export function fitTrace(trace, maxBytes = MAX_TRACE_BYTES) {
  let steps = trace.steps;
  let dropped = trace.dropped_steps;
  while (steps.length > 0) {
    const candidate = { ...trace, steps, step_count: steps.length, dropped_steps: dropped };
    if (Buffer.byteLength(serialized(candidate)) <= maxBytes) return candidate;
    const cut = Math.max(1, Math.floor(steps.length / 4));
    dropped += cut;
    steps = steps.slice(cut).map((step, seq) => ({ ...step, seq }));
  }
  return { ...trace, steps, step_count: 0, dropped_steps: dropped };
}

export async function writeTraceFile(cwd, trace) {
  const relative = `${TRACE_DIRECTORY}/${traceFileName(trace.trace_id)}`;
  const target = join(cwd, relative);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, serialized(trace), { mode: 0o600 });
  return relative;
}

function preview(steps) {
  return steps.filter((step) => step.kind === "tool").map((step) => step.tool).slice(0, 4).join(" → ");
}

async function sessionFile(manager, id) {
  if (isAbsolute(id)) return id;
  const directory = manager.getSessionDir();
  const direct = join(directory, id);
  if (id.endsWith(".jsonl")) return direct;
  const named = join(directory, `${id}.jsonl`);
  try {
    await stat(named);
    return named;
  } catch {
    return direct;
  }
}

async function readEntries(manager, id) {
  if (id === manager.getSessionId() || id === manager.getSessionFile()) {
    return { entries: manager.getEntries(), sessionId: manager.getSessionId(),
      sessionName: manager.getSessionName() };
  }
  const path = await sessionFile(manager, id);
  const entries = parseSessionLog(await readFile(path, "utf8"));
  const header = entries.find((entry) => entry?.type === "session");
  return { entries, sessionId: header?.id, sessionName: undefined, path };
}

export async function listSessions(ctx, { limit = SESSION_LIMIT } = {}) {
  const manager = ctx.sessionManager;
  const current = traceSteps(manager.getEntries());
  const sessions = [{
    trace_id: manager.getSessionId(),
    session_id: manager.getSessionId(),
    session_name: manager.getSessionName(),
    current: true,
    step_count: current.steps.length,
    preview: preview(current.steps),
  }];
  let names = [];
  try {
    names = await readdir(manager.getSessionDir());
  } catch {
    return sessions;
  }
  const currentFile = manager.getSessionFile();
  const candidates = [];
  for (const name of names) {
    if (!name.endsWith(".jsonl")) continue;
    const path = join(manager.getSessionDir(), name);
    if (path === currentFile) continue;
    const info = await stat(path).catch(() => null);
    if (info?.isFile()) candidates.push({ path, name, modified: info.mtimeMs });
  }
  candidates.sort((left, right) => right.modified - left.modified);
  for (const candidate of candidates.slice(0, limit)) {
    const text = await readFile(candidate.path, "utf8").catch(() => "");
    const entries = parseSessionLog(text);
    const header = entries.find((entry) => entry?.type === "session");
    const { steps } = traceSteps(entries);
    const fallbackId = basename(candidate.name, ".jsonl");
    sessions.push({
      trace_id: header?.id ?? fallbackId,
      session_id: header?.id ?? fallbackId,
      current: false,
      path: candidate.path,
      started_at: header?.timestamp,
      step_count: steps.length,
      preview: preview(steps),
    });
  }
  return sessions;
}

// Export the requested sessions next to the workspace so the Python service can read
// them through the host file API instead of a size-limited RPC frame.
export async function exportTraces(ctx, traceIds, { cwd = ctx.cwd, maxSteps } = {}) {
  const refs = [];
  for (const id of traceIds ?? []) {
    const resolved = await readEntries(ctx.sessionManager, String(id));
    const trace = buildTrace(resolved.entries, {
      traceId: resolved.sessionId ?? String(id),
      sessionId: resolved.sessionId,
      sessionName: resolved.sessionName,
      maxSteps,
    });
    const fitted = fitTrace(trace);
    refs.push({ trace_id: fitted.trace_id, path: await writeTraceFile(cwd, fitted),
      step_count: fitted.step_count, dropped_steps: fitted.dropped_steps });
  }
  return refs;
}
