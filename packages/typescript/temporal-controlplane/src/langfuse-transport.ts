/**
 * The injected langfuse transport seam (#573). The TS control plane holds no vendor SDK (thin-
 * transport philosophy, #499), so the two "live" tiers it otherwise reports honestly-degraded — the
 * connections reachability probe and the prompt-status registry drift — take an INJECTED structural
 * transport, exactly like `schemas` (catalog) and `ConnectionProbe` (connections) already are.
 *
 * One interface mirrors Python's three langfuse-reader capabilities:
 *   - `ping`                  → Python `connections.py` `_langfuse_probe` (`reader.list_traces(limit=1)`)
 *   - `promptLabelVersion`    → Python `prompt_status.py` `_registry_label_version` (`get_prompt(name,label).version`)
 *   - `lastRunPromptVersions` → Python `prompt_status.py` `_last_run_versions` (search → manifest → versions)
 *
 * The pure control plane never constructs one and never reads the network; a deployment injects a
 * real transport (see {@link fetchLangfuseTransport}) through `serve`. Host resolution and any
 * credentials live in the adapter, never the core.
 */

import { fanOut } from "@typeflux/temporal";

import type { ConnectionProbe } from "./connections.js";
import { fetchJson } from "./deadline-fetch.js";
import type { EnforcementTraceRecord } from "./enforcement.js";

/** The langfuse-reader operations the control plane needs. All async; a rejection is degraded by the caller. */
export interface LangfuseControlPlaneTransport {
  /**
   * Cheap reachability check (Python `reader.list_traces(TraceListQuery(limit=1))`). Resolving = the
   * host is reachable; rejecting = unreachable. `host` is the spec-configured host or null (the adapter
   * applies its own env fallback).
   */
  ping(options: { host: string | null; environmentId: string }): Promise<void>;
  /**
   * Resolve a prompt LABEL to its current registry version (Python `get_prompt(name, label).version`).
   * Returns the version string, or `undefined` when the label/prompt is absent. A rejection means the
   * lookup itself failed (surfaced as a drift `unknown` with a sanitized detail).
   */
  promptLabelVersion(options: { name: string; label: string; host: string | null }): Promise<string | undefined>;
  /**
   * The most-recent run's resolved prompt versions keyed by prompt name (Python `_last_run_versions`:
   * newest trace for the workflow → reconstructed manifest → `{name: resolved_version}`). Returns `{}`
   * when there is no run or the reconstruction fails (Python's "unknown side, never raise").
   */
  lastRunPromptVersions(options: { workflowName: string; host: string | null }): Promise<Record<string, string>>;
  /**
   * The newest trace correlated with one EXECUTION id, as a public summary dict (Python
   * `runs.py` `_langfuse_trace_summary`: `search_traces(TraceSearchQuery(workflow_id=…, limit=1))`
   * → `TraceSummaryView.to_public_dict()`). Resolves `null` when no trace matches (Python's
   * empty page); a REJECTION means the lookup itself failed and the caller degrades
   * (`/correlation`'s `reachable: false` + warning). `workflowId` is the Temporal workflow
   * (execution) id — the cross-edition join key both runtimes stamp into trace metadata.
   */
  traceSummary(options: { workflowId: string; host: string | null }): Promise<Record<string, unknown> | null>;
  /**
   * The bounded set of enforcement-relevant traces in a time window (Python `enforcement.py`
   * `default_langfuse_enforcement_reader`: `reader.search_traces(TraceSearchQuery(environment, since,
   * until, limit))`). Resolves the traces the enforcement feed extracts runtime verdicts from — each
   * carries its trace-level identity plus the observation metadata the moderation-block marker lives
   * on. A REJECTION means the read failed and the feed degrades LOUDLY (`partial.langfuse:
   * "unreachable"`), never a silent empty `ok`. The query is bounded by `limit` (capped at the feed's
   * MAX) and the `[since, until]` window — never an unbounded scan. `environmentId` is `null` when the
   * caller did not scope by environment.
   */
  searchEnforcementTraces(options: {
    environmentId: string | null;
    since: Date;
    until: Date;
    limit: number;
    host: string | null;
  }): Promise<EnforcementTraceRecord[]>;
  /**
   * Whether this transport holds the credentials the enforcement feed's runtime read needs — Python
   * `enforcement.py` `_langfuse_env_configured` (host + public key + secret key). The enforcement
   * reader checks this BEFORE any network call: absent credentials answer `not_configured` (a pinned
   * "wire up Langfuse" status), so an empty-credentialed transport never 401s into a misleading
   * `unreachable`. Optional and defaulting to configured — a fixture transport omits it (its
   * `searchEnforcementTraces` is what the test drives), so only the real fetch adapter reports `false`.
   */
  enforcementCredentialsConfigured?: boolean;
}

