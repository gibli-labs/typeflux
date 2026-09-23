/**
 * Status resource subscriptions (#326 Phase 1; design §4/§5.2). An MCP client subscribes to a run's
 * status resource
 *
 *   typeflux://{project}/workflows/{workflow_id}/status?environment_id=…&execution_id=…
 *
 * and the server polls the control plane at ITS recommended cadence
 * (`recommended_poll_interval_seconds`, default 1s), emitting `notifications/resources/updated` only
 * when the snapshot actually changes — so an agent can "watch this run until the review gate opens"
 * without hot-looping `get_status`. The poll is a plain status read (`trace=false`), so watching
 * never writes an audit event (§9 "no audit pollution from watching").
 *
 * The high-level `McpServer` implements resource LIST/READ but NOT subscribe/unsubscribe, so this
 * wires them on the underlying low-level `Server`: declare the `resources.subscribe` capability and
 * register the two request handlers. Polling stops when the run reaches a terminal state, on
 * unsubscribe, or on server dispose.
 */

import {
  SubscribeRequestSchema,
  UnsubscribeRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import type { Server } from "@modelcontextprotocol/sdk/server/index.js";

import type { Backend } from "./backend.js";
import { ApiRequestError } from "./control-plane/errors.js";

/** A parsed status-resource subscription target. */
export interface StatusTarget {
  project: string;
  workflowId: string;
  environmentId: string;
  executionId: string;
}

const STATUS_URI = /^typeflux:\/\/([^/]+)\/workflows\/([^/]+)\/status(?:\?(.*))?$/;

/** Terminal lifecycle states — once reached, the snapshot no longer changes, so polling stops. */
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled", "canceled", "terminated", "timed_out"]);

const MIN_INTERVAL_MS = 20;
const MAX_INTERVAL_MS = 60_000;
const DEFAULT_INTERVAL_MS = 1_000;

/**
 * Statuses a retry can never resolve — a subscription that hits one STOPS rather than hot-looping
 * forever. 403 (no inspect / expired token), 404 (unknown execution), 409 (a foreign/mismatched
 * execution), 422 (malformed request), 501 (unresolvable runtime). A 503 TemporalUnavailable is
 * transient — the poll keeps watching so a run reappears when Temporal reconnects.
 */
const PERMANENT_STATUSES = new Set([403, 404, 409, 422, 501]);

/**
 * Parse a status-resource URI into its target, or undefined when it is not a run-status URI (so a
 * subscription to any other resource is acknowledged but not polled). Parsed by regex — not `URL` —
 * to preserve the exact (case-sensitive) project id rather than lowercasing it as a hostname.
 */
export function parseStatusUri(uri: string): StatusTarget | undefined {
  const match = STATUS_URI.exec(uri);
  if (match === null) return undefined;
  const [, project, encodedWorkflow, rawQuery] = match;
  const query = new URLSearchParams(rawQuery ?? "");
  const workflowId = decodeURIComponent(encodedWorkflow!);
  const environmentId = query.get("environment_id") ?? "";
  const executionId = query.get("execution_id") ?? "";
  if (workflowId === "" || environmentId === "" || executionId === "") return undefined;
  return { project, workflowId, environmentId, executionId };
}

interface StatusSnapshot {
  status?: { state?: string; terminal_status?: string | null };
  recommended_poll_interval_seconds?: number;
}

function isTerminal(snapshot: StatusSnapshot): boolean {
  const status = snapshot.status;
  if (status === undefined) return false;
  if (status.terminal_status !== null && status.terminal_status !== undefined) return true;
  return status.state !== undefined && TERMINAL_STATES.has(status.state);
}

function clampInterval(seconds: number | undefined): number {
  if (typeof seconds !== "number" || !(seconds > 0)) return DEFAULT_INTERVAL_MS;
  return Math.min(Math.max(seconds * 1000, MIN_INTERVAL_MS), MAX_INTERVAL_MS);
}

interface Subscription {
  timer: ReturnType<typeof setTimeout> | undefined;
  last: string | undefined;
  stopped: boolean;
}

/** A wired status-subscription surface: the low-level handlers plus a `disposeAll` for shutdown. */
export interface StatusSubscriptions {
  /** Active subscription URIs (for tests/introspection). */
  active(): string[];
  /** Clear every poll timer (server shutdown). */
  disposeAll(): void;
}

/**
 * Wire status-resource subscriptions onto the low-level MCP `Server`. Declares the
 * `resources.subscribe` capability (must be called BEFORE the server connects) and registers the
 * subscribe/unsubscribe handlers that start/stop the per-run poll loop.
 */
export function registerStatusSubscriptions(
  server: Server,
  getBackend: () => Promise<Backend>,
): StatusSubscriptions {
  const subs = new Map<string, Subscription>();

  const stop = (uri: string): void => {
    const sub = subs.get(uri);
    if (sub === undefined) return;
    sub.stopped = true;
    if (sub.timer !== undefined) clearTimeout(sub.timer);
    subs.delete(uri);
  };

  const pollOnce = async (uri: string, target: StatusTarget): Promise<void> => {
    const sub = subs.get(uri);
    if (sub === undefined || sub.stopped) return;
    let intervalMs = DEFAULT_INTERVAL_MS;
    try {
      const backend = await getBackend();
      // Subscriptions are an inspect-tier read (like get_status). A degraded backend (403 on /meta =
      // no inspect) denies the live surface, so a status subscription must not poll either — stop it
      // rather than emit or loop.
      if (backend.degraded) {
        sub.stopped = true;
        return;
      }
      const snapshot = (await backend
        .scopedControlPlane(target.project)
        .status(target.workflowId, target.environmentId, target.executionId)) as StatusSnapshot;
      // The client may have unsubscribed DURING the in-flight status fetch — re-check before
      // emitting so a torn-down subscription never sends a stray notifications/resources/updated.
      if (sub.stopped) return;
      intervalMs = clampInterval(snapshot.recommended_poll_interval_seconds);
      const serialized = JSON.stringify(snapshot);
      // Baseline (first poll) establishes `last` WITHOUT emitting — the client just read it on
      // subscribe. Every later change emits `notifications/resources/updated`.
      if (sub.last !== undefined && serialized !== sub.last) {
        await server.sendResourceUpdated({ uri });
      }
      sub.last = serialized;
      if (isTerminal(snapshot)) {
        // A terminal run never changes again — release the timer (leave the sub so the client's
        // eventual unsubscribe is a clean no-op).
        sub.stopped = true;
        return;
      }
    } catch (error) {
      // A permanent failure (404/403/409/422/501) can't resolve by retrying — stop the loop so a
      // bad URI or an unauthorized/expired token doesn't hot-loop the control plane forever. A
      // transient failure (503 while Temporal reconnects, a network blip) keeps polling.
      if (error instanceof ApiRequestError && PERMANENT_STATUSES.has(error.status)) {
        sub.stopped = true;
        return;
      }
    }
    if (!sub.stopped) sub.timer = setTimeout(() => void pollOnce(uri, target), intervalMs);
  };

  // Declaring the capability must precede connect (registerCapabilities throws once a transport is
  // attached); createTypefluxMcpServer calls this during construction, before connect.
  server.registerCapabilities({ resources: { subscribe: true } });

  server.setRequestHandler(SubscribeRequestSchema, async (request) => {
    const uri = request.params.uri;
    const target = parseStatusUri(uri);
    // A non-status subscription is acknowledged (per MCP) but not polled — only run-status URIs get
    // a poll loop. Re-subscribing an ACTIVE URI is a no-op; re-subscribing a URI whose loop already
    // STOPPED (terminal run, permanent error, degrade) RESTARTS it — a stopped entry must not wedge
    // the URI so the client can never watch it again (Bugbot #711).
    if (target !== undefined) {
      const existing = subs.get(uri);
      if (existing === undefined || existing.stopped) {
        if (existing?.timer !== undefined) clearTimeout(existing.timer);
        subs.set(uri, { timer: undefined, last: undefined, stopped: false });
        void pollOnce(uri, target);
      }
    }
    return {};
  });

  server.setRequestHandler(UnsubscribeRequestSchema, async (request) => {
    stop(request.params.uri);
    return {};
  });

  return {
    active: () => [...subs.keys()],
    disposeAll: () => {
      for (const uri of [...subs.keys()]) stop(uri);
    },
  };
}
