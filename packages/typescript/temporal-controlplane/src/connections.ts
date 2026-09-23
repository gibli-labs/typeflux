/**
 * Registry / observability CONNECTION status for a resolved workflow (governance parity, #563
 * slice 2b; Python `project/connections.py` `workflow_connections` + `WorkflowConnections`).
 * Reports the backends the workflow's YAML configures — types + hosts only, never credentials.
 *
 * Probe seam: reachability is inherently a live result, so the pure control plane takes an injected
 * `ConnectionProbe` (like `schemas` for the catalog). The default probe performs NO network I/O: a
 * non-langfuse backend (inline registry / observer "none") has nothing to reach → reachable; a
 * langfuse backend reports an honest un-probed status until a real probe is injected (#573).
 */

import type { TypefluxYamlSpec } from "@typeflux/temporal-yaml";

/**
 * What a probe is asked about: the backend kind, the host the spec configured (or null), and the
 * SELECTED environment id — so an injected probe can resolve per-environment connection settings
 * (e.g. a different `LANGFUSE_HOST` for prod vs dev), which Python does via `project_environment_context`.
 */
export interface ConnectionProbeInput {
  readonly type: string;
  readonly configuredHost: string | null;
  readonly environmentId: string;
}

/** A probe's verdict: whether the backend is reachable, the resolved host, and an optional detail. */
export interface ConnectionProbeResult {
  readonly reachable: boolean;
  readonly host?: string | null;
  readonly detail?: string;
}

/**
 * Probes one backend's reachability (Python `_probe`). ASYNC-capable: a real langfuse probe is a
 * live network round-trip (Python's `_langfuse_probe` lists traces), so the seam accepts a promise.
 * SHOULD degrade to `reachable: false` internally; a throw/rejection is caught + degraded anyway.
 */
export type ConnectionProbe = (input: ConnectionProbeInput) => ConnectionProbeResult | Promise<ConnectionProbeResult>;

/**
 * The default, NETWORK-FREE probe: non-langfuse backends have nothing to reach (reachable, no host,
 * matching Python's `_probe` for the non-langfuse branch); a langfuse backend can't be verified
 * without a real probe, so it reports an honest un-probed status (a deployment injects a real one).
 */
export const defaultConnectionProbe: ConnectionProbe = ({ type, configuredHost }) => {
  // Environment-independent: performs no network I/O, so `environmentId` is unused here.
  if (type !== "langfuse") {
    return { reachable: true, host: null };
  }
  return {
    reachable: false,
    host: configuredHost,
    detail: "langfuse reachability not probed (no connection probe configured)",
  };
};

/** A backend connection status (Python `ConnectionStatus`); `host`/`detail` omit when absent (exclude_none). */
export interface ApiConnectionStatus {
  type: string;
  host?: string;
  reachable: boolean;
  detail?: string;
}

/** The observability backend status (Python `ObserverStatus`) — a connection plus its manifest/redaction floors. */
export interface ApiObserverStatus extends ApiConnectionStatus {
  execution_manifest: boolean;
  redaction_enabled: boolean;
}

/** A resolved workflow's registry + observability connections (Python `WorkflowConnections`). */
export interface ApiWorkflowConnections {
  workflow_id: string;
  environment_id: string;
  registry: ApiConnectionStatus;
  observability: ApiObserverStatus;
}

/**
 * Strip any credentials a host string may carry before it enters the secret-free contract — the
 * userinfo (`user:pass@`) and query/fragment (either can hold a token).
 *
 * Uses the real WHATWG `URL` parser as the primary path: replicating its authority parsing in a regex
 * is a losing game (variable slash counts `https:/…`/`http:////…`, `\`→`/` normalization, special vs
 * opaque schemes), so we let the parser normalize + re-serialize credential-free. A side effect is
 * canonicalization (e.g. a trailing `/`); that is acceptable for a display field and stricter than
 * Python, which returns the raw host (and no credential stripping at all). The regex fallback only
 * runs for genuinely relative refs the parser rejects (`//host`, bare hostnames).
 */