/**
 * A langfuse-backed {@link ConnectionProbe} built from an injected transport (Python `_probe`). A
 * non-langfuse backend has nothing to reach → reachable (matching the network-free default); a
 * langfuse backend resolves its host (configured → `LANGFUSE_HOST` → `LANGFUSE_BASE_URL`), pings, and
 * reports `reachable: true` on success or a SANITIZED-detail `reachable: false` on any failure.
 *
 * Unlike Python's `_probe`, which puts the raw `{exc}` in `detail`, the TS connections contract forbids
 * raw error text (it can carry a credentialed URL/key), so the failure detail is a fixed, host-free
 * class string — the probe owns its own sanitized detail (see `connections.ts` `probeConnection`).
 */
export function langfuseConnectionProbe(
  transport: LangfuseControlPlaneTransport,
  options: { env?: Record<string, string | undefined> } = {},
): ConnectionProbe {
  const env = options.env ?? process.env;
  return async ({ type, configuredHost, environmentId }) => {
    if (type !== "langfuse") {
      return { reachable: true, host: null };
    }
    // Python `configured_host or os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL")` —
    // truthy `||` so an empty-string configured host falls through to the env, then to null.
    const host = configuredHost || env["LANGFUSE_HOST"] || env["LANGFUSE_BASE_URL"] || null;
    try {
      await transport.ping({ host, environmentId });
      return { reachable: true, host };
    } catch {
      return {
        reachable: false,
        host,
        // No raw error text: a client/SDK error string can carry a credentialed URL or key.
        detail: "langfuse probe failed",
      };
    }
  };
}

/** One prompt's resolved drift verdict from the transport (versions only; see `prompt-status.ts`). */
export interface PromptDriftVerdict {
  status: "in_sync" | "drift" | "unknown";
  registryVersion?: string;
  detail?: string;
}

/**
 * Compare a label's live registry version against the last run's version (Python `workflow_prompt_status`
 * label arm): `unknown` if either side is missing, `drift` if they differ, else `in_sync`. A registry
 * lookup that throws degrades to `unknown` with a sanitized `registry lookup failed: …` detail — the
 * SAME prefix Python uses (`f"registry lookup failed: {exc}"`), but with the message class only, never a
 * host/credential. `lastRunVersion` is precomputed by the caller (it is shared across all prompts).
 */
export async function resolveLabelDrift(
  transport: LangfuseControlPlaneTransport,
  options: { name: string; label: string; host: string | null; lastRunVersion: string | undefined },
): Promise<PromptDriftVerdict> {
  let registryVersion: string | undefined;
  try {
    registryVersion = await transport.promptLabelVersion({
      name: options.name,
      label: options.label,
      host: options.host,
    });
  } catch (error) {
    return {
      status: "unknown",
      // Message class only (never the raw error, which can carry a credentialed URL) — the Python
      // detail prefix is preserved so the honest-degradation contract reads the same both editions.
      detail: `registry lookup failed: ${sanitizeErrorMessage(error)}`,
    };
  }
  const ran = options.lastRunVersion;
  if (registryVersion === undefined || ran === undefined) {
    return { status: "unknown", ...(registryVersion !== undefined ? { registryVersion } : {}) };
  }
  return { status: registryVersion === ran ? "in_sync" : "drift", registryVersion };
}

