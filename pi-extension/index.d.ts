import type { ExtensionAPI, ExtensionContext, InlineExtension } from "@earendil-works/pi-coding-agent";
export interface GenomeHost {
  invoke(action: string, args: Record<string, unknown>, callId: string, signal?: AbortSignal, context?: ExtensionContext): Promise<unknown>;
}
export declare function createGenomeExtension(host: GenomeHost): InlineExtension;
export declare function createBridgeHost(request: (method: string, payload: Record<string, any>, signal?: AbortSignal) => Promise<unknown>): GenomeHost;
export declare const GENOME_HOST_CHANNEL: "agentgenome:host:v1";
export interface GenomeEventBus {
  emit(channel: string, data: unknown): void;
  on(channel: string, handler: (data: unknown) => void): () => void;
}
export declare function provideGenomeHost(events: GenomeEventBus, request: (method: string, payload: Record<string, any>, signal?: AbortSignal) => Promise<unknown>): {
  dispose(): void;
  readonly discovered: boolean;
};
export default function agentGenome(pi: ExtensionAPI): void;
