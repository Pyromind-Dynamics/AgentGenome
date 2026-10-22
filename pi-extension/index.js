import { nativeHost } from "./native-extension.js";
import { discoverGenomeHost } from "./host-discovery.js";
import { exportTraces, listSessions } from "./session-trace.js";
export { GENOME_HOST_CHANNEL, provideGenomeHost, createBridgeHost } from "./host-discovery.js";

const string = { type: "string", minLength: 1 };
const asset = { asset_id: string, version: string };
const paths = { type: "array", items: string };
const cases = { type: "array", description: "Small regression cases. Each has unique id, files (relative path to text), params (use fixture:filename for inputs), and assertions [{node: full graph node path, output: output key, kind: json|csv|text|hash, expected: JSON subset / complete CSV row arrays including the header / exact text / sha256}]. Preserve inherited cases. Never copy private business datasets into shared fixtures.", items: { type: "object", additionalProperties: true } };
const matching = {
  applies_when: { ...paths, description: "何时适用：任务意图与输入形态（例如「原始问题 CSV 过滤后生成回答」）。后续复用先按它匹配，写场景而不是资产名。" },
  not_applies_when: { ...paths, description: "明确不适用的情况（例如「输入已是 messages-schema JSONL」），避免后续仅凭名字相似误复用。" },
  input_contract: { type: "object", additionalProperties: true, description: "根输入契约：参数名 → {type, format?, required_fields/fields?, description?}。type 必须显式写为 path / string / number / boolean / records；数值、布尔和计数不得省略 type，否则会被当成路径或被计划校验拦截。例如 {data_file: {type: \"path\", format: \"csv\", required_fields: [\"qid\", \"question_text\"]}, limit: {type: \"number\", description: \"最多保留多少行\"}}。复用方据此判断能否直接跑、是否需要先桥接中间产物。" },
  output_contract: { type: "object", additionalProperties: true, description: "产出契约：与 input_contract 同构，写明格式与字段，便于复用方核对结果。" },
  determinism: { type: "string", enum: ["script", "agent-assisted", "human-gated"], description: "复现这条链路需要什么：script=全链路固定调用（含脚本内部调用 LLM，调用形式固定）；agent-assisted=图里有 llm 节点，回放时该步骤交回 agent 执行；human-gated=图里有 human 节点，回放会停下等人。默认按图里的节点种类推导（human 优先于 llm，llm 优先于 script），只有确实要覆盖推导结果时才显式声明。" },
};
const policy = { type: "string", enum: ["scripts-first", "adaptive-first"], description: "固化策略，默认 scripts-first。scripts-first：llm 节点必须用 from 指向会话里真正来源的步骤，插件据此核对它不是固定命令、人工门禁或编辑工具写入，防止把固定命令伪装成 llm 节点。adaptive-first：信任 agent 的判断，from 可选、只作审计。" };
const executionScope = {
  type: "object",
  description: "本次执行范围。用户必须先看到实际输入、行数是否精确、图节点、人工门禁与副作用，再明确选择 full / sample / subset。带路径输入时缺省为 unspecified，不能直接确认执行。",
  properties: {
    mode: { type: "string", enum: ["full", "sample", "subset", "unspecified"] },
    description: { type: "string", description: "给用户看的范围说明，例如「全量 1189 行」或「前 10 行小样」。" },
    records: { type: "integer", minimum: 0, description: "可选：本次明确处理的记录数。" },
    source_path: { ...string, description: "可选：sample/subset 的来源文件。" },
  },
  required: ["mode"],
  additionalProperties: false,
};
const steps = {
  type: "array",
  description: "可选。第一次沉淀不要提交 steps：插件会从 trace 机械抽取候选链路。只有候选有歧义、或用户要求修改节点时才提交完整 steps；只收完整固化步骤。每步只能取下面一种形态，且不得添加 label：{run:\"完整命令\", output?:\"产出\", from?:\"trace:seq\"}、{tool:\"宿主工具名\", args:{}, output:\"结果 JSON artifact\", input?:[], keep?:true, from?}、{human:\"工作单\", input?:[路径], output:\"人工产物\", from?}、{llm:\"提示词\", input?:[路径], output:\"产物\", verify?, from}。标签属于顶层字段，不属于 step。",
  items: { type: "object", properties: {
    run: string,
    llm: string,
    human: { ...string, description: "人工工作单；必须原样包含 input 和 output 路径。" },
    tool: { ...string, description: "宿主工具名。" },
    args: { type: "object", additionalProperties: true, description: "工具参数；声明为 input 的路径必须出现在这里。" },
    input: { ...paths, description: "该节点消费的路径。" },
    output: {
      anyOf: [string, { type: "array", items: string }],
      description: "该节点的产出：单个路径，或多个路径的数组（脚本一次写多个文件时用数组）。",
    },
    verify: string,
    from: { anyOf: [string, { type: "null" }], description: "可选。来源 trace:seq；不确定就省略。" },
    keep: { type: "boolean", description: "副作用属于链路、但产物不被后续节点读取时设为 true；普通节点不加 keep。" },
  }, additionalProperties: false },
};
const tools = [
  ["list", "List latest published historical experiences (历史经验): reusable graphs with scripts and optional agent/human stages. For execution tasks, check these before choosing a Skill. Each entry carries applies_when / not_applies_when / input_contract / determinism when declared, plus a has_samples flag; use genome_get for the full contract and the sampled inputs.", {}, []],
  ["trace", "List recent Pi sessions in this workspace so the user can pick which runs to capture into a historical experience. Read-only. Call it before proposing capture, and never capture a session the user did not ask for.", {}, []],
  ["capture", "Sink the runs the user explicitly asked to 沉淀 into one reusable historical experience: pass the trace ids returned by genome_trace plus label (经验名, shown to the user). Call this only when the user asks to 沉淀/固化 this chain; never on your own initiative. Everything must already be on disk: inline code and heredocs cannot be solidified. Optional asset_id is the reuse handle (ASCII slug); a non-ASCII label degrades to capture-<hash>. Optional final_artifact reverse-slices the graph to what the chain must produce; version pins the released version; description, owner and min_support tune the report. new_params（换参数回放 B）暂时不做，不必提供；先把主轨迹沉淀跑通。Declare applies_when / not_applies_when / input_contract so later reuse can match precisely. 只收完整固化的链路：每步都必须是图能重跑的固定命令、宿主工具调用、llm 节点或 human 节点；有步骤固不下来时机械固化会失败并列出原因，改用 genome_distill 提交规范化链路。Returns a capture id immediately; the replay continues in the background and its result comes back to you automatically.", { trace_ids: { ...paths, description: "Session ids from genome_trace, in the order the chain ran." }, label: string, asset_id: string, final_artifact: string, new_params: { type: "object", additionalProperties: true, description: "可选：换参数回放 B 的输入。当前阶段不做泛化验证，不要主动填；只有用户明确要求时才给。" }, min_support: { type: "number" }, version: string, description: string, owner: string, policy, ...matching }, ["trace_ids", "label"]],
  ["distill", "Prepare one lightweight structural plan from completed Pi traces. Omit steps on the first call so the plugin can mechanically extract artifact-connected candidate nodes, exclude unreferenced branches, and report ambiguities. Only provide steps when correcting that candidate. Planning does not read script bytes, materialize scripts, compile a graph, or queue a replay. Show the plan to the user and wait for confirmation; genome_confirm_distill is the only step that runs one complete preflight. Queue one bounded sample/subset replay only when the user explicitly approves that scope and the plan contains validation_params with execution_scope.mode=sample|subset.", { steps, label: string, asset_id: string, final_artifact: string, source_traces: { ...paths, description: "genome_trace 给出的会话 id，按链路顺序。插件导出这些完整会话，据此机械生成候选步骤并核对链路完整性。" }, params: { type: "object", additionalProperties: true, description: "根输入的真实值。省略时插件从候选链路的根输入推导 path 参数；可覆盖自动命名。" }, new_params: { type: "object", additionalProperties: true, description: "可选：换参数回放 B 的输入。当前阶段不做泛化验证，不要主动填；只有用户明确要求时才给。" }, min_support: { type: "number" }, version: string, description: string, owner: string, policy, ...matching }, ["label", "source_traces"]],
  ["confirm_distill", "Confirm exactly one prepared distillation plan after the user has explicitly approved its source trace, step classification, scripts, side branches, human gates, final artifact and validation scope. Pass plan_id returned by genome_distill and optionally its digest. This is the only step that creates the capture; only when the plan declares a sample/subset validation does it also queue that bounded replay. Confirmation is idempotent: the same plan returns the same capture/run. Never call before user confirmation and never use it to widen a sample/subset to full.", { plan_id: string, digest: string }, ["plan_id"]],
  ["capture_status", "Read the stage, report, replay verdict, published version or draft location of a prepared or confirmed capture.", { capture_id: string }, ["capture_id"]],
  ["get", "Read a historical experience's inputs, expected outputs, applicability and revision_availability before reusing it. Compare applies_when / not_applies_when / input_contract with the real inputs first. input_samples 是沉淀时对该链路根输入做的有界采样（每个参数的文件头/schema 与文本预览），用来核对本次输入是否与当时一致——它只是证据，不是要处理的真实数据。graph 覆盖的确定性步骤按 genome_prepare_run → 用户确认 → genome_confirm_run 执行：不要用文件工具重写 graph 里的脚本，也不要手工跑等价命令来替代它；llm 节点会在回放时交回给 agent，human 节点会停下等人。", asset, ["asset_id", "version"]],
  ["prepare_run", "Prepare a historical-experience execution plan without starting a run. Inspect the real inputs first, then call this with the exact asset, params and intended execution_scope. The result lists input evidence, graph nodes, human gates, side effects and expected outputs. Show that analysis to the user and wait for explicit confirmation before genome_confirm_run. A path input defaults to unspecified and cannot be confirmed; choose full, sample or subset from the user's instruction.", { ...asset, params: { type: "object", additionalProperties: true }, execution_scope: executionScope, allow_mismatch: { type: "boolean", description: "Only when the user explicitly approves reuse despite an input-contract mismatch. The forced drift is recorded in the plan." } }, ["asset_id", "version", "params"]],
  ["confirm_run", "Confirm and start exactly one prepared execution plan after the user has explicitly approved its inputs, graph, side effects and execution scope. Pass the plan_id returned by genome_prepare_run; pass digest too when reporting a plan digest back. Confirmation is idempotent: the same plan returns the same run. Never call this before the user confirms, and never silently widen sample/subset to full.", { plan_id: string, digest: string }, ["plan_id"]],
  ["run", "Reuse a historical experience graph with asset_id + version + params, OR revision_id + params + change_summary, never both (version is optional for a revision candidate). 复用已发布资产前，插件会用沉淀采样的 schema/required_fields 机械核对本次根输入：格式或必需字段不符会直接拒绝并说明差在哪，避免把经验套到不匹配的数据上；确实要按现状强行复用才带 allow_mismatch=true（会被记录为 forced 并在结果里标出）。 这是执行 graph 里那部分确定性步骤的唯一通道：不要为了「顺手」用 terminal 重跑等价命令，也不要用 write/edit 重写 graph 已经固化的脚本（哪怕你觉得能写得更好）。脚本确实不适用时走 revision_id 通道（change_summary + baseline_cases/regression_cases），不要绕开 graph 在会话里重写。Changed scripts/interfaces require baseline_cases when no inherited cases exist, and regression_cases covering the new scenario. Old and new scenarios must work; do not hardcode the new input. Passing all cases publishes the candidate as latest unless latest changed concurrently. Parameter-only changes do not publish. Each revision starts from the beginning. There is no revision-count limit; use the latest run ID for further revisions and decide yourself when genome_takeover is more appropriate. Preserve input paths exactly as verified with host file tools, including any path prefix. With asset_id this is a compatibility alias for genome_prepare_run: it returns an execution plan and does not start a run; show it to the user and use genome_confirm_run after explicit confirmation. With revision_id it returns a run ID immediately and execution continues in the background after this turn yields; completion automatically returns results to you for a follow-up turn. Tell the user it was submitted, not completed. End this turn without polling; do not also execute its scripts yourself.", { ...asset, revision_id: string, change_summary: string, baseline_cases: cases, regression_cases: cases, parameter_patch: { type: "object", description: "Optional declarations and bindings limited to the revision policy; never verification or graph structure.", additionalProperties: true }, allow_mismatch: { type: "boolean", description: "Only with asset_id reuse: run even though the input contract check failed. Records the drift as forced instead of stopping." }, params: { type: "object", additionalProperties: true } }, ["params"]],
  ["resume", "继续一条停在人工节点（do: {human}）上的历史经验 run：回放跑到 human 节点会停下等人，把 node / question 告诉用户（并给出能直接打开的目标地址，见 guidance）；用户完成人工步骤（例如 Label Studio 复核导出）后，用同一个 run_id 调用本工具唤醒它，回放从停下的节点继续，不会再问一遍已完成的人工步骤。用户把导出结果放在自己的位置时，用 artifact 传该文件路径，宿主会把它落到图声明的人工产物上；用户已经写进图给出的 artifact 时省略 artifact。run 不在 waiting / interrupted 状态时只返回当前状态，不会重跑。不要自己执行或重放 graph，也不要替用户伪造人工产物。", { run_id: string, artifact: { ...string, description: "用户导出的人工产物文件路径（用户自己的导出位置，例如 Label Studio 导出的 json 文件）；唤醒时由宿主把它落到图声明的 artifact 上。用户已按工作单把结果写进图给出的 artifact 时省略。" } }, ["run_id"]],
  ["replay_requeue", "把一次死在执行基础设施上的回放重新排队：run_id 传失败的那次回放。仅用于失败与图里的脚本无关的情况（沙箱或 run 目录被回收、连接中断、执行平面超时这类宿主/网络问题）。检查点里还停着人工节点的 run 会从那个检查点继续（人已经做过的部分不会再问一遍），其余情况用同一份冻结草稿从头再跑一次，仍算同一次沉淀验证。图里脚本本身报错属于链路缺陷：修好脚本重新 genome_distill 沉淀，不要用重排掩盖。waiting（停在人工节点）或 queued/running 不算失败，不要重排。已发布的资产不能重排，走 revision 通道。", { run_id: string, artifact: { ...string, description: "可选：人工节点已答复但结果在用户自己的位置时，随重排一并交回该文件路径；宿主落到图声明的 artifact 上。" } }, ["run_id"]],
  ["prepare_revision", "Prepare a further revision from the latest frozen scripts when genome_status recovery.revision.available is true. For a succeeded run whose results do not satisfy the task, reason is required. 只修改本次副本中允许的业务脚本和参数。不要修改验收脚本、验收提示词、阈值、graph 验收规则或伪造验收结果。无法在保持验收标准的前提下完成适配时，调用 genome_takeover。 Legacy fixed script packages allow revision by default: provide editable_scripts and verification_resources declarations. Keep old behavior working and add synthetic regression cases for new behavior. Returned baseline_cases and pending_regression_cases preserve unvalidated examples from the preceding attempt; fix draft mistakes if needed, but never weaken already frozen regression_cases. Host protection may be prompt-only; follow these constraints regardless.", { run_id: string, reason: string, editable_scripts: paths, verification_resources: paths }, ["run_id"]],
  ["takeover", "Hand back the remaining task after a historical experience has stopped, including a succeeded run whose outputs do not satisfy the user. Give the reason and use available outputs with business Skills. This preserves the original execution status; it does not rerun the graph or require revision support. Choose takeover when further revision is not appropriate; failed revisions can be revised again. Do not automatically return to the old graph.", { run_id: string, reason: string }, ["run_id", "reason"]],
  ["step_result", "Write the declared stage outputs, then submit this receipt and end the turn. This only records your result; AgentGenome verifies files after this exact turn ends. Never start another graph from a graph stage.", { request_id: string, outcome: { type: "string", enum: ["completed", "failed"] }, summary: string }, ["request_id", "outcome", "summary"]],
  ["status", "Read progress, results and recovery availability of a historical experience run in this conversation. A succeeded status means the recorded checks passed; still assess whether the outputs satisfy the user.", { run_id: string }, ["run_id"]],
  ["cancel", "Request cancellation of a historical experience run in this conversation.", { run_id: string }, ["run_id"]],
];