/** Options for {@link fetchLangfuseTransport} — credentials + host, with test seams for `fetch`/`env`. */
export interface FetchLangfuseTransportOptions {
  /** Langfuse public key (else `LANGFUSE_PUBLIC_KEY`). */
  publicKey?: string;
  /** Langfuse secret key (else `LANGFUSE_SECRET_KEY`). */
  secretKey?: string;
  /** Base host override (else the per-call host, then `LANGFUSE_HOST`/`LANGFUSE_BASE_URL`, then cloud). */
  host?: string;
  /** Env source (defaults to `process.env`) — the key/host fallbacks read from here. */
  env?: Record<string, string | undefined>;
  /** `fetch` override (defaults to the global) — injected in unit tests. */
  fetch?: typeof fetch;
  /**
   * Per-request deadline in ms (default 5000). These calls sit directly on `/connections` and
   * `/prompt-status`, so a stalled langfuse host must time out and degrade (unreachable / unknown)
   * rather than hang the route — the injected-probe "carry your own deadline" contract.
   */
  timeoutMs?: number;
  /**
   * Per-request deadline for the `/correlation` trace lookup in ms (default `max(timeoutMs, 15000)`).
   * Separate from {@link timeoutMs} on purpose: the connections/prompt-status probes back
   * latency-sensitive panels, while the correlation card is a one-shot lookup whose page scan
   * tolerates a slower host (Python's reader carries no per-request bound at all).
   */
  traceLookupTimeoutMs?: number;
}

const LANGFUSE_CLOUD = "https://cloud.langfuse.com";

/**
 * A dependency-free reference {@link LangfuseControlPlaneTransport} over langfuse's public REST API
 * (`fetch` + HTTP Basic auth from `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`). This is the concrete
 * transport a deployment injects (via `serve`'s `--langfuse` flag or `langfuseFor`); the CP core stays
 * vendor-free. A non-2xx response throws so the caller degrades (`reachable:false` / drift `unknown`).
 *
 * Endpoints (langfuse public API): `GET /api/public/traces?limit=1` (ping), `GET /api/public/v2/prompts/{name}?label=…`
 * (label→version), `GET /api/public/traces?name=…&limit=1` + `GET /api/public/traces/{id}` (last-run
 * versions from the newest trace's typeflux activity manifests). The last-run reconstruction is
 * best-effort and defensive — any shape it doesn't recognize yields `{}` (an honest `unknown` drift),
 * never a throw. Verifying the exact response shapes needs a live langfuse instance (see the gated test).
 */
