import { randomUUID } from "node:crypto";
import { NativeClient, setupRuntime } from "./native-client.js";

const ACTIVE = new Set(["queued", "running", "cancelling"]);
const RECEIPT = "agentgenome.completed";

export function deliveredRunIds(entries) {
  return new Set(entries.flatMap((entry) => {
    const message = entry.type === "custom_message" ? entry
      : entry.type === "message" && entry.message?.role === "custom" ? entry.message : null;
    return message?.customType === RECEIPT && message.details?.run_id ? [message.details.run_id] : [];
  }));
}

export function progressLines(run) {
  const progress = run.progress || {};
  return [`历史经验 ${run.asset_id}@${run.version} · ${run.status}`,
    `节点：${progress.node || run.node || "等待启动"} · ${progress.event || run.status}`,
    ...(progress.command ? [`命令：${progress.command}`] : []),
    ...(progress.verdict ? [`验收：${progress.verdict}`] : []),
    ...(progress.tail ? [progress.tail.slice(-2000)] : [])];
}

export function nativeHost(pi, { clientFactory = (options) => new NativeClient(options), setup = setupRuntime } = {}) {
  let current;

  function alive(state) { return current === state && !state.closed; }
  function show(state, lines) {
    if (alive(state) && state.ctx.hasUI) state.ctx.ui.setWidget("agentgenome", lines);
  }
  function entries(state) { return state.ctx.sessionManager.getEntries(); }
  function begin(ctx) {
    if (current) stop();
    const scope = `pi:${ctx.sessionManager.getSessionFile() || ctx.sessionManager.getSessionId()}`;
    const state = { ctx, scope, ready: new Set(), sent: new Map(), closed: false };
    state.client = clientFactory({ scope, cwd: ctx.cwd });
    current = state;
    const delivered = deliveredRunIds(entries(state));
    if (entries(state).some((entry) => entry.type === "custom" && entry.customType === "agentgenome.run"
      && !delivered.has(entry.data?.run_id))) watch(state);
    return state;
  }
  function stop() {
    if (!current) return;
    current.closed = true;
    clearTimeout(current.timer);
    current.client.close();
    current = undefined;
  }

  async function refresh(state) {
    const { runs } = await state.client.request("snapshot");
    if (!alive(state)) return false;
    const delivered = deliveredRunIds(entries(state));
    for (const run of runs) {
      if (!alive(state)) return false;
      show(state, progressLines(run));
      if (ACTIVE.has(run.status)) continue;
      if (delivered.has(run.id) || run.status === "cancelled") {
        await state.client.request("ack", { run_id: run.id, receipt: true });
        state.sent.delete(run.id);
        continue;
      }
      if (state.sent.has(run.id)) {
        await state.client.request("renew", { run_id: run.id, token: state.sent.get(run.id) });
        continue;
      }
      const { token } = await state.client.request("claim", { run_id: run.id });
      if (!token || !alive(state)) continue;
      state.sent.set(run.id, token);
      // A completion belongs to this session, never whichever session was switched to later.
      try { pi.sendMessage({ customType: RECEIPT, display: true,
        details: { run_id: run.id, asset_id: run.asset_id, version: run.version },
        content: `历史经验执行结束：${JSON.stringify(run)}\n`
          + (run.status === "succeeded"
            ? "请读取结果中的实际产物并向用户汇报，给出真实本地文件路径。"
            : "请向用户解释失败或中断原因，不要自动重复执行历史经验。"),
      }, { triggerTurn: true, deliverAs: "followUp" }); }
      catch (error) {
        state.sent.delete(run.id);
        await state.client.request("release", { run_id: run.id, token });
        throw error;
      }
      // Ack only after the actual custom_message appears in Pi history, not when queued.
    }
    return runs.length > 0;
  }

  function watch(state) {
    if (!alive(state) || state.watching) return;
    state.watching = true;
    const tick = async () => {
      if (!alive(state)) return;
      let more = true;
      try { more = await refresh(state); }
      catch (error) { show(state, [`历史经验连接异常：${error.message}`]); }
      if (!alive(state)) return;
      if (more) state.timer = setTimeout(tick, 1000);
      else state.watching = false;
    };
    state.timer = setTimeout(tick, 0);
  }

  async function startReady(state) {
    for (const id of state.ready) {
      if (!alive(state)) return;
      try {
        await state.client.request("start", { run_id: id });
        state.ready.delete(id);
      } catch (error) { show(state, [`无法确认启动 ${id}：${error.message}；请查询状态。`]); }
    }
    watch(state);
  }

  function remember(state, run) {
    if (!alive(state)) return;
    pi.appendEntry("agentgenome.run", { run_id: run.id });
    if (run.status === "queued") state.ready.add(run.id);
    watch(state);
  }

  pi.on("session_start", (_event, ctx) => { begin(ctx); });
  pi.on("session_shutdown", () => { stop(); });
  pi.on("agent_end", async () => { if (current?.ready.size) await startReady(current); });
  pi.registerCommand("genome", {
    description: "AgentGenome: setup | import <path> | validate <id> <version> <JSON params> | publish <id> <version>",
    async handler(input, ctx) {
      try {
        const text = input.trim();
        if (text === "setup") {
          ctx.ui.notify("正在安装 AgentGenome 隔离 Python 环境…", "info");
          const python = await setup();
          ctx.ui.notify(`AgentGenome 已就绪：${python}`, "info");
          return;
        }
        const state = current || begin(ctx);
        let action, args;
        if (text.startsWith("import ")) { action = "import"; args = { source: text.slice(7).trim() }; }
        else {
          const match = /^(validate|publish)\s+(\S+)\s+(\S+)(?:\s+([\s\S]+))?$/.exec(text);
          if (!match) throw new Error("用法：/genome setup | import <目录> | validate <ID> <版本> <JSON参数> | publish <ID> <版本>");
          action = match[1]; args = { asset_id: match[2], version: match[3] };
          if (action === "validate") args.params = JSON.parse(match[4] || "{}");
        }
        const result = await state.client.request(action, args, randomUUID());
        if (!alive(state)) return;
        ctx.ui.notify(JSON.stringify(result), "info");
        if (action === "validate") { remember(state, result); await startReady(state); }
      } catch (error) { ctx.ui.notify(error.message, "error"); }
    },
  });

  return {
    async invoke(action, args, callId, _signal, ctx) {
      const state = current || begin(ctx);
      const result = await state.client.request(action, args, callId);
      if (action === "run") remember(state, result);
      else if (action === "cancel") watch(state);
      return result;
    },
  };
}
