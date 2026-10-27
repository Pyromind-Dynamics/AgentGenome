import { randomUUID } from "node:crypto";
import { NativeClient, setupRuntime, defaultHome, VERSION } from "./native-client.js";
import { modelJudge, supportsModelJudge } from "./native-judge.js";
import { executeNativeTool } from "./native-tools.js";

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
  const external = waiting?.kind === "external";
  return [`历史经验 ${run.asset_id}@${run.version} · ${run.status}`,
    `节点：${progress.node || waiting?.node || run.node || "等待启动"} · ${progress.event || run.status}`,
    ...(waiting?.question ? [`待办：${waiting.question}`] : []),
    ...(waiting?.artifact ? [`产物：${waiting.artifact}`] : []),
    ...(external ? ["正在等待外部任务：后台自动检查，无需人工写入产物或重新提交。",
      ...(waiting.task_id ? [`任务：${waiting.task_id}`] : []),
      ...(waiting.deadline ? [`截止时间：${new Date(waiting.deadline * 1000).toISOString()}`] : []),
      "后续需要 Pi 工具或模型时须保持会话连接并具备执行上下文。取消仅停止工作流等待。"] : []),
    ...(progress.command ? [`命令：${progress.command}`] : []),
    ...(progress.verdict ? [`验收：${progress.verdict}`] : []),
    ...(progress.tail ? [progress.tail.slice(-2000)] : [])];
}