export function fetchLangfuseTransport(options: FetchLangfuseTransportOptions = {}): LangfuseControlPlaneTransport {
  const env = options.env ?? process.env;
  const doFetch = options.fetch ?? fetch;
  const timeoutMs = options.timeoutMs ?? 5000;
  const publicKey = options.publicKey ?? env["LANGFUSE_PUBLIC_KEY"] ?? "";
  const secretKey = options.secretKey ?? env["LANGFUSE_SECRET_KEY"] ?? "";
  const authHeader = `Basic ${Buffer.from(`${publicKey}:${secretKey}`).toString("base64")}`;
  // Python `_langfuse_env_configured`: host + public + secret all present. The enforcement reader
  // gates on this to answer `not_configured` WITHOUT a network call when credentials are absent (an
  // empty-credentialed transport otherwise 401s into a misleading `unreachable`).
  const hostConfigured = (options.host ?? env["LANGFUSE_HOST"] ?? env["LANGFUSE_BASE_URL"] ?? "") !== "";
  const enforcementCredentialsConfigured = publicKey !== "" && secretKey !== "" && hostConfigured;

  const baseFor = (host: string | null): string => {
    // Per-call host (from the workflow's own registry/observability config) is the MOST specific and
    // wins; then the transport's construction-time default; then the env; then cloud. Truthy `||` so an
    // empty string falls through (parity with the probe wrapper's resolution order).
    const resolved = host || options.host || env["LANGFUSE_HOST"] || env["LANGFUSE_BASE_URL"] || LANGFUSE_CLOUD;
    return resolved.replace(/\/+$/, ""); // no trailing slash before we append `/api/...`
  };
  const traceLookupTimeoutMs = options.traceLookupTimeoutMs ?? Math.max(timeoutMs, 15_000);
  const get = async (host: string | null, path: string, deadlineMs: number = timeoutMs): Promise<unknown> =>
    // The shared deadline-bound JSON wrapper (#727 F7) owns the per-request timeout + the non-ok gate
    // + `.json()`; langfuse's classifier throws ONE named error on any non-2xx.
    fetchJson({
      fetch: doFetch,
      url: `${baseFor(host)}${path}`,
      headers: { authorization: authHeader, accept: "application/json" },
      timeoutMs: deadlineMs,
      onErrorResponse: (response) => {
        // A CUSTOM error class so a downstream `detail` can name the failure class safely (the message,
        // kept for logs, is never echoed into the contract — see `sanitizeErrorMessage`).
        const error = new Error(`langfuse request failed (status ${response.status})`);
        error.name = "LangfuseRequestError";
        throw error;
      },
    });

  return {
    enforcementCredentialsConfigured,
    async ping({ host }): Promise<void> {
      await get(host, "/api/public/traces?limit=1");
    },
    async promptLabelVersion({ name, label, host }): Promise<string | undefined> {
      const body = (await get(
        host,
        `/api/public/v2/prompts/${encodeURIComponent(name)}?label=${encodeURIComponent(label)}`,
      )) as { version?: unknown } | null;
      const version = body?.version;
      return version === undefined || version === null ? undefined : String(version);
    },
    async lastRunPromptVersions({ workflowName, host }): Promise<Record<string, string>> {
      try {
        // Typeflux tags every workflow-related trace `typeflux.workflow:<name>` (the low-cardinality
        // locator; the EXECUTION trace's NAME is `TypefluxWorkflow:<name>`, so a bare `?name=<name>`
        // would miss). But LIFECYCLE traces (status/review/cancel — `TypefluxLifecycle*`) share the tag
        // and carry NO execution manifest, and can be NEWER than the last execution. So scan newest-first
        // (Python's reader post-filters candidates too), across BOUNDED pages, and return the first trace
        // whose reconstruction yields any versions — a long run of lifecycle/empty traces must not hide
        // an older execution trace on a later page (Bugbot).
        const executionTraceName = `TypefluxWorkflow:${workflowName}`;
        const tag = encodeURIComponent(`typeflux.workflow:${workflowName}`);
        const pageSize = 20;
        const maxPages = 5; // cap the scan at 100 traces — bounded, unlike an unfollowed single page
        for (let page = 1; page <= maxPages; page += 1) {
          const list = (await get(host, `/api/public/traces?tags=${tag}&limit=${pageSize}&page=${page}`)) as {
            data?: Array<{ id?: unknown; name?: unknown }>;
            meta?: { totalPages?: unknown };
          } | null;
          const rows = list?.data ?? [];
          const candidates = rows.filter(
            // Keep entries with an id; when a name is PRESENT keep only the execution trace (drop
            // lifecycle traces up front). An ABSENT name — `undefined` OR JSON `null` — is kept and left
            // for reconstruction to filter (a `null` name must not be treated as "not the execution trace").
            (trace) =>
              typeof trace.id === "string" &&
              (trace.name === undefined || trace.name === null || trace.name === executionTraceName),
          );
          for (const candidate of candidates) {
            const trace = (await get(host, `/api/public/traces/${encodeURIComponent(candidate.id as string)}`)) as {
              metadata?: unknown;
              observations?: Array<{ metadata?: unknown }>;
            } | null;
            const versions = reconstructTraceVersions(trace);
            if (Object.keys(versions).length > 0) return versions; // first trace that actually recorded versions
          }
          // Stop at the last (or a short/empty) page — no further candidates to scan.
          const totalPages = typeof list?.meta?.totalPages === "number" ? list.meta.totalPages : page;
          if (rows.length < pageSize || page >= totalPages) break;
        }
        return {};
      } catch {
        // Python `_last_run_versions`: "unknown side, never raise" — any failure is an empty map.
        return {};
      }
    },
    async traceSummary({ workflowId, host }): Promise<Record<string, unknown> | null> {
      // Python `search_traces` semantics over REST: scan newest-first BOUNDED pages of the trace
      // list (its reader pages the same way — a plain limit=1 list would miss the join key in a
      // busy project), matching each row's TRACE metadata against the execution id under the
      // cross-edition key set (see `metadataCarriesWorkflowId`). Failures propagate: unlike the
      // drift tier, correlation reports an unreachable backend honestly (Python's warning path).
      const pageSize = 20;
      const maxPages = 5; // cap the scan at 100 traces — the same bound as lastRunPromptVersions
      for (let page = 1; page <= maxPages; page += 1) {
        const list = (await get(host, `/api/public/traces?limit=${pageSize}&page=${page}`, traceLookupTimeoutMs)) as {
          data?: Array<{ id?: unknown; metadata?: unknown }>;
          meta?: { totalPages?: unknown };
        } | null;
        const rows = list?.data ?? [];
        for (const row of rows) {
          if (typeof row.id !== "string") continue;
          if (!metadataCarriesWorkflowId(row.metadata, workflowId)) continue;
          const trace = (await get(host, `/api/public/traces/${encodeURIComponent(row.id)}`, traceLookupTimeoutMs)) as FullTrace | null;
          if (trace === null) continue;
          return buildTraceSummary(row.id, trace, workflowId);
        }
        // OBSERVATION-metadata fallback for the NEWEST page only, BEFORE deeper
        // pagination: a worker-only traced run (a control-plane start, #686)
        // writes no caller-side parent metadata — the activity spans carry the
        // join key, and the langfuse list endpoint returns no observations.
        // Python's reader searches an observations feed and matches these
        // outright; this edition follows the newest 20 traces' details (the
        // sought run is overwhelmingly the newest, and deep pagination is the
        // slow path on a loaded host) — bounded, never a per-row fan-out
        // across every page.
        if (page === 1) {
          // Per-id best-effort: one slow/failed detail GET (a spiky host) must
          // not abort the whole lookup while later ids could still match. If
          // EVERY detail fetch failed and nothing matched, the backend is
          // genuinely unreachable — rethrow so the route degrades honestly
          // instead of reporting a false "reachable, no trace".
          let fallbackFailure: unknown;
          let fallbackSucceeded = false;
          for (const row of rows) {
            if (typeof row.id !== "string") continue;
            let trace: FullTrace | null;
            try {
              trace = (await get(host, `/api/public/traces/${encodeURIComponent(row.id)}`, traceLookupTimeoutMs)) as FullTrace | null;
            } catch (error) {
              fallbackFailure = error;
              continue;
            }
            fallbackSucceeded = true;
            if (trace === null) continue;
            const observations = trace.observations ?? [];
            if (observations.some((observation) => metadataCarriesWorkflowId(observation.metadata, workflowId))) {
              return buildTraceSummary(row.id, trace, workflowId);
            }
          }
          if (!fallbackSucceeded && fallbackFailure !== undefined && rows.length > 0) {
            throw fallbackFailure;
          }
        }
        const totalPages = typeof list?.meta?.totalPages === "number" ? list.meta.totalPages : page;
        if (rows.length < pageSize || page >= totalPages) break;
      }
      return null;
    },
    async searchEnforcementTraces({ since, until, limit, host }): Promise<EnforcementTraceRecord[]> {
      // Bound the read on OBSERVATION start time, NOT trace timestamp: Python's reader queries the
      // observations API (`client.api.observations.get_many` → GET /api/public/observations) bounded
      // by `fromStartTime`/`toStartTime`, then groups rows into traces. A long-running workflow whose
      // ROOT trace predates the window but has an in-window moderation-block observation is thus
      // surfaced — bounding on the trace timestamp (the old GET /api/public/traces path) silently
      // dropped it, which is typeflux's core use case. The environment is NOT forwarded as Langfuse's
      // native `environment` query param: typeflux writers never set that native tag, so the
      // server-side filter would match ZERO rows (a false-healthy `ok + []`). Environment scoping is
      // metadata-based and applied client-side (`enforcement.ts` `filterEvents`), mirroring Python.
      //
      // Unlike the drift tier this path PROPAGATES failure: any non-2xx throws so the reader reports
      // `unreachable` (never a silent empty `ok`). The exact langfuse REST shapes are best-effort/
      // defensive (verified live in the gated test), so an unrecognized payload degrades to an empty
      // record set, never a crash.
      const bounded = Math.max(1, Math.min(limit, 200));
      // Ask for more observations than traces (a trace has many spans) so `bounded` distinct traces
      // can be gathered from one page — mirrors Python's `_observation_limit` (~10× the trace limit,
      // capped at the Langfuse per-page max).
      const observationLimit = Math.min(Math.max(bounded * 10, 50), 1000);
      // Scan up to SCAN_PAGES observation pages before capping distinct traces — Python's
      // `TraceSearchQuery.scan_pages` (default 5) does the same, so a busy window whose first
      // page is dominated by a few chatty traces still surfaces blocks from later pages
      // (Bugbot: one page under-returned vs the Python edition).
      const SCAN_PAGES = 5;
      const traceIds: string[] = [];
      const seen = new Set<string>();
      for (let page = 1; page <= SCAN_PAGES && traceIds.length < bounded; page += 1) {
        const params = new URLSearchParams({
          limit: String(observationLimit),
          page: String(page),
          fromStartTime: since.toISOString(),
          toStartTime: until.toISOString(),
        });
        const list = (await get(host, `/api/public/observations?${params.toString()}`, traceLookupTimeoutMs)) as {
          data?: Array<{ traceId?: unknown }>;
        } | null;
        const rows = list?.data ?? [];
        // Distinct trace ids in newest-first order, capped at the feed's bound.
        for (const row of rows) {
          const traceId = typeof row.traceId === "string" ? row.traceId : undefined;
          if (traceId === undefined || seen.has(traceId)) continue;
          seen.add(traceId);
          traceIds.push(traceId);
          if (traceIds.length >= bounded) break;
        }
        if (rows.length < observationLimit) break; // last page — no further rows to scan.
      }
      // Fetch each distinct trace's detail (for its trace-level identity + the full observation set the
      // moderation marker rides on) with BOUNDED concurrency — a pool, not up to 200 serial GETs.
      // `fanOut` (the repo's bounded parallel map) propagates the first rejection after in-flight
      // siblings settle, so a failed detail fetch still degrades the feed to `unreachable`.
      return fanOut(
        traceIds,
        async (traceId) => {
          const trace = (await get(host, `/api/public/traces/${encodeURIComponent(traceId)}`, traceLookupTimeoutMs)) as FullTrace | null;
          return enforcementTraceRecord(traceId, trace);
        },
        { concurrency: 8 },
      );
    },
  };
}

