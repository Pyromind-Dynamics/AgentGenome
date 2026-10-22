// Trace export: compact previews for legacy callers, complete evidence for capture.
// Uses only ctx.sessionManager and Node built-ins, so every Pi host works the same way.

import { mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, join } from "node:path";

export const TRACE_VERSION = 1;
export const TRACE_DIRECTORY = "public_data/agentgenome-traces";
export const DEFAULT_MAX_STEPS = 400;
// Legacy compact exports fit a single RPC read; complete exports use local file reads.
export const MAX_TRACE_BYTES = 240 * 1024;
// A complete export keeps every step. The cap guards memory, not evidence: tool output
// is projected first (see `projectText`), so this is measured after the payload is gone.
export const MAX_COMPLETE_TRACE_BYTES = 8 * 1024 * 1024;
const TEXT_LIMIT = 2000;
const ARRAY_LIMIT = 20;
const SESSION_LIMIT = 20;

// Complete exports keep tool output only where it can carry evidence. `_reported_writes`
// reads a product path off a line that also names the write, and `_REFUSAL` reads a
// refusal; everything else is a progress log that only costs bytes. Keeping the head and
// tail as well preserves what a human needs to read the step.
const EXCERPT_FULL_LIMIT = 4096;
const EXCERPT_HEAD_LINES = 20;
const EXCERPT_TAIL_LINES = 20;
const EXCERPT_LINE_LIMIT = 4096;
const EXCERPT_LINE_HEAD = 1024;
const EXCERPT_LINE_TAIL = 3000;
// Deliberately broader than the Python readers: an over-kept line only costs bytes,
// a dropped one loses evidence.
const EVIDENCE_LINE =
  /wrote|written|saved|output|outputted|已写入|写入|输出到|产物|denied|rejected|blocked|not allowed|导出|handoff/i;
const ARROW_LINE = /(?:->|=>)/;

function clip(value, limit = TEXT_LIMIT, clippedFields, field) {
  if (typeof value !== "string" || value.length <= limit) return value;
  if (clippedFields && !clippedFields.includes(field)) clippedFields.push(field);
  return `${value.slice(0, limit)}…[+${value.length - limit} chars]`;
}

function textOf(content) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n");
}

function clipArguments(args, clippedFields) {
  if (!args || typeof args !== "object" || Array.isArray(args)) return args;
  const clipped = {};
  for (const [key, value] of Object.entries(args)) {
    const field = `args.${key}`;
    if (typeof value === "string") clipped[key] = clip(value, TEXT_LIMIT, clippedFields, field);
    else if (Array.isArray(value)) {
      if (value.length > ARRAY_LIMIT) clippedFields.push(field);
      clipped[key] = value.slice(0, ARRAY_LIMIT).map((item) =>
        clip(item, TEXT_LIMIT, clippedFields, field));
    } else clipped[key] = value;
  }
  return clipped;
}

function excerptLine(line) {
  if (line.length <= EXCERPT_LINE_LIMIT) return line;
  const tail = line.slice(-EXCERPT_LINE_TAIL);
  return `${line.slice(0, EXCERPT_LINE_HEAD)}…[+${line.length - EXCERPT_LINE_HEAD - EXCERPT_LINE_TAIL} chars]…${tail}`;
}

