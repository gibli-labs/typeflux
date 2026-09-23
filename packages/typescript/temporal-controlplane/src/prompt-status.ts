/**
 * Prompt-registry drift projection (#639; Python `project/prompt_status.py`, #254): label
 * resolution vs last-run versions for one workflow under one environment. Label-pinned prompts
 * are mutable by design (#192); drift requires BOTH sides known — an unknown side reports
 * `unknown`, never a false in-sync. Versions and names only; registry-managed prompt text never
 * serializes (inline registries excepted — the template is the user's own YAML).
 *
 * The TS control plane holds no registry client (thin-transport philosophy, #499). When a langfuse
 * transport is INJECTED (#573), the backend-registry tier resolves label drift live (label version vs
 * last-run version) exactly like Python; with none injected, label refs report an honest `unknown`
 * with a `detail` naming the missing seam — the same contract Python uses when its lookup fails.
 * Pinned refs are static and mirror Python exactly (plus the last-run version when observable).
 */

import type { PromptRefSpec, TypefluxYamlSpec } from "@typeflux/temporal-yaml";
import { stringify as stringifyYaml } from "yaml";

import { type LangfuseControlPlaneTransport, resolveLabelDrift } from "./langfuse-transport.js";

export type PromptDriftStatus = "in_sync" | "drift" | "unknown";

/** One prompt's drift row (Python `PromptStatus`; absent fields are Python's `exclude_none`). */
export interface ApiPromptStatus {
  name: string;
  mode: "pinned" | "label" | "inline";
  selector: string;
  registry_version?: string;
  last_run_version?: string;
  status: PromptDriftStatus;
  used_by_activities: string[];
  detail?: string;
  /** Inline registries only — the template text from the user's own YAML. */
  template?: string;
}

/** The workflow's prompt-status report (Python `WorkflowPromptStatus`). */
export interface ApiWorkflowPromptStatus {
  workflow_id: string;
  environment_id: string;
  registry_type: string;
  prompts: ApiPromptStatus[];
}

/** A normalized prompt ref: a bare-string `prompt:` is a name-only ref (Python `PromptRef`). */
type NormalizedRef = { name: string; version?: number | undefined; label?: string | undefined };

const normalizeRef = (prompt: string | PromptRefSpec): NormalizedRef =>
  typeof prompt === "string" ? { name: prompt } : prompt;

/**
 * Readable preview for inline templates: strings pass through; structured (chat) prompt specs
 * render as YAML, never a model repr. (This is `_render_template`'s documented contract — the
 * Python inline arm currently emits `str(model)` for structured specs, tracked as #648; string
 * templates, the conformance-pinned case, are identical in both editions.)
 */
const renderTemplate = (value: unknown): string =>
  typeof value === "string" ? value : stringifyYaml(value).trim();

/**
 * Project one workflow's prompt drift under the RESOLVED spec (environment overlays applied —
 * a per-environment registry override changes the answer). Mirrors Python
 * `workflow_prompt_status` tier by tier.
 */
