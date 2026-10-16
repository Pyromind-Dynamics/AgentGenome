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
  input_contract: { type: "object", additionalProperties: true, description: "根输入契约：参数名 → {format, required_fields/fields, description}，例如 {data_file: {format: \"csv\", required_fields: [\"qid\", \"question_text\"]}}。复用方据此判断能否直接跑、是否需要先桥接中间产物。" },
  output_contract: { type: "object", additionalProperties: true, description: "产出契约：与 input_contract 同构，写明格式与字段，便于复用方核对结果。" },
  determinism: { type: "string", enum: ["script", "agent-assisted", "human-gated"], description: "复现这条链路需要什么：script=全链路固定调用（含脚本内部调用 LLM，调用形式固定）；agent-assisted=图里有 llm 节点，回放时该步骤交回 agent 执行；human-gated=图里有 human 节点，回放会停下等人。默认按图里的节点种类推导（human 优先于 llm，llm 优先于 script），只有确实要覆盖推导结果时才显式声明。" },
};
const policy = { type: "string", enum: ["scripts-first", "adaptive-first"], description: "固化策略，默认 scripts-first。scripts-first：llm 节点必须用 from 指向会话里真正来源的步骤，插件据此核对它不是固定命令、人工门禁或编辑工具写入，防止把固定命令伪装成 llm 节点。adaptive-first：信任 agent 的判断，from 可选、只作审计。" };
const steps = {
  type: "array",
  description: "规范化链路，按执行顺序。每步是 run / llm / tool / human 之一：run（完整 shell 命令，输入产出在命令行上）、llm（{llm: 提示词模板, output: 产物路径, input: [输入路径], verify?}，节点编译成 do: {llm}，由 genome_run 驱动 agent 重跑）、tool（{tool: 宿主工具名, args: {...}, output: 结果 JSON 落到哪个 artifact}，由 genome_run 直接调用宿主工具）、human（{human: 交给人的工作单模板, output: 人工结果必须写入的 artifact 路径, input?: [输入路径]}，图跑到这里停下等人，用户做完后 genome_resume 唤醒）。链路只收完整固化的图：固不下来的步骤不会被发布，固定命令改用 run，固定工具调用改用 tool，人工门禁改用 human，真正的 agent 裁量改用 llm，编辑工具写出的产物改成脚本产出或声明成 `{参数名}` 输入边界。llm 提示词除原样包含声明路径外，还必须写明该步骤原本由哪个 skill、以什么命令形态执行；from：llm 步骤在默认策略下必填，标注真正来源的 `<trace_id>:<seq>`；来源为固定命令（改用 run）、人工门禁（改用 human）或编辑写入（改成脚本产出或输入边界）都会被直接拒绝。",
  items: { type: "object", properties: {
    run: string,
    llm: string,
    human: { ...string, description: "仅 human 步骤：交给人的工作单模板（例如「请到 Label Studio 复核 {input.prelabeled}，把导出的复核结果写到 {artifact}」）。必须原样包含 input/output 里的每个路径。" },
    tool: { ...string, description: "仅 tool 步骤：宿主工具名，例如 df_submit_pipeline。" },
    args: { type: "object", additionalProperties: true, description: "仅 tool 步骤：传给工具的参数；字符串值里的路径会变成 graph 输入槽位，声明的 input 必须出现在这里。" },
    input: { ...paths, description: "仅 llm/tool/human 步骤：它消费的输入路径列表（`{参数名}` 或链路内更早步骤产出的字面路径）。llm/human 模板里必须原样出现；tool 步骤必须出现在 args 里。" },
    output: string,
    verify: string,
    from: string,
  }, additionalProperties: false },
};
const tools = [
  ["list", "List latest published historical experiences (历史经验): reusable graphs with scripts and optional agent/human stages. For execution tasks, check these before choosing a Skill. Each entry carries applies_when / not_applies_when / input_contract / determinism when declared, plus a has_samples flag; use genome_get for the full contract and the sampled inputs.", {}, []],
  ["trace", "List recent Pi sessions in this workspace so the user can pick which runs to capture into a historical experience. Read-only. Call it before proposing capture, and never capture a session the user did not ask for.", {}, []],
  ["capture", "Sink the runs the user explicitly asked to 沉淀 into one reusable historical experience: pass the trace ids returned by genome_trace plus label (经验名, shown to the user). Call this only when the user asks to 沉淀/固化 this chain; never on your own initiative. Everything must already be on disk: inline code and heredocs cannot be solidified. Optional asset_id is the reuse handle (ASCII slug); a non-ASCII label degrades to capture-<hash>. Optional final_artifact reverse-slices the graph to what the chain must produce; version pins the released version; description, owner and min_support tune the report. 链路只要有根输入就必须给 new_params（与 params 同键、值换成另一组真实输入，或按 input_contract 造的小样本），插件据此跑回放 B 证明链路没把本次输入写死；缺了它只能发布成未经泛化验证的窄口径经验，报告会写明。Declare applies_when / not_applies_when / input_contract so later reuse can match precisely. 只收完整固化的链路：每步都必须是图能重跑的固定命令、宿主工具调用、llm 节点或 human 节点；有步骤固不下来时机械固化会失败并列出原因，改用 genome_distill 提交规范化链路。Returns a capture id immediately; the replay continues in the background and its result comes back to you automatically.", { trace_ids: { ...paths, description: "Session ids from genome_trace, in the order the chain ran." }, label: string, asset_id: string, final_artifact: string, new_params: { type: "object", additionalProperties: true, description: "换参数回放的参数：与 params 同键、值换成另一组真实输入（或按 input_contract 造的小样本）。链路有根输入时必给；与原参数相同不算泛化证据。" }, min_support: { type: "number" }, version: string, description: string, owner: string, policy, ...matching }, ["trace_ids", "label"]],
  ["distill", "沉淀一条真实跑通、但命令本身不干净的链路：当会话里的步骤是内联代码（python3 -c、heredoc）、只读探测（ls/head/find、2>/dev/null）或由脚本内部写出的产物时，机械固化会失败。你先在磁盘上补全缺失的脚本，再用本工具提交规范化链路，它会校验契约、编译成 graph，并立刻排队一次回放验证（与 genome_capture 相同，结果自动回来）。只收完整固化的链路：每步都必须是 run / llm / tool / human 之一，固不下来的步骤不会被发布。steps 按链路顺序排列，每步是下面四种之一：(1) run 步骤——完整 shell 命令，输入与产出路径必须出现在命令行上（`> 产出`、`--out 产出`、`python3 脚本.py 输入 > 产出`）；脚本必须已经存在且路径写在命令行上；产出必须显式（`>`、`--out`、`--output` 之类）：`python3 脚本.py 输入 产出` 的位置参数一律按输入识别；不要提交探测命令，不要提交 `2>/dev/null` 的步骤。(2) llm 步骤——由 agent 自由裁量完成、但确实属于这条链路的步骤（例如「过滤恶意问题」「生成回答」）：`llm` 是提示词模板，`output` 声明它产出的 artifact 路径，`input` 声明它消费的输入路径列表（可省略）；提示词里必须原样出现 input/output 里写的每一个路径，emit 会把它们替换成 `{input.*}` / `{artifact}` 槽位，节点编译成 `do: {llm}`，由 genome_run 驱动 agent 重跑。可选 verify 覆盖默认的 `test -s`（可用 `{artifact}`、`{input.*}`）；llm 节点的 verify 尽量写能识别错误产出的检查（行数、字段、格式），默认的 `test -s` 只能证明文件非空。llm 步骤的 prompt 必须保留这一步的原始执行方式：原会话由哪个 skill 完成，就点名该 skill 和它的命令形态（脚本、--pipeline、模型 profile 等），回放时 agent 才会沿同一条路径执行；只写任务目标会让回放绕开 skill 自行硬做。只有真正的 agent 自由裁量才用 llm 步骤：一条固定 shell 命令（哪怕脚本内部调用 LLM、每次产出的文本都不同）必须用 run，人工门禁必须用 human 步骤，编辑工具产物必须改成脚本产出或声明成 `{参数名}` 输入边界，插件会按 source_traces 校验并拒绝错判。(3) tool 步骤——宿主工具调用（平台提交 df_submit_pipeline、Label Studio 建项目这类固定调用）：tool 是宿主工具名，args 是它收到的参数（字符串里的路径会变成 graph 输入槽位），output 声明工具返回的 JSON 写到哪个 artifact；节点编译成 do: {tool: {name, args}}，由 genome_run 直接调用宿主工具重跑，没有模型在环。会话里真实跑过的固定工具调用必须用 tool 步骤提交，不能包装成 llm 节点。(4) human 步骤——人做的那一步，agent 不能替他做（Label Studio 人工复核、人工导出这类）：`human` 是交给人的工作单模板（写清要做什么），`output` 声明人工结果必须落到哪个 artifact 路径，`input` 声明它消费的输入（可省略），工作单里必须原样出现这些路径（emit 会把它们换成 {input.*} / {artifact} 槽位）。节点编译成 `do: {human: ...}`：genome_run 跑到这里会停下等人，图会给出 node / question / artifact，用户做完人工工作、把结果写进那个 artifact 后，调用 genome_resume 唤醒同一条 run，回放从下一个节点继续。人工门禁必须用 human 步骤提交，不能包装成 llm 节点；agent 不得替用户伪造人工产物。from 是 llm 步骤在 scripts-first（默认）下的必填项：填写该步骤真正来源的 `<trace_id>:<seq>`，插件据此核对它不是固定命令；来源是固定命令会被拒绝（改用 run），来源是人工门禁会被拒绝（改用 human），来源是编辑写入会被拒绝（改成脚本产出或输入边界）。adaptive-first 下 from 可选，仅作审计。链路必须完整：某一步读取的产物如果在本会话里由更早的步骤产出，而那个步骤是固定 shell 命令（哪怕脚本内部调用 LLM），就必须把那一步补进 steps；只有链路开始前就存在的输入（用户一开始给的数据、链路外的既有资产）、编辑工具写出的脚本或纯 agent 产物才允许写成 `{参数名}` 占位符并用 params 声明为输入边界；会话中途用户自己做完并交回的产物（「我已经人工复核完导出了 …」）不是输入边界，它是 human 节点的产出，必须把人工门禁写成 human 步骤（input=会话为人工准备的材料，output=用户交回的产物），上游固定步骤一并补进 steps。把固定命令产出的路径写成占位符会被校验直接拒绝。如果某个 `{参数名}` 指向的就是你写进工作区的脚本（.py/.sh 等），插件会把脚本随包搬运、把命令改写成具体路径并把它从 params 里去掉（报告写「脚本参数已物化」）：这是脚本被固化的正常途径，不要为了绕开「编辑工具写入的产物无法固化」而要求复用方自带脚本。脚本在运行目录里按文件名调用的同目录辅助文件（.py/.sh）会一起随包搬运，不需要你为每个 helper 再声明参数。source_traces 传 genome_trace 给出的会话 id，插件据此校验完整性；params 必须覆盖链路的根输入（命令里的 `{参数名}` 或由字面路径派生的输入名），也可以一并给出链路中间产物的占位符绑定，它的值就是回放 A 使用的真实输入；链路有根输入时还必须给 new_params（换一组真实输入或按 input_contract 造的小样本）跑回放 B，否则这条经验只能窄口径复用。同时声明 applies_when / not_applies_when / input_contract，让后续复用能精确匹配。", { steps, label: string, asset_id: string, final_artifact: string, source_traces: { ...paths, description: "genome_trace 给出的会话 id，按链路顺序。插件导出这些会话，用来校验链路完整性（由固定命令产出的上游产物必须来自链路内步骤；只有链路开始前就存在的输入、编辑工具写出的脚本、纯 agent 产物才允许作为显式输入边界；会话中途人工交回的产物必须写成 human 步骤，不能当根输入）。" }, params: { type: "object", additionalProperties: true, description: "根输入的真实值：键与命令里的 `{参数名}`（或由路径派生的根输入名）一致，值是回放 A 使用的真实路径。命令里出现占位符时必须给出，否则 distill 直接报错；链路中间产物的 `{参数名}` 也可以一并给出，它们不是根输入、不进 graph 参数。指向工作区脚本的参数会被物化进包并从 params 去掉。" }, new_params: { type: "object", additionalProperties: true, description: "换参数回放的参数：与 params 同键、值换成另一组真实输入（或按 input_contract 造的小样本）。链路有根输入时必给；与原参数相同不算泛化证据。" }, min_support: { type: "number" }, version: string, description: string, owner: string, policy, ...matching }, ["steps", "label"]],
  ["capture_status", "Read the stage, report, replay verdict, published version or draft location of a capture started with genome_capture.", { capture_id: string }, ["capture_id"]],
  ["get", "Read a historical experience's inputs, expected outputs, applicability and revision_availability before reusing it. Compare applies_when / not_applies_when / input_contract with the real inputs first. input_samples 是沉淀时对该链路根输入做的有界采样（每个参数的文件头/schema 与文本预览），用来核对本次输入是否与当时一致——它只是证据，不是要处理的真实数据。graph 覆盖的确定性步骤一律交给 genome_run 执行：不要用文件工具重写 graph 里的脚本，也不要手工跑等价命令来替代它；llm 节点会在回放时交回给 agent，human 节点会停下等人。", asset, ["asset_id", "version"]],
  ["run", "Reuse a historical experience graph with asset_id + version + params, OR revision_id + params + change_summary, never both (version is optional for a revision candidate). 复用已发布资产前，插件会用沉淀采样的 schema/required_fields 机械核对本次根输入：格式或必需字段不符会直接拒绝并说明差在哪，避免把经验套到不匹配的数据上；确实要按现状强行复用才带 allow_mismatch=true（会被记录为 forced 并在结果里标出）。 这是执行 graph 里那部分确定性步骤的唯一通道：不要为了「顺手」用 terminal 重跑等价命令，也不要用 write/edit 重写 graph 已经固化的脚本（哪怕你觉得能写得更好）。脚本确实不适用时走 revision_id 通道（change_summary + baseline_cases/regression_cases），不要绕开 graph 在会话里重写。Changed scripts/interfaces require baseline_cases when no inherited cases exist, and regression_cases covering the new scenario. Old and new scenarios must work; do not hardcode the new input. Passing all cases publishes the candidate as latest unless latest changed concurrently. Parameter-only changes do not publish. Each revision starts from the beginning. There is no revision-count limit; use the latest run ID for further revisions and decide yourself when genome_takeover is more appropriate. Preserve input paths exactly as verified with host file tools, including any path prefix. Returns a run ID immediately. Execution continues in the background after this turn yields; completion automatically returns results to you for a follow-up turn. Tell the user it was submitted, not completed. End this turn without polling; do not also execute its scripts yourself.", { ...asset, revision_id: string, change_summary: string, baseline_cases: cases, regression_cases: cases, parameter_patch: { type: "object", description: "Optional declarations and bindings limited to the revision policy; never verification or graph structure.", additionalProperties: true }, allow_mismatch: { type: "boolean", description: "Only with asset_id reuse: run even though the input contract check failed. Records the drift as forced instead of stopping." }, params: { type: "object", additionalProperties: true } }, ["params"]],
  ["resume", "继续一条停在人工节点（do: {human}）上的历史经验 run：回放跑到 human 节点会停下等人，把 node / question / artifact 告诉用户；用户完成人工步骤（例如 Label Studio 复核导出）并把结果写进那个 artifact 后，用同一个 run_id 调用本工具唤醒它，回放从停下的节点继续，不会再问一遍已完成的人工步骤。run 不在 waiting 状态时只返回当前状态，不会重跑。不要自己执行或重放 graph，也不要替用户伪造人工产物。", { run_id: string }, ["run_id"]],
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
  },
};