function namesAPath(line) {
  return line.split(/\s+/).some((token) => {
    const value = token.replace(/^["'`([{<]+/, "").replace(/["'`)\]}>.,;:]+$/, "");
    return value.lastIndexOf("/") > 0 && /\.[A-Za-z0-9]{1,8}$/.test(value);
  });
}

// Keep the lines that can carry evidence plus the head and tail, and mark what was
// dropped. A short text is returned untouched, so ordinary steps stay byte-identical.
export function projectText(text) {
  if (typeof text !== "string" || text.length <= EXCERPT_FULL_LIMIT) {
    return { text, excerpt: null };
  }
  const lines = text.split("\n");
  const keep = new Set();
  lines.forEach((line, index) => {
    if (index < EXCERPT_HEAD_LINES || index >= lines.length - EXCERPT_TAIL_LINES
      || EVIDENCE_LINE.test(line) || ARROW_LINE.test(line) || namesAPath(line)) {
      keep.add(index);
    }
  });
  const kept = [...keep].sort((left, right) => left - right);
  const parts = [];
  let previous = -1;
  for (const index of kept) {
    if (previous >= 0 && index > previous + 1) {
      parts.push(`…[${index - previous - 1} 行省略]…`);
    }
    parts.push(excerptLine(lines[index]));
    previous = index;
  }
  return {
    text: parts.join("\n"),
    excerpt: { kept_lines: kept.length, total_lines: lines.length, total_chars: text.length },
  };
}

// Pair assistant tool calls with their results; user turns stay in the sequence so the
// captured graph can tell an instruction from an action.
export function traceSteps(entries, maxSteps = DEFAULT_MAX_STEPS, { complete = false } = {}) {
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
      const clippedFields = [];
      const text = textOf(message.content);
      const projected = complete
        ? projectText(text)
        : { text: clip(text, TEXT_LIMIT, clippedFields, "text"), excerpt: null };
      steps.push({ kind: "user", entry_id: entry.id, timestamp: entry.timestamp,
        text: projected.text,
        ...(projected.excerpt ? { text_excerpt: projected.excerpt } : {}),
        ...(clippedFields.length ? { clipped_fields: clippedFields } : {}) });
      continue;
    }
    if (message?.role !== "assistant") continue;
    for (const block of message.content ?? []) {
      if (block?.type !== "toolCall") continue;
      const result = results.get(block.id);
      const clippedFields = [];
      const resultText = result ? textOf(result.content) : null;
      // Arguments first: `clipped_fields` lists them in the order they were clipped.
      const args = complete ? block.arguments : clipArguments(block.arguments, clippedFields);
      const projected = complete && resultText !== null
        ? projectText(resultText)
        : { text: clip(resultText, TEXT_LIMIT, clippedFields, "result.text"), excerpt: null };
      steps.push({ kind: "tool", entry_id: entry.id, timestamp: entry.timestamp,
        tool: block.name, call_id: block.id, args,
        result: result
          ? { is_error: result.isError === true, text: projected.text,
            ...(projected.excerpt ? { excerpt: projected.excerpt } : {}) }
          : null,
        ...(clippedFields.length ? { clipped_fields: clippedFields } : {}) });
    }
  }
  const dropped = complete ? 0 : Math.max(0, steps.length - maxSteps);
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

export function buildTrace(entries, { traceId, sessionId, sessionName, maxSteps, complete = false } = {}) {
  const { steps, dropped } = traceSteps(entries, maxSteps, { complete });
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

function excerptedSteps(trace) {
  return trace.steps.filter((step) => step.result?.excerpt || step.text_excerpt).length;
}

// Name the steps that make an export too big. "Split the session" is not actionable on
// its own; "step 12 carries a 3 MiB inline script" tells the author what to change.
function largestSteps(trace, limit = 3) {
  return trace.steps
    .map((step) => ({ step, size: Buffer.byteLength(JSON.stringify(step), "utf8") }))
    .sort((left, right) => right.size - left.size)
    .slice(0, limit)
    .map(({ step, size }) => `${step.tool ?? "user"}@${step.seq}（${(size / 1024).toFixed(0)} KiB）`)
    .join("、");
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

// Export the requested sessions next to the workspace. Capture/distill request
// complete local files; legacy callers can still use the compact RPC-sized export.
export async function exportTraces(ctx, traceIds, { cwd = ctx.cwd, maxSteps, complete = false } = {}) {
  const refs = [];
  for (const id of traceIds ?? []) {
    const resolved = await readEntries(ctx.sessionManager, String(id));
    const trace = buildTrace(resolved.entries, {
      traceId: resolved.sessionId ?? String(id),
      sessionId: resolved.sessionId,
      sessionName: resolved.sessionName,
      maxSteps,
      complete,
    });
    if (complete) {
      const size = Buffer.byteLength(serialized(trace), "utf8");
      if (size > MAX_COMPLETE_TRACE_BYTES) {
        throw new Error(`轨迹 ${trace.trace_id} 完整导出 ${size} 字节，超过 `
          + `${MAX_COMPLETE_TRACE_BYTES} 字节（8 MiB）上限；工具输出已按产物行投影，`
          + `仍然超出说明命令或参数本身过大：${largestSteps(trace)}。`
          + "请把这些大内容改成文件路径传递，或把这条链路拆到更短的会话里再沉淀。");
      }
    }
    const exported = complete ? trace : fitTrace(trace);
    refs.push({ trace_id: exported.trace_id, path: await writeTraceFile(cwd, exported),
      step_count: exported.step_count, dropped_steps: exported.dropped_steps,
      excerpted_steps: complete ? excerptedSteps(exported) : 0 });
  }
  return refs;
}
