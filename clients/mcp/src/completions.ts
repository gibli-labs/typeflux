/**
 * MCP argument completions (#326 Phase 2; design §4). Autocompletion for the opaque IDs an agent has
 * to type — `workflow_id`, `environment_id`, `policy_id`, `project`, and the deployment `plan_id` —
 * each backed by the corresponding control-plane LIST endpoint, so an ID picker is always the live
 * project's current values rather than a guess.
 *
 * The same completer type satisfies BOTH surfaces the SDK can complete:
 *   - PROMPT arguments (via `completable(z.string(), completer)` in prompts.ts), and
 *   - RESOURCE-TEMPLATE variables (via the `complete` map on a `ResourceTemplate` in resources.ts).
 * Both call shapes are `(value, { arguments }) => string[]`, so one function wires into either.
 *
 * DEGRADE/CAPABILITY AWARENESS (design §4 "a degraded backend completes only what static discovery
 * allows"): every completer is backed by a live `inspect` read. A degraded backend (403 on /meta =
 * no inspect) or ANY list failure yields NO candidates — static discovery exposes no dynamic IDs, so
 * the honest completion there is empty. A completion request is a low-stakes hint channel, so a
 * failure never surfaces an error; it just offers nothing.
 *
 * PROJECT SCOPE: candidates are drawn from the project the caller is already targeting. When a
 * `project` argument is already filled on the prompt/template (`context.arguments.project`), the
 * completer lists that project's scope; the `project` completer itself lists the registry and is
 * never scoped (mirrors client.ts, which never project-scopes the registry listing).
 */

import type { Backend } from "./backend.js";
import type { TypefluxControlPlane } from "./control-plane/client.js";

/** The completion context the SDK passes: the arguments already filled on the prompt/template. */
export interface CompletionContext {
  arguments?: Record<string, string>;
}

/**
 * A completer usable as BOTH an MCP `completable()` callback and a `ResourceTemplate` completion
 * callback (the two signatures coincide): given the partial value the user has typed and the context
 * of already-filled arguments, return the matching candidate IDs.
 */
export type IdCompleter = (value: string, context?: CompletionContext) => Promise<string[]>;

/** The design §4 completion variables — the IDs that get an autocompleter. */
export type CompletionVariable =
  | "workflow_id"
  | "environment_id"
  | "policy_id"
  | "project"
  | "plan_id";

/** MCP caps a completion response at 100 values; narrow to that before returning. */
const MAX_COMPLETIONS = 100;

/**
 * Rank candidates against the partial value: exact case-insensitive PREFIX matches first, then
 * substring matches, de-duplicated and capped. An empty value returns the full (capped) list so a
 * fresh cursor still gets the pick list. Exported for the completion test.
 */
export function narrow(value: string, candidates: readonly string[]): string[] {
  const seen = new Set<string>();
  const unique: string[] = [];
  for (const candidate of candidates) {
    if (candidate !== "" && !seen.has(candidate)) {
      seen.add(candidate);
      unique.push(candidate);
    }
  }
  const needle = value.trim().toLowerCase();
  if (needle === "") return unique.slice(0, MAX_COMPLETIONS);
  const prefix = unique.filter((c) => c.toLowerCase().startsWith(needle));
  const substring = unique.filter(
    (c) => !c.toLowerCase().startsWith(needle) && c.toLowerCase().includes(needle),
  );
  return [...prefix, ...substring].slice(0, MAX_COMPLETIONS);
}

/** The registered ID completers, plus a variable-name lookup for resource-template wiring. */
export interface Completers extends Record<CompletionVariable, IdCompleter> {
  /** Resolve a completer by the URI-template / prompt-argument variable name (undefined if none). */
  forVariable(name: string): IdCompleter | undefined;
}

/**
 * Build the ID completers backed by the control plane's list endpoints (design §4). `getBackend` is
 * the same lazily-resolved backend the tools/resources use; a completion never forces a fresh
 * control plane (a failed resolution just yields no candidates).
 */
export function makeCompleters(getBackend: () => Promise<Backend>): Completers {
  /** The project-scoped control plane for a completion, honoring an already-filled `project` arg. */
  const scoped = (backend: Backend, context: CompletionContext | undefined): TypefluxControlPlane => {
    const project = context?.arguments?.project;
    return project !== undefined && project !== ""
      ? backend.scopedControlPlane(project)
      : backend.controlPlane;
  };

  /**
   * A scoped-list completer: resolve the backend, refuse to list on a degraded (no-inspect) backend,
   * pull the ids, and narrow — swallowing any failure into an empty candidate set.
   */
  const scopedList = (pick: (cp: TypefluxControlPlane) => Promise<string[]>): IdCompleter => {
    return async (value, context) => {
      try {
        const backend = await getBackend();
        if (backend.degraded) return [];
        return narrow(value, await pick(scoped(backend, context)));
      } catch {
        return [];
      }
    };
  };

  const workflow_id = scopedList((cp) => cp.workflows().then((ws) => ws.map((w) => w.id)));
  const environment_id = scopedList((cp) => cp.environments().then((es) => es.map((e) => e.id)));
  const policy_id = scopedList((cp) => cp.policies().then((ps) => ps.map((p) => p.id)));
  const plan_id = scopedList((cp) => cp.deployments().then((ds) => ds.map((d) => d.plan_id)));

  // The project registry is project-agnostic — always the default client, never scoped.
  const project: IdCompleter = async (value) => {
    try {
      const backend = await getBackend();
      if (backend.degraded) return [];
      return narrow(value, (await backend.controlPlane.projects()).map((p) => p.id));
    } catch {
      return [];
    }
  };

  const byVariable: Record<CompletionVariable, IdCompleter> = {
    workflow_id,
    environment_id,
    policy_id,
    project,
    plan_id,
  };

  return {
    ...byVariable,
    forVariable: (name) => (name in byVariable ? byVariable[name as CompletionVariable] : undefined),
  };
}

/** The design §4 completion variables, for the conformance/inventory test. */
export const COMPLETION_VARIABLES: readonly CompletionVariable[] = [
  "workflow_id",
  "environment_id",
  "policy_id",
  "project",
  "plan_id",
];