export async function buildWorkflowPromptStatus(
  spec: TypefluxYamlSpec,
  workflowId: string,
  environmentId: string,
  transport?: LangfuseControlPlaneTransport,
): Promise<ApiWorkflowPromptStatus> {
  const registryType = spec.runtime.registry.type;
  const report = (prompts: ApiPromptStatus[]): ApiWorkflowPromptStatus => ({
    workflow_id: workflowId,
    environment_id: environmentId,
    registry_type: registryType,
    prompts,
  });

  // Group refs by prompt name over the SORTED activity names (Python iterates
  // `sorted(activities)`); `used_by_activities` accumulates in that order, and the prompt list
  // keeps first-appearance order. Every TS activity definition is AI (module activities have no
  // TS analogue, #496) — Python's `isinstance(activity, AIActivity)` filter is a no-op here.
  const definitions = [...(spec.activities.definitions ?? [])].sort((a, b) =>
    a.name < b.name ? -1 : a.name > b.name ? 1 : 0,
  );
  const refs = new Map<string, { ref: NormalizedRef; users: string[] }>();
  for (const definition of definitions) {
    const ref = normalizeRef(definition.prompt);
    const entry = refs.get(ref.name);
    if (entry === undefined) refs.set(ref.name, { ref, users: [definition.name] });
    else entry.users.push(definition.name);
  }

  if (registryType === "inline") {
    const inlineTemplates = spec.runtime.registry.prompts ?? {};
    return report(
      [...refs.values()].map(({ ref, users }) => ({
        name: ref.name,
        mode: "inline",
        selector: "inline",
        status: "in_sync",
        used_by_activities: users,
        ...(Object.hasOwn(inlineTemplates, ref.name)
          ? { template: renderTemplate(inlineTemplates[ref.name]) }
          : {}),
      })),
    );
  }

  if (registryType === "custom" || registryType === "langsmith") {
    // Resolves at runtime (by class, or by tag/commit) with no statically introspectable
    // templates or versions; report the requested selector with an honest unknown status.
    return report(
      [...refs.values()].map(({ ref, users }) => ({
        name: ref.name,
        mode: ref.version !== undefined ? "pinned" : "label",
        // Python `ref.label or "—"` — TRUTHINESS: an empty-string label reads "—" too.
        selector:
          ref.version !== undefined
            ? `v${ref.version}`
            : ref.label !== undefined && ref.label.length > 0
              ? ref.label
              : "—",
        status: "unknown",
        used_by_activities: users,
      })),
    );
  }

  // Backend registries (langfuse-tier): Python compares a LIVE label resolution against the latest
  // execution manifest. With a transport injected (#573) we do the same; with none, both sides are
  // unknown for label refs — honest `unknown` with the Python-shaped detail. Pinned refs are static
  // (always in_sync) and gain the last-run version when the observer is langfuse and observable.
  const registrySpec = spec.runtime.registry as { label?: string; host?: string };
  // Python `getattr(..., "label", None) or "production"` — TRUTHINESS: "" falls through too.
  const rawDefaultLabel = registrySpec.label;
  const defaultLabel =
    rawDefaultLabel !== undefined && rawDefaultLabel.length > 0 ? rawDefaultLabel : "production";
  const registryHost = registrySpec.host && registrySpec.host.length > 0 ? registrySpec.host : null;

  // Last-run versions are gated on the OBSERVER being langfuse (Python `_last_run_versions` only when
  // `observer == "langfuse"`), independent of the registry. Fetch once; shared across all prompts.
  // `host: null` is deliberate and NOT the registry host: the observer is a SEPARATE backend (it can
  // point at a different langfuse than the registry), and the observability spec carries no `host`
  // field — its host is resolved from env by the transport, exactly like Python's `from_env()`. A
  // self-hosted observer therefore needs `LANGFUSE_HOST` set, the same deployment contract as Python.
  const observerType = spec.runtime.observability?.type ?? "none";
  const lastRun: Record<string, string> =
    transport !== undefined && observerType === "langfuse"
      ? await transport
          .lastRunPromptVersions({ workflowName: spec.workflow.name, host: null })
          .catch((): Record<string, string> => ({}))
      : {};

  const rows = await Promise.all(
    [...refs.values()].map(async ({ ref, users }): Promise<ApiPromptStatus> => {
      // OWN-property lookup: `lastRun` is a plain object (an injected transport may return one), so a
      // prompt legitimately named like a prototype key (`toString`, `constructor`) must read `undefined`
      // when absent — never an inherited function — else drift misfires + a non-serializable version leaks.
      const lastRunVersion = Object.hasOwn(lastRun, ref.name) ? lastRun[ref.name] : undefined;
      if (ref.version !== undefined) {
        return {
          name: ref.name,
          mode: "pinned",
          selector: `v${ref.version}`,
          registry_version: String(ref.version),
          ...(lastRunVersion !== undefined ? { last_run_version: lastRunVersion } : {}),
          status: "in_sync",
          used_by_activities: users,
        };
      }
      // Python `ref.label if ref.label is not None else default_label` — IDENTITY, not truthiness:
      // an explicit empty-string label stays "" (the pinned "" parity rule).
      const label = ref.label !== undefined ? ref.label : defaultLabel;
      if (transport === undefined) {
        // No seam injected — honest unknown with the Python-shaped detail prefix.
        return {
          name: ref.name,
          mode: "label",
          selector: `@${label}`,
          status: "unknown",
          used_by_activities: users,
          detail:
            "registry lookup failed: the TypeScript control plane holds no registry client " +
            "(transports are injected, #499); inject a langfuse transport for live label drift (#573)",
        };
      }
      const verdict = await resolveLabelDrift(transport, {
        name: ref.name,
        label,
        host: registryHost,
        lastRunVersion,
      });
      return {
        name: ref.name,
        mode: "label",
        selector: `@${label}`,
        status: verdict.status,
        used_by_activities: users,
        ...(verdict.registryVersion !== undefined ? { registry_version: verdict.registryVersion } : {}),
        ...(lastRunVersion !== undefined ? { last_run_version: lastRunVersion } : {}),
        ...(verdict.detail !== undefined ? { detail: verdict.detail } : {}),
      };
    }),
  );
  return report(rows);
}