export function nativeHost(pi, { clientFactory = (options) => new NativeClient(options), setup = setupRuntime,
  judge = modelJudge, judgeTimeout = 90000, registryHome = defaultHome } = {}) {
  let current;

  function alive(state) { return current === state && !state.closed; }
  function receiptWasFinalAction(messages, callId) {
    if (!callId || !Array.isArray(messages)) return false;
    let receiptSeen = false;
    for (const message of messages) {
      if (message?.role !== "assistant" || !Array.isArray(message.content)) continue;
      for (const part of message.content) {
        if (part?.type !== "toolCall") continue;
        if (part.id === callId) receiptSeen = true;
        else if (receiptSeen) return false;
      }
    }
    return receiptSeen;
  }
  function show(state, lines) {
    if (alive(state) && state.ctx.hasUI) state.ctx.ui.setWidget("agentgenome", lines);
  }
  function entries(state) { return state.ctx.sessionManager.getEntries(); }
  function begin(ctx) {
    if (current) stop();
    const scope = `pi:${ctx.sessionManager.getSessionFile() || ctx.sessionManager.getSessionId()}`;
    const saved = [...ctx.sessionManager.getEntries()].reverse().find(entry =>
      entry.type === "custom" && entry.customType === "agentgenome.registry"
      && typeof entry.data?.home === "string" && entry.data.home.startsWith("/"));
    const home = saved?.data.home || registryHome();
    const state = { ctx, scope, ready: new Set(), sent: new Map(),
      gates: deliveredGates(ctx.sessionManager.getEntries()), closed: false, judges: new Map(),
      toolExecutors: new Map(), toolTasks: new Map() };
    state.client = clientFactory({ home, scope, cwd: ctx.cwd, judgeProtocol: supportsModelJudge(ctx) });
    state.registry = { home, scope, version: VERSION };
    state.registryPinned = Boolean(saved);
    current = state;
    if (saved && registryHome() !== home && ctx.hasUI) {
      ctx.ui.notify?.(`AgentGenome 继续使用本会话的 registry：${home}`, "info");
    }
    const delivered = deliveredRunIds(entries(state));
    if (entries(state).some((entry) => entry.type === "custom" && entry.customType === "agentgenome.run"
      && !delivered.has(entry.data?.run_id))) watch(state);
    return state;
  }
  function rememberRegistry(state) {
    if (alive(state) && !state.registryPinned) {
      pi.appendEntry?.("agentgenome.registry", state.registry);
      state.registryPinned = true;
    }
  }
  function stop() {
    if (!current) return;
    current.closed = true;
    clearTimeout(current.timer);
    for (const task of current.judges.values()) task.controller.abort();
    for (const task of current.toolTasks.values()) { task.controller.abort(); clearTimeout(task.timer); }
    current.client.close();
    current = undefined;
  }

  async function refresh(state) {
    const { runs, stages = [], judges = [], tools = [] } = await state.client.request("snapshot");
    if (!alive(state)) return false;
    rememberRegistry(state);
    for (const [id, task] of state.toolTasks) {
      const pending = tools.find(item => item.execution_id === id);
      if (!pending || pending.cancel_requested) task.controller.abort();
      if (!pending) clearTimeout(task.timer);
    }
    for (const pending of tools) {
      if (pending.claimed || pending.cancel_requested || state.toolTasks.has(pending.execution_id)) continue;
      const claim = await state.client.request("tool_claim", { execution_id: pending.execution_id });
      if (!claim.claimed || !alive(state)) continue;
      const controller = new AbortController();
      const task = { controller, timer: setTimeout(() => controller.abort(), 300000) };
      state.toolTasks.set(pending.execution_id, task);
      void (async () => {
        try {
          let response;
          try {
            response = await executeNativeTool(state.toolExecutors.get(pending.run_id) || state.toolExecutor,
              claim.request, controller.signal);
          } catch (error) { response = { error: String(error.message || "Tool outcome unknown").slice(0, 2000) }; }
          if (alive(state)) {
            pi.appendEntry("agentgenome.tool", { run_id: pending.run_id, execution_id: pending.execution_id,
              name: claim.request.name, state: typeof response.is_error === "boolean"
                ? response.is_error ? "failed" : "completed" : "unknown" });
            await state.client.request("tool_result", { execution_id: pending.execution_id, response });
          }
        } catch (error) { if (alive(state)) show(state, [`工具结果未确认，勿自动重复执行：${error.message}`]); }
        finally { clearTimeout(task.timer); state.toolTasks.delete(pending.execution_id); }
      })();
    }
    for (const task of state.judges.values()) {
      if (!runs.some(run => run.id === task.run_id && run.status === "running")) task.controller.abort();
    }
    for (const pending of judges) {
      if (state.judges.has(pending.request_id)) continue;
      const claim = await state.client.request("judge_claim", { request_id: pending.request_id });
      if (!claim.claimed || !alive(state)) continue;
      const controller = new AbortController();
      state.judges.set(pending.request_id, { controller, run_id: pending.run_id });
      // Separate request: no chat message, agent turn, tool, or stage receipt.
      void (async () => {
        let timer, abort;
        try {
          const aborted = new Promise((_, reject) => {
            abort = () => reject(new Error("Model judge cancelled or timed out"));
            controller.signal.addEventListener("abort", abort, { once: true });
          });
          timer = setTimeout(() => controller.abort(), judgeTimeout);
          let response;
          try { response = { verdict: await Promise.race([judge(state.ctx, claim.request, controller.signal), aborted]) }; }
          catch (error) { response = { error: String(error.message || "Model judge failed").slice(0, 2000) }; }
          if (alive(state)) {
            await state.client.request("judge_result", { request_id: pending.request_id, response });
          }
        } catch (error) { if (alive(state)) show(state, [`模型验收回执未确认：${error.message}`]); }
        finally {
          clearTimeout(timer);
          controller.signal.removeEventListener("abort", abort);
          state.judges.delete(pending.request_id);
        }
      })();
    }
    const delivered = deliveredRunIds(entries(state));
    if (state.ctx.isIdle?.() ?? true) state.aborting = false;
    if (!state.stage && !state.aborting && (state.ctx.isIdle?.() ?? true)) {
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
        if (state.stage.active) {
          // Pi abort settles asynchronously. A follow-up queued during that
          // teardown may never enter session history, leaving its lease renewing forever.
          state.aborting = true;
          state.ctx.abort?.();
        }
        state.stage = undefined;
      }
      show(state, progressLines(run));
      if ((run.status === "waiting" || run.status === "waiting_external")
          && run.result?.waiting?.kind === "external") continue;
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
      if (state.aborting) continue;
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
    return runs.length > 0 || stages.length > 0 || state.judges.size > 0;
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
    if (state.toolExecutor) state.toolExecutors.set(run.id, state.toolExecutor);
    pi.appendEntry("agentgenome.run", { run_id: run.id });
    if (run.status === "queued") state.ready.add(run.id);
    watch(state);
  }

  function rememberCapture(state, result) {
    if (result?.run_id) remember(state, { id: result.run_id, status: "queued" });
  }

  function rememberRun(state, result) {
    const run = result?.run?.id ? result.run : (result?.id ? result : null);
    if (run) remember(state, run);
  }

  pi.on("session_start", (_event, ctx) => { begin(ctx); });
  pi.on("session_shutdown", () => { stop(); });
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
      const receiptSurvivedProviderError = last?.stopReason === "error"
        && receiptWasFinalAction(event?.messages, stage.receiptCallId);
      const status = last?.stopReason === "stop" || receiptSurvivedProviderError
        ? "completed" : last?.stopReason === "aborted" ? "aborted" : "failed";
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
    description: "AgentGenome: registry | setup | import <path> | validate <id> <version> <JSON params> | publish <id> <version>",
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
        if (text === "registry") {
          ctx.ui.notify(JSON.stringify(state.registry), "info");
          return;
        }
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
        if (["run", "confirm_run", "confirm_distill", "resume", "replay_requeue", "capture", "distill", "prepare_revision"].includes(action)) {
          throw new Error("An llm stage cannot start or resume another graph");
        }
      }
      if (action === "step_result") {
        const stage = state.stage;
        if (!stage?.active || args.request_id !== stage.request.request_id) {
          throw new Error("genome_step_result must belong to this active llm stage round");
        }
        args = { ...args, execution_id: stage.execution_id };
        const result = await state.client.request(action, args, callId);
        if (result?.accepted === true) stage.receiptCallId = callId;
        rememberRegistry(state);
        return result;
      }
      const executor = typeof ctx.executeTool === "function"
        ? { execute: ctx.executeTool.bind(ctx), names: () => (ctx.tools || []).map(tool => tool.name) } : undefined;
      if (executor && !state.toolsBound) {
        await state.client.request("tool_bind", { protocol: 1 });
        state.toolsBound = true;
      }
      if (executor) state.toolExecutor = executor;
      const result = await state.client.request(action, args, callId);
      rememberRegistry(state);
      const run = result?.run?.id ? result.run : result?.id ? result : result?.run_id ? { id: result.run_id } : null;
      if (executor && run && ["confirm_run", "run", "resume", "replay_requeue", "capture", "confirm_distill"].includes(action)) {
        state.toolExecutors.set(run.id, executor);
      }
      if (action === "prepare_run") return result;
      if (["confirm_run", "run", "resume", "replay_requeue"].includes(action)) {
        rememberRun(state, result);
      }
      else if (action === "capture" || action === "confirm_distill") rememberCapture(state, result);
      else if (action === "cancel") {
        if (state.stage?.active && state.stage.request.run_id === args.run_id) state.ctx.abort?.();
        watch(state);
      }
      if (["list", "distill", "prepare_distill"].includes(action)
        && result && typeof result === "object") return { ...result, registry: state.registry };
      return result;
    },
  };
}
