export async function executeNativeTool(executor, request, signal) {
  if (!executor || signal.aborted) {
    return { is_error: true, result: {}, text: "Tool not dispatched: context unavailable or cancelled" };
  }
  if (request.name.startsWith("genome_") || !executor.names().includes(request.name)) {
    return { is_error: true, result: {}, text: `Tool is not callable in this Pi session: ${request.name}` };
  }
  // Use the public tool pipeline, including validation, hooks and permissions.
  const outcome = await executor.execute(request.name, request.arguments, { signal });
  if (typeof outcome?.isError !== "boolean" || !outcome.result) throw new Error("Invalid Pi tool outcome");
  const raw = outcome.result;
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const result = object(raw.structuredContent) ? raw.structuredContent
    : object(raw.details) && Object.keys(raw.details).length ? raw.details : { content: raw.content || [] };
  const response = { is_error: outcome.isError, result,
    text: (raw.content || []).filter(block => block.type === "text").map(block => block.text).join("\n") };
  // Pi also wraps tool exceptions and result-hook failures as isError outcomes.
  // durationMs is present only when execution started; such errors cannot prove
  // that no side effect happened. Throw so the host records unknown and stops retries.
  if (outcome.isError && outcome.durationMs !== undefined) {
    throw new Error(`Pi tool ran but returned an error; outcome unknown, do not retry automatically: ${response.text.slice(0, 1000)}`);
  }
  if (Buffer.byteLength(JSON.stringify(response)) > 262144) throw new Error("Tool result exceeds limit; outcome unknown");
  return response;
}