// `genome_distill` may freeze a chain narrower than the session, but only after the
// user confirms that boundary; the fields belong to that tool alone.
const extraFields = {
  distill: {
    review_confirmed: { type: "boolean", description: "仅当插件报「链路范围需要用户复核」、且用户明确确认这次只要链路的后半段时才设为 true，必须同时给出 review_reason。不要自己替用户缩小范围。" },
    review_reason: { ...string, description: "用户确认过的边界：写清用户为什么只要这一段（例如「上半段已经在平台做过，这次从复核导出的结果开始」），会写进沉淀报告。" },
    execution_scope: executionScope,
    validation_params: { type: "object", additionalProperties: true, description: "必填的最小验证回放参数。必须和 execution_scope.mode=sample|subset 同时出现；没有它时计划不可确认，必须先补充真实小样并重新生成计划。" },
  },
  run: { execution_scope: executionScope },
};

const distillTool = tools.find(([action]) => action === "distill");
distillTool[1] =
  "Prepare a structural distillation plan from completed Pi traces. First call it with label, "
  + "source_traces, params/validation scope and no steps: the plugin mechanically builds a candidate "
  + "chain from artifact producers and consumers, excludes unreferenced branches, and returns the "
  + "proposed steps, human gates, tool receipts and ambiguities. Do not manually write the whole graph. "
  + "Only submit steps when correcting a reported ambiguity. Planning does not read script bytes, "
  + "materialize scripts, compile a graph or replay. Show the returned review to the user and ask for "
  + "confirmation; then call genome_confirm_distill with the same plan_id. Confirmation performs one "
  + "complete preflight and queues only the approved bounded sample/subset replay. "
  + "Publication waits for replay success.";
