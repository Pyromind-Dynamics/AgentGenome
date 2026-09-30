import { nativeHost } from "./native-extension.js";
import { discoverGenomeHost } from "./host-discovery.js";
export { GENOME_HOST_CHANNEL, provideGenomeHost, createBridgeHost } from "./host-discovery.js";

const string = { type: "string", minLength: 1 };
const asset = { asset_id: string, version: string };
const tools = [
  ["list", "List latest published historical experiences (历史经验): reusable graphs with fixed scripts. For execution tasks, check these before choosing a Skill; compare applicability with genome_get.", {}, []],
  ["get", "Read a historical experience graph's inputs, expected outputs and applicability before reusing it.", asset, ["asset_id", "version"]],
  ["run", "Reuse a historical experience graph with explicit inputs. Preserve input paths exactly as verified with host file tools, including any path prefix. Returns a run ID immediately. Execution continues in the background after this turn yields; completion automatically returns results to you for a follow-up turn. Tell the user it was submitted, not completed. End this turn without polling; do not also execute its scripts yourself.", { ...asset, params: { type: "object", additionalProperties: true } }, ["asset_id", "version", "params"]],
  ["status", "Read progress and results of a historical experience run in this conversation.", { run_id: string }, ["run_id"]],
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