// The graph only ever replays nodes the host can execute, so the prompt teaches the
// agent to reshape a chain instead of declaring the parts it could not solidify.
const guidance = [
  "历史经验（AgentGenome graph）是沉淀在用户自己 storage 里的可复用资产，只能在运行时用 genome_* 工具发现，不要假设有哪些资产；它是已沉淀的 graph + scripts，不是业务平台的工作流或 DSL。",
  "需要执行的任务（包括用户说「按上次的流程」「复用之前沉淀的链路」）先 genome_list，再用 applies_when / not_applies_when / input_contract 对照当前真实输入（先用文件工具确认格式）；不要仅凭名称相似复用。选中候选后 genome_get 读完整契约。",
  "graph 覆盖的确定性步骤只能交给 genome_run 执行：不要用文件工具重写 graph 里的脚本，也不要手工跑一遍等价命令来「替代」graph；脚本确实不适用时走 genome_run 的 revision 通道（change_summary + baseline_cases/regression_cases），不要绕开 graph 在会话里重写。",
  "graph 里的 llm 节点会在回放时交回给你：按节点给出的提示词完成该步骤，把产物写到它声明的 artifact，再用 genome_step_result 交回，图会继续往下走；human 节点会停下等人：把 node / question / artifact 告诉用户，用户做完人工工作、把结果写进那个 artifact 后调用 genome_resume 唤醒同一条 run，不要替用户伪造人工产物，也不要把人工门禁当成自己的任务。",
  "输入不满足 input_contract 时，先按 Skill 正常执行或先桥接出缺失的中间产物，不要让用户重新挑资产。",
  "回放失败或结果不满足用户时：先看有没有更贴的历史经验，仍不行就走 genome_prepare_revision 修订；确实不适合继续修订（验收标准无法保持）时用 genome_takeover 交回任务，不要绕过 graph 在会话里重写。",
  "闲聊或无需执行的问答不必查询；复用仍须遵守用户授权和业务约束。",
  "用户明确说「沉淀一下」「固化这条链路」时，才用 genome_trace 选会话、genome_capture 沉淀；不要主动提议沉淀。",
  "用户说「沉淀一下」后：先按执行顺序列一张步骤表（一步一行：会话里的原始步骤 → 归类 run/tool/llm/human → 输入与产物路径），把本会话真实跑过的固定命令、固定工具调用、人工门禁（包括用户中途自己做完并交回产物的人工步骤，例如「我已经人工复核完导出了 …」）、agent 裁量都列进去（包括你后来改用脚本重跑过的版本），列完立刻按这张表提交 genome_capture 或 genome_distill；不要用散文反复权衡链路范围，不要因为某一步「上游已经做过」就把它排除在表外，也不要在提交前自己缩范围——范围问题由插件校验后带着具体步骤回来找用户确认。",
  "沉淀只收完整固化的链路：固定命令→run，固定宿主工具调用→tool，人工门禁→human，真正的 agent 裁量→llm。有 LLM 参与但调用形式固定的脚本步骤要作为可执行节点进图，不要以为它固不下来；固不下来的步骤不会被发布，先把它补全再提交。",
  "沉淀时把适用范围（applies_when/not_applies_when）、输入契约（input_contract）一起声明，后续复用才能精确匹配。",
  "沉淀带根输入的链路时必须给出 new_params（与 params 同键、换成另一组真实输入，或按 input_contract 造的小样本），插件用它跑回放 B 验证链路没有把本次输入写死；没有换参数回放的经验只能发布成未泛化的窄口径版本，报告会写明。",
  "链路里只要有内联代码（python3 -c、heredoc）、只读探测命令，或产物是脚本内部写出的，机械固化就不会通过：先把那段代码落盘成脚本，把输入产出写到命令行上，并在会话里用这条命令重跑一次（重跑让 trace 里留下可固化的命令，也顺带验证脚本本身跑得通），再用 genome_distill 提交规范化链路；命令本身已经干净的会话直接用 genome_capture。内联代码不是缩小链路范围的理由，缺哪段脚本就按这条补上。",
  "沉淀必须交完整链路：计划里某一步读取的产物如果在本会话里由更早的固定 shell 命令产出（哪怕脚本内部调用 LLM），就必须把那一步补进 steps；只有链路开始前就存在的输入（用户一开始给的原始数据、链路外的既有资产）、编辑工具写出的脚本、纯 agent 产物才允许用 `{参数名}` 占位符 + params 声明为输入边界。会话中途用户自己做完并交回的产物（「我已经人工复核完导出了 <path>」）不是输入边界：那是 human 节点的产出，要把人工门禁写成 human 步骤（input=会话为它准备的材料，output=用户交回的产物），并把上游固定步骤一并补进 steps。不要悄悄少交步骤，也不要拿占位符跳过固定命令，插件会按 source_traces 校验并直接报错。占位符指向的如果是你写进工作区的脚本，插件会把脚本随包搬运、把命令改写成具体路径并把它从 params 去掉（报告写「脚本参数已物化」），这是脚本被固化的正常途径，不要改成让复用方自带脚本；脚本按文件名调用的同目录辅助文件（.py/.sh）会一起随包搬运。物化后的脚本是随资产冻结的副本：skill 里后续的修复不会传播进已发布资产，需要更新时走 genome_run 的 revision 通道。",
  "沉淀的范围不能靠「找一个干净的根输入」来缩：人工交接（Label Studio 复核、人工导出、用户手工改完文件说「我弄好了，继续」）是链路里的一步，不是链路的起点；从人交回的产物开始沉淀等于把这一步从链路里吸收了。链路长、依赖平台配置（DF_*、VLM model profile、Label Studio 项目）都不是缩范围的理由——这些正是 tool / human 节点要固化下来的步骤；插件报「链路范围需要用户复核」时按那条规则处理，不要自己判定成旁支就缩范围。",
  "与链路不相连的旁支步骤（本地试跑小样、只写调试文件的探测、被后续步骤取代的尝试）不挡沉淀：插件把它们记进报告的旁支段，不要为了消掉它们把它们塞进 steps，也不要主动为它们缩范围；缩范围只有在插件报「链路范围需要用户复核」、用户明确确认之后才允许——插件点名了步骤（包括它认定成人工交接前半段的步骤）时，按上一条原样拿给用户看，不要自己判定成旁支。",
  "genome_distill 报「链路范围需要用户复核」时，说明会话里还有步骤不在这次链路里（产出过文件、接在链路后面，或在用户点名交回产物的那一步之前跑过）：把列出的步骤原样给用户看，问清这次是不是只要后半段；用户明确确认后才带 review_confirmed=true 与 review_reason 重新提交（确认过的边界会写进报告），用户没确认就不要缩小范围。人工交接的前半段被点名时，优先问用户「要不要把人工复核和它前面的步骤一起沉淀」，不要直接带确认重新提交。",
  "genome_distill 的 steps 里，真正的 agent 自由裁量步骤（例如「过滤恶意问题」「生成回答」这类没有固定命令的 LLM 步骤）可以声明成 llm 节点：{llm: 提示词模板, output: 产出路径, input: [输入路径], from: <trace_id>:<seq>}，节点会编译成 `do: {llm}` 进入 graph，由 genome_run 驱动 agent 重跑，不再当成需要人工补做的步骤。llm 节点的 prompt 必须把这一步的原始执行方式写进去：原会话用哪个 skill 完成的，就点名该 skill 和它的命令形态（脚本、--pipeline、模型 profile 等），让回放的 agent 沿同一条路径执行；只写任务目标会让回放绕开 skill 自己硬做。硬边界：一条真实跑过的固定 shell 命令（哪怕脚本内部调用 LLM、每次产出不同）必须用 run，不能写成 llm；人工门禁用 human 步骤；编辑工具产物必须改成脚本产出或声明成 `{参数名}` 输入边界。llm 节点在默认策略（scripts-first）下必须带 from 指向会话里真正来源的步骤，插件会按 source_traces 核对：来源是固定命令或人工/编辑步骤会直接报错并列出可选的会话步骤；只有确实没有固定调用形式的裁量步骤才配 llm。提示词里要原样出现 input/output 声明的每个路径，插件会替换成 `{input.*}` / `{artifact}` 槽位；llm 节点尽量给 verify（行数/字段/格式），默认 `test -s` 只能证明文件非空，识别不了错的产出。",
  "genome_distill 的 steps 里，会话中真实跑过的固定宿主工具调用（平台提交 df_submit_pipeline、Label Studio 导入建项目这类）用 tool 步骤提交：{tool: 工具名, args: {传给工具的参数}, output: 结果 JSON 落到哪个 artifact, from?}。args 里的路径会变成 graph 输入槽位（也可以用 input 显式声明，但必须出现在 args 里）；args 里指向脚本的参数可以写成 `{参数名}`，脚本会和 run 步骤的脚本一样随包搬运、参数从 params 去掉；节点编译成 `do: {tool: {name, args}}`，由 genome_run 直接调用宿主工具重跑，没有模型在环。把固定工具调用包装成 llm 节点会被校验拒绝。",
  "genome_distill 的 steps 里，人做的那一步（Label Studio 人工复核、人工导出这类）用 human 步骤提交：{human: 交给人的工作单模板, output: 人工结果必须写入的 artifact 路径, input?: [输入路径]}。工作单里必须原样出现 input/output 的路径；图跑到 human 节点会停下等人，把 node / question / artifact 告诉用户，用户做完后 genome_resume 唤醒同一条 run。不要替用户伪造人工产物。",
];

// trace reads Pi's own session manager; capture hands the Python service a file reference
// instead of the whole history, because host RPC frames are size limited.
async function dispatch(host, action, args, callId, signal, ctx) {
  if (action === "trace") return { sessions: await listSessions(ctx) };
  if (action === "capture") {
    const refs = await exportTraces(ctx, args.trace_ids);
    if (!refs.length) throw new Error("genome_capture needs trace ids from genome_trace");
    const { trace_ids: _ignored, ...rest } = args;
    return host.invoke(action, { ...rest, trace_refs: refs }, callId, signal, ctx);
  }
  // The source session ids double as the completeness check's evidence: export them
  // the same way capture does, so the plugin can tell a full chain from a truncated one.
  if (action === "distill" && Array.isArray(args.source_traces) && args.source_traces.length) {
    const refs = await exportTraces(ctx, args.source_traces);
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