const confirmDistillTool = tools.find(([action]) => action === "confirm_distill");
confirmDistillTool[1] =
  "Confirm exactly one reviewed distillation plan and queue its bounded sample/subset replay. "
  + "The plan must have review.validation.ready=true. Confirmation runs one complete preflight, "
  + "creates the capture, and starts the replay; the asset is published only after that replay succeeds. "
  + "Never confirm a plan missing validation_params or using full/unspecified as validation scope; "
  + "regenerate the plan after the user supplies a real bounded sample. Confirmation is idempotent.";
const captureTool = tools.find(([action]) => action === "capture");
captureTool[1] =
  "Advanced legacy direct capture. Do not use it for a normal 沉淀一下 request because it has no reviewable plan. Use genome_distill -> show plan -> genome_confirm_distill. "
  + captureTool[1];
const runTool = tools.find(([action]) => action === "run");
runTool[1] = "Compatibility alias for genome_prepare_run when reusing an asset: it prepares a plan, does not start execution, and the caller must show the analysis and obtain explicit confirmation before genome_confirm_run. It does not accept plan_id; use genome_confirm_run for confirmation. " + runTool[1];

// The graph only ever replays nodes the host can execute, so the prompt teaches the
// agent to reshape a chain instead of declaring the parts it could not solidify.
const guidance = [
  "历史经验是用户自己 storage 里的 graph + scripts，不是业务工作流 DSL。需要复用或执行时先 genome_list，再用 applies_when / not_applies_when / input_contract 对照真实输入；名称相似不算匹配，选中后再 genome_get。闲聊或无需执行的问答不必查询。",
  "复用只走 genome_prepare_run → 展示实际输入、范围、节点、副作用和产物 → 用户确认同一 plan_id → genome_confirm_run。带路径输入而用户没说范围时必须问 full / sample / subset，不能默认全量；输入或范围变化就重新 prepare。genome_run 只是兼容别名。graph 内的 llm / human 节点按节点契约执行，不要手工跑等价命令替代 graph；不适用时走 revision 或 takeover。",
  "只有用户明确说「沉淀一下」「固化这条链路」才用 genome_trace 选来源并调用 genome_distill。第一次调用必须省略 steps，只读取已有 trace，让插件机械生成候选链路；不要为了回忆重读整段会话、手写完整 graph，或为了让链路看起来完整而重跑历史命令。候选计划会返回步骤、根输入、旁支和少量 ambiguities。计划阶段不读脚本内容、不物化脚本、不建包、不编译 graph、不回放。",
  "先展示候选计划给用户确认链路范围；插件报 ambiguity 时，只针对被点名的节点提交完整修正 steps，未点名的候选节点保持不变。链路范围按数据流判断：某步产物被后续读取，或它读取了链路上游产物，才进入候选；没人消费的探测、本地试跑小样和失败尝试是旁支，不进 steps，也不用问用户；试跑与正式执行共用脚本不算相连。人工交接是链路步骤而不是根输入；范围确实有歧义、且会明显改变长链路或跨人工门禁的执行时，先用一句具体问题让用户选两个范围。插件报「链路范围需要用户复核」时，把点名步骤原样给用户，确认后才带 review_confirmed=true 和 review_reason 重提。",
  "沉淀默认覆盖从根输入到最终产物的整条链路。用户说「沉淀一下」时，指的就是他刚跑完的那条流程（平台提交 → 等待 → 中间产物 → 导入打标 → 人工复核 → 最终产物），不是最好写的那一步。只有用户明确说「只沉淀 XX 这一段」才可以截取；不要自行缩小范围、拆成多次沉淀，也不要因中间某步难写就只沉淀尾段。确实要拆时先说明拆哪里、为什么，等用户点头。",
  "只有五类节点：固定命令→run，固定宿主工具调用→tool，异步等待→await，人工门禁→human，真正的 agent 裁量→llm。整条链路必须完整固化，只收完整固化步骤；有副作用但产物无人消费的步骤加 keep: true。llm 节点的 prompt 必须写明原会话使用的 skill、命令形态和输入输出路径，并尽量提供格式/字段校验。真实跑过的固定 shell 命令即使内部调用 LLM 也必须用 run。",
  "异步工具写成 tool + await：tool 保存任务句柄并 keep: true，await 读这张句柄，由宿主探测任务终态、把真实产物落成 artifact；不要写轮询脚本（`wait_pipeline.py`、`poll_task.py` 已废弃，写了也会被改写成 await）。判据是**数据流**——那个 await 步骤消费了受理单。缺这个等待步骤的链路会在沉淀校验阶段直接被拒绝。平台提交节点回放时会先停在 danger 审批门禁，用 genome_resume 批准后才真正提交。Label Studio 导入等固定工具调用用 tool，人工复核/导出用 human；human 节点停下后用 genome_resume 唤醒同一条 run，不能伪造人工产物。",
  "路径一律写相对工作区的形式：`public_data/…`、`scripts/…`、`storage/datasets/…`。不要写沙箱挂载前缀 `/target-workspace/…`、`/workspace/…`，也不要写 `/storage/…`——挂载前缀是执行环境的临时拼写，换个挂载就不存在；`/storage/…` 平台按对象路径读，认不出来。校验阶段会直接拒绝这两种写法。",
  "沉淀时发现脚本硬编码路径、不可重跑，是正常现象：把脚本改成接受命令行参数、把产物声明成该步的 output，然后继续沉淀整条链路。不要因为「这个脚本要改」就判定链路沉淀不了。",
  "从 Storage 里的清单切子清单时，必须写回**同一个 Storage 目录**（如 `storage/datasets/<dir>/prelabel_subset.jsonl`），不要写进运行目录：清单里的图片按所在目录解析，换目录下游就找不到图，提交前预检会失败。真要写进运行目录，脚本必须把图片一并复制或改写成绝对路径。",
  "改造脚本用 edit 做局部替换（换掉硬编码常量、接上 argparse 参数），不要整文件重写：十几 KB 的脚本要写几分钟，断流就白做；单次写入超过 4KB 就拆成几次 edit。",
  "占位符 `{参数名}` 只用于链路开始前已存在的根输入；会话中固定命令或人工节点产生的产物必须由对应步骤产出。指向工作区脚本的占位符会在确认时随包物化，helper `.py/.sh` 一并搬运；编辑工具写出的其他业务产物要改成脚本产出或根输入边界。同时声明 applies_when、not_applies_when、input_contract、output_contract 和 final_artifact，最终节点要校验格式与必需字段。",
  "计划必须包含 validation_params 和 execution_scope.mode=sample|subset；full/unspecified 不能作为自动验证范围。向用户展示计划并确认后，genome_confirm_distill 只做一次机械 preflight，把问题一次全列出来；预检失败时不会创建 capture，修完计划后重新确认同一 plan_id。工具返回可定位的错误时，本回合必须修正并立即重新调用对应工具，不能只说「准备重新提交」就结束；只有需要用户决策时才停。通过后创建 capture 并排队有界验证回放，成功才发布。",
  "泛化回放 B 暂时不做：先把主轨迹沉淀跑通，不要把 new_params 当成必填项，也不要为了凑泛化证据去造第二组输入。只有用户明确要求「验证换参数还能跑」时才给 new_params。",
  "人工节点停下时把工作单和上游产物里的 open_url / 项目链接转给用户，图内 artifact 只作为回执位置；用户用 genome_resume 的 artifact 传导出文件。回放失败先分类：宿主/基础设施失败用 genome_replay_requeue；图内脚本错误则修好后重新 genome_distill；waiting、queued、running 不算失败。",
];

