/**
 * Typed client for the Typeflux control-plane HTTP API (#241/#242).
 *
 * Every contract type is generated from the normative OpenAPI contract
 * (contracts/controlplane/openapi.v1.json, #616) — servers conform to that
 * document, and CI fails if `src/schema.ts` drifts from it. This module is
 * glue only: it must never declare contract types by hand.
 */

import createClient from "openapi-fetch";

import type { paths } from "./schema";

export type { components, operations, paths } from "./schema";

export interface ControlPlaneClientOptions {
  /** Base URL of a `python -m typeflux.controlplane serve` instance. */
  baseUrl: string;
  /** Optional fetch implementation (defaults to the global fetch). */
  fetch?: typeof fetch;
  /** Optional headers sent with every request. */
  headers?: HeadersInit;
}

/**
 * Create a typed control-plane client.
 *
 * The returned client exposes `GET`/`POST` calls keyed by the API paths —
 * bundle (including the nodes+edges topology projection), catalog,
 * validate, discovery, the cross-version drain view (`/versions`), and the
 * start/status/review/cancel operations. Versioned workflow types, spec
 * digests, per-version drain counts, and per-execution valid review
 * decisions ride through verbatim; the client adds no version logic.
 */
export function createControlPlaneClient(options: ControlPlaneClientOptions) {
  return createClient<paths>({
    baseUrl: options.baseUrl,
    fetch: options.fetch,
    headers: options.headers,
  });
}

export type ControlPlaneClient = ReturnType<typeof createControlPlaneClient>;
