// Synchronous capability discovery on Pi's standard, session-scoped event bus.
// Providers register before extensions load. No globals or Pi patches are used.
export const GENOME_HOST_CHANNEL = "agentgenome:host:v1";

export function createBridgeHost(request) {
  return { invoke: (action, args, callId, signal) => request("workflow.invoke", {
    action, arguments: JSON.parse(JSON.stringify(args)), request_id: callId,
  }, signal) };
}

export function provideGenomeHost(events, request) {
  let discoveries = 0;
  const dispose = events.on(GENOME_HOST_CHANNEL, (query) => {
    if (query?.version !== 1 || !Array.isArray(query.providers)) return;
    query.providers.push(createBridgeHost(request));
    discoveries++;
  });
  return { dispose, get discovered() { return discoveries > 0; } };
}

export function discoverGenomeHost(events) {
  const query = { version: 1, providers: [] };
  events.emit(GENOME_HOST_CHANNEL, query);
  if (query.providers.length > 1) throw new Error("Multiple AgentGenome execution hosts registered");
  const host = query.providers[0];
  if (host !== undefined && typeof host?.invoke !== "function") throw new Error("Invalid AgentGenome execution host");
  return host;
}