/**
 * Map one full langfuse trace to the edition-native {@link EnforcementTraceRecord} the enforcement
 * extractor consumes. Trace-level identity comes from the execution-manifest rollup the drift/
 * correlation tiers already read (`metadata.typeflux.execution_manifest`); each observation's raw
 * metadata is passed through so the pure extractor can find the FLAT `typeflux_moderation` marker.
 * The execution id falls back to the same cross-edition metadata keys {@link metadataCarriesWorkflowId}
 * matches on — a real TS run stamps the flat `metadata.workflow_id` (#681), NOT only the
 * execution-manifest rollup, so without the fallback the moderation event would omit `execution_id`.
 * Defensive: any shape it doesn't recognize yields undefined fields, never a throw.
 */
function enforcementTraceRecord(traceId: string, trace: FullTrace | null): EnforcementTraceRecord {
  const manifest = typefluxField(trace?.metadata, "execution_manifest");
  const rollup = typeof manifest === "object" && manifest !== null ? (manifest as Record<string, unknown>) : {};
  const provenance =
    typeof rollup["code_provenance"] === "object" && rollup["code_provenance"] !== null
      ? (rollup["code_provenance"] as Record<string, unknown>)
      : {};
  const asString = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);
  const name = asString(trace?.name);
  const workflowName = name !== undefined && name.startsWith("TypefluxWorkflow:") ? name.slice("TypefluxWorkflow:".length) : asString(rollup["workflow_name"]);
  const appliedPolicyIds = Array.isArray(rollup["applied_policy_ids"])
    ? (rollup["applied_policy_ids"] as unknown[]).map((id) => String(id))
    : [];
  const observations = (trace?.observations ?? []).map((observation) => ({
    startTime: asString((observation as { startTime?: unknown }).startTime) ?? null,
    metadata: typeof observation.metadata === "object" && observation.metadata !== null ? (observation.metadata as Record<string, unknown>) : null,
  }));
  return {
    traceId,
    timestamp: asString(trace?.timestamp) ?? null,
    workflowName: workflowName ?? null,
    workflowId: asString(rollup["workflow_id"]) ?? workflowIdCandidates(trace?.metadata)[0] ?? null,
    environment: asString(provenance["environment"]) ?? null,
    appliedPolicyIds,
    observations,
  };
}