export function sanitizeHost(host: string): string {
  // Cut at the first whitespace (a valid host has none) so a smuggled newline can't hide a second
  // credentialed segment (`host\nuser:pass@evil`) from the sanitizer.
  const singleLine = host.replace(/\s[\s\S]*$/, "");
  try {
    const url = new URL(singleLine);
    // Only trust the parser's credential fields when it found a real AUTHORITY. An OPAQUE-path URL
    // (`user:pass@host` with no `//`) parses as scheme + path, so `username`/`password` are empty and
    // `href` would re-emit the credential verbatim — fall through to the userinfo strip for that case.
    if (url.host !== "") {
      url.username = "";
      url.password = "";
      url.search = "";
      url.hash = "";
      return url.href;
    }
  } catch {
    // Not an absolute URL (bare hostname / protocol-relative) — handled by the strip below.
  }
  // Relative refs + opaque-path URLs: drop query/fragment, then strip userinfo greedily to the LAST
  // `@` before the path (`/` or `\`) so an embedded/duplicated `@` in the credential can't leave a fragment.
  const stripped = singleLine.replace(/[?#].*$/, "").replace(/^([/\\]*)[^/\\]*@/, "$1");
  // Unparseable AND still credential-suspect (`https://[bad/user:sk@x` fails the URL parser and
  // dodges the userinfo regex behind its `/`): mask whole rather than guess at the authority —
  // fail closed, same policy as the secret slots (Python matches, #600).
  return stripped.includes("@") ? "***" : stripped;
}

/** Run the probe for one backend and assemble its `ConnectionStatus` (host/detail omitted when null). */
async function probeConnection(
  type: string,
  configuredHost: string | null,
  environmentId: string,
  probe: ConnectionProbe,
): Promise<ApiConnectionStatus> {
  let result: ConnectionProbeResult;
  try {
    result = await probe({ type, configuredHost, environmentId });
  } catch {
    // Python `_probe` degrades ANY probe failure (DNS/auth/client error) to unreachable — it never
    // raises to the panel, so one backend outage can't break the whole request. We emit NO raw error
    // text: this contract is "types + hosts only, never credentials", and a client/SDK error string can
    // carry a credentialed URL or key — so the injected probe owns any (sanitized) detail it wants.
    return {
      type,
      ...(configuredHost != null ? { host: sanitizeHost(configuredHost) } : {}),
      reachable: false,
      detail: "probe failed",
    };
  }
  // A probe that reports only reachability (`host` OMITTED, i.e. undefined) keeps the configured host,
  // so the endpoint isn't erased (Python always reports the host it probed). An EXPLICIT `null` is the
  // probe deliberately saying "no host" (the default probe does this for non-langfuse) → omitted.
  const host = result.host === undefined ? configuredHost : result.host;
  return {
    type,
    ...(host != null ? { host: sanitizeHost(host) } : {}),
    reachable: result.reachable,
    ...(result.detail != null ? { detail: result.detail } : {}),
  };
}

/**
 * Project a resolved workflow spec into its connection status (Python `workflow_connections`).
 * `execution_manifest` and `redaction.enabled` default TRUE when unset — Python's `runtime.observability`
 * is a default-constructed `ObservabilitySpec` (fields `execution_manifest`/`redaction.enabled` default
 * True), so an absent observability block is NOT "off". The observer probe is passed no configured host
 * (parity with Python `_probe(kind=observer_type, configured_host=None)`).
 */
export async function buildWorkflowConnections(
  spec: TypefluxYamlSpec,
  workflowId: string,
  environmentId: string,
  probe: ConnectionProbe,
): Promise<ApiWorkflowConnections> {
  const registrySpec = spec.runtime.registry;
  const observabilitySpec = spec.runtime.observability;
  // Python `observability_spec.type or "none"` — a falsy/empty type reads as "none" (truthy, not ??).
  const observerType = observabilitySpec?.type || "none";

  // The two probes are independent; run them concurrently. `registry.host || null` uses truthy `||`
  // (not `??`): Python `_probe` resolves the host with `configured_host or …`, so an empty-string host
  // is falsy → treated as absent (env/None fallback), never surfaced as `""`.
  const [registry, observer] = await Promise.all([
    probeConnection(registrySpec.type, registrySpec.host || null, environmentId, probe),
    probeConnection(observerType, null, environmentId, probe),
  ]);

  return {
    workflow_id: workflowId,
    environment_id: environmentId,
    registry,
    observability: {
      ...observer,
      execution_manifest: observabilitySpec?.execution_manifest ?? true,
      redaction_enabled: observabilitySpec?.redaction?.enabled ?? true,
    },
  };
}
