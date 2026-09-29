import type { InlineExtension } from "@earendil-works/pi-coding-agent";
export interface GenomeHost {
  invoke(action: string, args: Record<string, unknown>, callId: string, signal?: AbortSignal): Promise<unknown>;
}
export declare function createGenomeExtension(host: GenomeHost): InlineExtension;
export default function standalone(): never;
