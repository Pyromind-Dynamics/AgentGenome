import { randomUUID } from "node:crypto";
import { NativeClient, setupRuntime } from "./native-client.js";

const ACTIVE = new Set(["queued", "running", "cancelling"]);
const RECEIPT = "agentgenome.completed";
const GATE = "agentgenome.waiting";
const STAGE = "agentgenome.stage";

function gateKey(waiting) {
  return JSON.stringify([waiting.node || "", waiting.question || "", waiting.artifact || ""]);
}

function deliveredGates(entries) {
  const gates = new Map();
  for (const entry of entries) {
    const message = entry.type === "custom_message" ? entry
      : entry.type === "message" && entry.message?.role === "custom" ? entry.message : null;
    if (message?.customType !== GATE || !message.details?.run_id) continue;
    if (typeof message.details.gate_key === "string") {
      gates.set(message.details.run_id, message.details.gate_key);
    } else if (typeof message.content === "string") {
      // Existing sessions have the work order in the first line, without a key.
      try {
        const firstLine = message.content.split("\n")[0];
        const waiting = JSON.parse(firstLine.slice(firstLine.indexOf("{")));
        if (waiting.node && waiting.artifact) gates.set(message.details.run_id, gateKey(waiting));
      } catch {}
    }
  }
  return gates;
}

export function deliveredRunIds(entries) {
  return new Set(entries.flatMap((entry) => {
    const message = entry.type === "custom_message" ? entry
      : entry.type === "message" && entry.message?.role === "custom" ? entry.message : null;
    return message?.customType === RECEIPT && message.details?.run_id ? [message.details.run_id] : [];
  }));
}

