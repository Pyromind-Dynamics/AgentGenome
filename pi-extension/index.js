import { nativeHost } from "./native-extension.js";
import { discoverGenomeHost } from "./host-discovery.js";
export { GENOME_HOST_CHANNEL, provideGenomeHost, createBridgeHost } from "./host-discovery.js";

const string = { type: "string", minLength: 1 };
const asset = { asset_id: string, version: string };
const paths = { type: "array", items: string };
const cases = { type: "array", description: "Small synthetic regression cases. Each has unique id, files (relative path to text), params (use fixture:filename for inputs), and assertions [{node: full graph node path, output: output key, kind: json|csv|text, expected: JSON subset / complete CSV row arrays including the header / exact text}]. Preserve inherited cases. Never copy private business datasets into shared fixtures.", items: { type: "object", additionalProperties: true } };
const tools = [
  ["list", "List latest published historical experiences (历史经验): reusable graphs with scripts and optional Agent stages. For execution tasks, check these before choosing a Skill; compare applicability with genome_get.", {}, []],
  ["get", "Read a historical experience graph's inputs, expected outputs, applicability and revision_availability before reusing it.", asset, ["asset_id", "version"]],
  ["run", "Reuse a historical experience graph with asset_id + version + params, OR revision_id + params + change_summary, never both (version is optional for a revision candidate). Changed scripts/interfaces require baseline_cases when no inherited cases exist, and regression_cases covering the new scenario. Old and new scenarios must work; do not hardcode the new input. Passing all cases publishes the candidate as latest unless latest changed concurrently. Parameter-only changes do not publish. Each revision starts from the beginning. There is no revision-count limit; use the latest run ID for further revisions and decide yourself when genome_takeover is more appropriate. Preserve input paths exactly as verified with host file tools, including any path prefix. Returns a run ID immediately. Execution continues in the background after this turn yields; completion automatically returns results to you for a follow-up turn. Tell the user it was submitted, not completed. End this turn without polling; do not also execute its scripts yourself.", { ...asset, revision_id: string, change_summary: string, baseline_cases: cases, regression_cases: cases, parameter_patch: { type: "object", description: "Optional declarations and bindings limited to the revision policy; never verification or graph structure.", additionalProperties: true }, params: { type: "object", additionalProperties: true } }, ["params"]],
  ["prepare_revision", "Prepare a further revision from the latest frozen scripts when genome_status recovery.revision.available is true. For a succeeded run whose results do not satisfy the task, reason is required. 只修改本次副本中允许的业务脚本和参数。不要修改验收脚本、验收提示词、阈值、graph 验收规则或伪造验收结果。无法在保持验收标准的前提下完成适配时，调用 genome_takeover。 Legacy fixed script packages allow revision by default: provide editable_scripts and verification_resources declarations. Keep old behavior working and add synthetic regression cases for new behavior. Returned baseline_cases and pending_regression_cases preserve unvalidated examples from the preceding attempt; fix draft mistakes if needed, but never weaken already frozen regression_cases. Host protection may be prompt-only; follow these constraints regardless.", { run_id: string, reason: string, editable_scripts: paths, verification_resources: paths }, ["run_id"]],
  ["takeover", "Hand back the remaining task after a historical experience has stopped, including a succeeded run whose outputs do not satisfy the user. Give the reason and use available outputs with business Skills. This preserves the original execution status; it does not rerun the graph or require revision support. Choose takeover when further revision is not appropriate; failed revisions can be revised again. Do not automatically return to the old graph.", { run_id: string, reason: string }, ["run_id", "reason"]],
  ["step_result", "Write the declared stage outputs, then submit this receipt and end the turn. This only records your result; AgentGenome verifies files after this exact turn ends. Never start another graph from a graph stage.", { request_id: string, outcome: { type: "string", enum: ["completed", "failed"] }, summary: string }, ["request_id", "outcome", "summary"]],
  ["status", "Read progress, results and recovery availability of a historical experience run in this conversation. A succeeded status means the recorded checks passed; still assess whether the outputs satisfy the user.", { run_id: string }, ["run_id"]],
  ["cancel", "Request cancellation of a historical experience run in this conversation.", { run_id: string }, ["run_id"]],
];

export function createGenomeExtension(host) {
  return (pi) => {
    pi.on("before_agent_start", (event) => ({
      systemPrompt: event.systemPrompt + "\n\n" +
        "历史经验（AgentGenome graph）和 Skill 都是可复用的经验。对于需要执行的任务，先用 genome_list 查看历史经验，并用 genome_get 确认候选的适用范围、输入和预期产物；适用则复用，否则再选择 Skill。闲聊或无需执行的问答不必查询。历史经验是已沉淀的 graph + scripts，不是业务平台的工作流或 DSL。复用仍须遵守用户授权和业务约束，不能仅凭名称相似就执行。",
    }));
    for (const [action, description, properties, required] of tools) {
      pi.registerTool({
        name: `genome_${action}`, label: `历史经验 ${action}`, description,
        parameters: { type: "object", properties, required, additionalProperties: false },
        async execute(callId, args, signal, _onUpdate, ctx) {
          const result = await host.invoke(action, args, callId, signal, ctx);
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