/** The full-trace GET shape this module reads (defensive — unknown shapes degrade, never crash). */
interface FullTrace {
  name?: unknown;
  timestamp?: unknown;
  metadata?: unknown;
  observations?: Array<{ metadata?: unknown; level?: unknown }>;
}

/**
 * Whether a trace metadata bag correlates with one Temporal workflow (execution) id — the
 * cross-edition key set: Python's reader keys (`observability/inspect.py`
 * `_workflow_ids_from_metadata`: the flat `temporal.workflow_id` /
 * `typeflux.temporal.workflow_id` / `typeflux.lifecycle_operation.workflow_id` and their nested
 * `typeflux.{execution_manifest,temporal,lifecycle_operation}.workflow_id` forms) PLUS the bare
 * `workflow_id` the TS runtime stamps on the grouped parent trace (`runtime.ts`
 * `recordWorkflowRun` metadata, #681).
 */
function workflowIdCandidates(metadata: unknown): string[] {
  if (typeof metadata !== "object" || metadata === null) return [];
  const bag = metadata as Record<string, unknown>;
  const candidates: unknown[] = [
    bag["workflow_id"],
    bag["temporal.workflow_id"],
    bag["typeflux.temporal.workflow_id"],
    bag["typeflux.lifecycle_operation.workflow_id"],
  ];
  const nested = bag["typeflux"];
  if (typeof nested === "object" && nested !== null) {
    // `activity_execution_manifest` included (#682/#686): a WORKER-only traced
    // run (a control-plane start) writes no caller-side parent metadata, so
    // its activity spans are the only join surface the trace offers.
    for (const key of ["execution_manifest", "activity_execution_manifest", "temporal", "lifecycle_operation"]) {
      const section = (nested as Record<string, unknown>)[key];
      if (typeof section === "object" && section !== null) {
        candidates.push((section as Record<string, unknown>)["workflow_id"]);
      }
    }
  }
  return candidates.filter((candidate): candidate is string => typeof candidate === "string");
}