export function progressLines(run) {
  const progress = run.progress || {};
  const waiting = run.status === "waiting" ? run.result?.waiting || {} : null;
  return [`历史经验 ${run.asset_id}@${run.version} · ${run.status}`,
    `节点：${progress.node || waiting?.node || run.node || "等待启动"} · ${progress.event || run.status}`,
    ...(waiting?.question ? [`待办：${waiting.question}`] : []),
    ...(waiting?.artifact ? [`产物：${waiting.artifact}`] : []),
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
    const state = { ctx, scope, ready: new Set(), sent: new Map(),
      gates: deliveredGates(ctx.sessionManager.getEntries()), closed: false };
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
    const { runs, stages = [] } = await state.client.request("snapshot");
    if (!alive(state)) return false;
    const delivered = deliveredRunIds(entries(state));
    if (!state.stage && (state.ctx.isIdle?.() ?? true)) {
      const pending = stages.find((stage) => !stage.claimed);
      if (pending) {
        const claim = await state.client.request("stage_claim", { request_id: pending.request.request_id });
        if (claim.claimed && alive(state)) {
          const stage = { ...claim, active: false };
          state.stage = stage;
          try {
            pi.sendMessage({ customType: STAGE, display: false,
              details: { request_id: claim.request.request_id, execution_id: claim.execution_id, scope: state.scope },
              content: `执行历史经验的 llm 阶段：${JSON.stringify(claim.request)}\n`
                + "只执行本阶段任务，写入指定产物后调用 genome_step_result，再立即结束当前轮次。不要新建或恢复其他图，不要重跑上下游脚本。",
            }, { triggerTurn: true, deliverAs: "followUp" });
          } catch (error) {
            state.stage = undefined;
            // Closing the owner connection fails the handoff; never re-deliver it.
            state.client.close();
            throw error;
          }
        }
      }
    }
    for (const run of runs) {
      if (!alive(state)) return false;
      if (state.stage?.request.run_id === run.id && !ACTIVE.has(run.status)) {
        if (state.stage.active) state.ctx.abort?.();
        state.stage = undefined;
      }
      show(state, progressLines(run));
      if (run.status === "waiting") {
        const waiting = run.result?.waiting || {};
        const gate = gateKey(waiting);
        if (state.gates.get(run.id) !== gate) {
          state.gates.set(run.id, gate);
          try { pi.sendMessage({ customType: GATE, display: true,
            details: { run_id: run.id, asset_id: run.asset_id, version: run.version, gate_key: gate },
            content: `历史经验停在人工节点：${JSON.stringify({ run_id: run.id, ...waiting })}\n`
              + "请把 node / question / artifact 告诉用户，等用户完成人工步骤并把结果写进该 artifact 后，"
              + "用同一个 run_id 调用 genome_resume 唤醒这条 run；不要替用户伪造人工产物。",
          }, { triggerTurn: true, deliverAs: "followUp" }); }
          catch (error) { state.gates.delete(run.id); throw error; }
        }
        continue;
      }
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
      const capture = run.result?.capture;
      const guidance = capture
        ? `这是轨迹沉淀的回放验证。请读取报告 ${capture.report}，向用户说明是否已发布为可复用经验、泛化结论以及草稿位置。`
        : run.status === "succeeded"
          ? "请读取结果中的实际产物并向用户汇报，给出真实本地文件路径。"
          : "请向用户解释失败或中断原因，不要自动重复执行历史经验。";
      // A completion belongs to this session, never whichever session was switched to later.
      try { pi.sendMessage({ customType: RECEIPT, display: true,
        details: { run_id: run.id, asset_id: run.asset_id, version: run.version },
        content: `历史经验执行结束：${JSON.stringify(run)}\n${guidance}`,
      }, { triggerTurn: true, deliverAs: "followUp" }); }
      catch (error) {
        state.sent.delete(run.id);
        await state.client.request("release", { run_id: run.id, token });
        throw error;
      }
      // Ack only after the actual custom_message appears in Pi history, not when queued.
    }
    return runs.length > 0 || stages.length > 0;
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

  function rememberCapture(state, result) {
    if (result?.run_id) remember(state, { id: result.run_id, status: "queued" });
  }

  pi.on("session_start", (_event, ctx) => { begin(ctx); });
  pi.on("session_shutdown", () => { stop(); });
  pi.on("context", (event) => {
    const state = current, stage = state?.stage;
    // Keep the audit history intact, but never send old stage instructions to
    // a later model round (abort alone leaves the custom message in history).
    return { messages: event.messages.filter((message) =>
      message.role !== "custom" || message.customType !== STAGE
      || (stage?.active && message.details?.request_id === stage.request.request_id
        && message.details?.execution_id === stage.execution_id
        && message.details?.scope === state.scope)) };
  });
  pi.on("message_start", async (event) => {
    const state = current, stage = state?.stage, message = event.message;
    if (message?.role !== "custom" || message.customType !== STAGE) return;
    const live = stage && message.details?.request_id === stage.request.request_id
      && message.details?.execution_id === stage.execution_id && message.details?.scope === state.scope;
    if (!live) {
      if (state) {
        // A delivered stage message whose handoff is already dead (cancelled run,
        // switched session) must never reach the model; abort the turn it arrived in.
        state.ctx.abort?.();
        show(state, ["已拦截失效的 llm 阶段消息：运行已取消或阶段已结束，本轮中止。"]);
      }
      return;
    }
    try {
      await state.client.request("stage_begin", {
        request_id: stage.request.request_id, execution_id: stage.execution_id,
      });
      if (alive(state) && state.stage === stage) stage.active = true;
    } catch (error) {
      if (alive(state)) {
        state.ctx.abort?.();
        show(state, [`阶段已失效：${error.message}`]);
      }
    }
  });
  pi.on("agent_end", async (event) => {
    const state = current, stage = state?.stage;
    if (stage?.active) {
      const last = [...(event?.messages || [])].reverse().find((message) => message.role === "assistant");
      const status = last?.stopReason === "stop" ? "completed" : last?.stopReason === "aborted" ? "aborted" : "failed";
      try {
        await state.client.request("stage_finish", {
          request_id: stage.request.request_id, execution_id: stage.execution_id, status,
        });
      } catch (error) { if (alive(state)) show(state, [`阶段结束确认失败：${error.message}`]); }
      finally { if (state.stage === stage) state.stage = undefined; }
    }
    if (current?.ready.size) await startReady(current);
  });
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
      if (state.stage?.active) {
        if (["run", "resume", "capture", "distill", "prepare_revision"].includes(action)) {
          throw new Error("An llm stage cannot start or resume another graph");
        }
      }
      if (action === "step_result") {
        const stage = state.stage;
        if (!stage?.active || args.request_id !== stage.request.request_id) {
          throw new Error("genome_step_result must belong to this active llm stage round");
        }
        args = { ...args, execution_id: stage.execution_id };
      }
      const result = await state.client.request(action, args, callId);
      if (action === "run" || action === "resume") remember(state, result);
      else if (action === "capture" || action === "distill") rememberCapture(state, result);
      else if (action === "cancel") {
        if (state.stage?.active && state.stage.request.run_id === args.run_id) state.ctx.abort?.();
        watch(state);
      }
      return result;
    },
  };
}