// trace reads Pi's own session manager; capture hands the Python service a file reference
// instead of the whole history, because host RPC frames are size limited.
async function dispatch(host, action, args, callId, signal, ctx) {
  if (action === "trace") return { sessions: await listSessions(ctx) };
  if (action === "capture") {
    const refs = await exportTraces(ctx, args.trace_ids, { complete: true });
    if (!refs.length) throw new Error("genome_capture needs trace ids from genome_trace");
    const { trace_ids: _ignored, ...rest } = args;
    return host.invoke(action, { ...rest, trace_refs: refs }, callId, signal, ctx);
  }
  // The source session ids double as the completeness check's evidence: export them
  // the same way capture does, so the plugin can tell a full chain from a truncated one.
  if (action === "distill" && Array.isArray(args.source_traces) && args.source_traces.length) {
    const refs = await exportTraces(ctx, args.source_traces, { complete: true });
    return host.invoke(action, { ...args, trace_refs: refs }, callId, signal, ctx);
  }
  return host.invoke(action, args, callId, signal, ctx);
}

export function createGenomeExtension(host) {
  return (pi) => {
    pi.on("before_agent_start", (event) => ({
      systemPrompt: event.systemPrompt + "\n\n" + guidance.join("\n"),
    }));
    for (const [action, description, properties, required] of tools) {
      pi.registerTool({
        name: `genome_${action}`, label: `历史经验 ${action}`, description,
        parameters: { type: "object", properties: { ...properties, ...extraFields[action] },
          required, additionalProperties: false },
        async execute(callId, args, signal, _onUpdate, ctx) {
          const result = await dispatch(host, action, args, callId, signal, ctx);
          return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
        },
      });
    }
  };
}

export default function agentGenome(pi) {
  const host = discoverGenomeHost(pi.events);
  createGenomeExtension(host ?? nativeHost(pi))(pi);
}