function metadataCarriesWorkflowId(metadata: unknown, workflowId: string): boolean {
  return workflowIdCandidates(metadata).includes(workflowId);
}

/**
 * A best-effort public summary of one full trace (Python `TraceSummaryView.to_public_dict()`,
 * edition-native breadth by design — the correlation contract's `trace` is an open object, like
 * the binding-opaque identity fields). Mirrors the Python keys this transport can honestly
 * derive from the REST payload: identity (`trace_id`/`timestamp`/`status`/`workflow_name`/
 * `workflow_id`) plus the activity rollup (`activities`/`prompt_refs`/`provider_models`) from the
 * same manifest metadata the drift tier reads. Keys with no derivable value OMIT (Python's
 * `exclude_none`).
 */
function buildTraceSummary(traceId: string, trace: FullTrace, workflowId: string): Record<string, unknown> {
  const observations = trace.observations ?? [];
  // Python `_trace_status`: any observation at level ERROR marks the trace "error".
  const status = observations.some(
    (observation) => typeof observation.level === "string" && observation.level.toUpperCase() === "ERROR",
  )
    ? "error"
    : "ok";
  const activities: string[] = [];
  const promptRefs: string[] = [];
  const providerModels: string[] = [];
  const push = (values: string[], value: unknown): void => {
    if (typeof value === "string" && value.length > 0 && !values.includes(value)) values.push(value);
  };
  const addManifest = (manifest: unknown): void => {
    if (typeof manifest !== "object" || manifest === null) return;
    const record = manifest as Record<string, unknown>;
    push(activities, record["activity_name"]);
    push(promptRefs, readActivityRecord(manifest).name);
    push(providerModels, record["provider_model"]);
  };
  const rollup = typefluxField(trace.metadata, "execution_manifest");
  if (typeof rollup === "object" && rollup !== null) {
    const entries = (rollup as Record<string, unknown>)["activities"];
    if (Array.isArray(entries)) for (const entry of entries) addManifest(entry);
  }
  for (const observation of observations) {
    addManifest(typefluxField(observation.metadata, "activity_execution_manifest"));
  }
  return {
    trace_id: traceId,
    ...(typeof trace.timestamp === "string" ? { timestamp: trace.timestamp } : {}),
    status,
    ...(typeof trace.name === "string" ? { workflow_name: trace.name } : {}),
    workflow_id: workflowId,
    activities,
    prompt_refs: promptRefs,
    provider_models: providerModels,
    warnings: [],
  };
}

/**
 * Reconstruct `{prompt name: resolved version}` from one full trace (Python `reconstruct_execution_manifest`
 * over the trace). FIRST writer wins: the authoritative workflow rollup
 * (`metadata.typeflux.execution_manifest.activities`) is read first, so it beats any per-observation
 * span (`typeflux.activity_execution_manifest`) for the same activity — a stale retry span must not
 * overwrite the rollup and report a false drift; observations only FILL activities the rollup lacks.
 * The map is null-prototype so an untrusted prompt name (`toString`) can't collide with inherited keys.
 */
