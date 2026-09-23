/**
 * A reference-style session-cache provider that records the whole lifecycle
 * (#478), so the example can PRINT the bracket the workflow drives:
 *
 *   prepareCachedSession  (once per fan-out, before any item)
 *     → structuredCall × N  (each per-item call threads the SAME handle)
 *   → releaseCachedSession  (once, after the fan-out)
 *
 * Reference style (Gemini-shaped) hands back a server-side `cache_id`; a
 * prefix-style provider (Anthropic/OpenAI) would instead mark cache_control
 * breakpoints with no id. Deterministic + offline — no network.
 */

import type { CachedSessionHandle, ModelProvider, StructuredCallParams } from "@typeflux/temporal";

export class RecordingReferenceCacheProvider implements ModelProvider {
  readonly providerName = "fake-reference";
  readonly supportsSessionCache = true;
  readonly sessionCacheStyle = "reference" as const;

  /** Observable lifecycle for the example to print. */
  readonly prepared: string[] = [];
  readonly perItemCacheIds: (string | null | undefined)[] = [];
  readonly released: (string | null)[] = [];

  prepareCachedSession(params: { identityHash: string; ttlSeconds?: number }): CachedSessionHandle {
    this.prepared.push(params.identityHash);
    return {
      provider: this.providerName,
      identity_hash: params.identityHash,
      supported: true,
      style: "reference",
      cache_id: "cachedContents/session-cache-demo",
      model: null,
      created_at: null,
      ttl_seconds: params.ttlSeconds ?? null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    };
  }

  releaseCachedSession(handle: CachedSessionHandle): void {
    this.released.push(handle.cache_id);
  }

  structuredCall(params: StructuredCallParams): unknown {
    // Each per-item call receives the fan-out's cached-session handle.
    this.perItemCacheIds.push(params.cachedSession?.cache_id ?? null);
    const rendered = params.messages.map((m) => (typeof m.content === "string" ? m.content : "")).join(" ");
    const id = /id:\s*(\S+)/.exec(rendered)?.[1] ?? "unknown";
    return { id, ok: true };
  }
}