function reconstructTraceVersions(
  trace: { metadata?: unknown; observations?: Array<{ metadata?: unknown }> } | null,
): Record<string, string> {
  const versions: Record<string, string> = Object.create(null) as Record<string, string>;
  const add = (record: { name: string | undefined; version: string | undefined } | undefined): void => {
    if (record?.name !== undefined && record.version !== undefined && !Object.hasOwn(versions, record.name)) {
      versions[record.name] = record.version;
    }
  };
  for (const activity of readTypefluxWorkflowManifestActivities(trace?.metadata)) add(activity);
  for (const observation of trace?.observations ?? []) add(readTypefluxActivityManifest(observation.metadata));
  return versions;
}

/**
 * Pull `{prompt name, resolved version}` out of one observation's activity execution manifest metadata
 * (the same field Python's `reconstruct_execution_manifest` walks). Typeflux writes the CANONICAL form
 * NESTED under the structured `typeflux` object (`metadata.typeflux.activity_execution_manifest`); a
 * flat dotted key (`metadata["typeflux.activity_execution_manifest"]`) is the legacy fallback readers
 * still accept. Defensive: unknown shapes return `undefined`, so a format change degrades to `unknown`
 * drift, never a crash.
 */
function readTypefluxActivityManifest(
  metadata: unknown,
): { name: string | undefined; version: string | undefined } | undefined {
  const manifest = typefluxField(metadata, "activity_execution_manifest");
  return manifest === undefined ? undefined : readActivityRecord(manifest);
}

/**
 * The `{name, version}` entries from the ROOT trace's workflow-level rollup
 * (`metadata.typeflux.execution_manifest.activities[]`, or the legacy flat key). Python reconstructs
 * last-run versions from the whole trace, so a trace that only recorded them in the rollup still
 * resolves. Returns an empty array for any unrecognized shape.
 */
function readTypefluxWorkflowManifestActivities(
  metadata: unknown,
): Array<{ name: string | undefined; version: string | undefined }> {
  const manifest = typefluxField(metadata, "execution_manifest");
  if (typeof manifest !== "object" || manifest === null) return [];
  const activities = (manifest as Record<string, unknown>)["activities"];
  if (!Array.isArray(activities)) return [];
  return activities.map(readActivityRecord);
}

/**
 * Read a `typeflux.<key>` field from an observation/trace metadata bag, preferring the CANONICAL nested
 * `metadata.typeflux.<key>` and falling back to the legacy flat dotted `metadata["typeflux.<key>"]`.
 */
function typefluxField(metadata: unknown, key: string): unknown {
  if (typeof metadata !== "object" || metadata === null) return undefined;
  const bag = metadata as Record<string, unknown>;
  const nested = bag["typeflux"];
  if (typeof nested === "object" && nested !== null) {
    const value = (nested as Record<string, unknown>)[key];
    if (value !== undefined) return value; // canonical nested form present
  }
  // Legacy flat dotted key — also the fallback for a MIXED bag that has a `typeflux` object which
  // simply doesn't carry this particular field (a nested object must not shadow the flat fallback).
  return bag[`typeflux.${key}`];
}

/** Extract `{prompt name, resolved version}` from one activity-manifest record (`prompt_ref` + `resolved_prompt_version`). */
function readActivityRecord(manifest: unknown): { name: string | undefined; version: string | undefined } {
  if (typeof manifest !== "object" || manifest === null) return { name: undefined, version: undefined };
  const record = manifest as Record<string, unknown>;
  const ref = record["prompt_ref"];
  const name =
    typeof ref === "object" && ref !== null && typeof (ref as Record<string, unknown>)["name"] === "string"
      ? ((ref as Record<string, unknown>)["name"] as string)
      : typeof ref === "string"
        ? ref.split("@")[0]
        : undefined;
  const rawVersion = record["resolved_prompt_version"];
  const version = rawVersion === undefined || rawVersion === null ? undefined : String(rawVersion);
  return { name, version };
}

/**
 * A credential-safe rendering of a thrown value for the `detail` contract ("types + hosts only, never
 * credentials"). A free-form error MESSAGE can embed a host/token with any spacing (`connect
 * ECONNREFUSED lf.local:3000`, `auth failed for token abc123`), so we NEVER echo it — we emit only the
 * error's class NAME, which is a static developer-authored identifier. The name is screened to a plain
 * identifier shape (and the default `"Error"` rejected) so anything odd/settable fails closed to a
 * fixed string. Callers that want an informative detail should throw a custom-named error class.
 */
export function sanitizeErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "lookup error";
  const name = error.name;
  if (name.length > 0 && name !== "Error" && /^[A-Za-z][A-Za-z0-9_]*$/.test(name)) return name;
  return "lookup error";
}
