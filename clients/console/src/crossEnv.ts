/**
 * Cross-environment insight engine (#604): compares ONE workflow's resolved state across
 * every project environment and surfaces the splits that per-bundle insights (one env at a
 * time) can never see — "admits in prod but fails in staging" is the promote-would-break
 * signal, and it belongs in the Overview feed, not behind a manual diff.
 *
 * Follows the drift-feed conventions exactly: an environment where the workflow does not
 * resolve is a deduped NOTE (outage honesty), never an insight; nothing derives until the
 * bundle matrix settles, so a partial fan-out cannot flash a false split. One insight per
 * workflow per class keeps the flat feed dedup-by-construction.
 *
 * Deliberately excluded: spec-digest splits — `deriveEnvironmentDrift` owns cross-env
 * digest comparison on the Drift page; a second engine reporting the same fact would
 * double-feed it.
 */

import type { Insight } from "./insights";
import { sortInsights } from "./insights";
import type { BundleCell } from "./queries";

export interface CrossEnvFeed {
  insights: Insight[];
  /** "workflow does not resolve in env" notes, deduped — rendered as a hint, not alarms. */
  notes: string[];
  /** True while any matrix cell is in flight — first load OR a background refetch of
   * cached data (codex: isFetching with isPending false can transiently mix stale and
   * fresh cells). The caller must render the feed as still-comparing, never as an
   * all-clear; already-derived insights stay visible during refetches. */
  pending: boolean;
}

/** The `secret_references` entries are untyped in the schema; the same convention cast
 * `insights.ts` uses. `runtime_path` + `source_name` identify one slot across bundles. */
interface SecretRef {
  runtime_path?: string;
  source_kind?: string;
  source_name?: string;
  configured?: boolean;
}

const secretRefs = (cell: BundleCell): SecretRef[] =>
  (cell.bundle?.secret_references ?? []) as SecretRef[];

/** Sorted selected-policy ids; `null` policy = no selection (empty). */
const selectedPolicies = (cell: BundleCell): string[] =>
  [...(cell.bundle?.policy?.selected_policy_ids ?? [])].sort();

const envList = (envs: string[]): string => envs.join(", ");

export function deriveCrossEnvInsights(cells: BundleCell[]): CrossEnvFeed {
  const insights: Insight[] = [];
  const notes: string[] = [];
  // FIRST load (no data yet) blanks the feed; a background refetch keeps deriving from
  // the cached cells but reports pending so the caller shows the still-comparing hint —
  // a transient mixed-stale state self-corrects on settle without flashing an all-clear.
  if (cells.some((cell) => cell.pending)) return { insights: [], notes: [], pending: true };
  const refreshing = cells.some((cell) => cell.fetching);

  const byWorkflow = new Map<string, BundleCell[]>();
  for (const cell of cells) {
    byWorkflow.set(cell.workflowId, [...(byWorkflow.get(cell.workflowId) ?? []), cell]);
  }

  for (const [workflowId, workflowCells] of byWorkflow) {
    // A cell with an ERROR is unresolvable even when React Query still holds stale data
    // from an earlier success — comparing a snapshot of an environment that has stopped
    // answering could report a false split (codex).
    const resolved = workflowCells.filter(
      (cell) => cell.bundle !== undefined && cell.error === undefined,
    );
    for (const cell of workflowCells) {
      if (cell.bundle === undefined || cell.error !== undefined) {
        notes.push(`${workflowId} does not resolve in ${cell.env}`);
      }
    }
    // A split needs at least two RESOLVED environments to compare.
    if (resolved.length < 2) continue;

    // Admission split (critical): validation.ok differs — a promote would break.
    const passing = resolved.filter((cell) => cell.bundle?.validation.ok);
    const failing = resolved.filter((cell) => !cell.bundle?.validation.ok);
    if (passing.length > 0 && failing.length > 0) {
      const firstFailing = failing[0];
      const codes = [
        ...new Set(
          failing.flatMap((cell) => (cell.bundle?.validation.issues ?? []).map((issue) => issue.code)),
        ),
      ].slice(0, 3);
      insights.push({
        id: `crossenv:${workflowId}:admission`,
        severity: "critical",
        title: `${workflowId} admits in ${envList(passing.map((c) => c.env))} but fails validation in ${envList(failing.map((c) => c.env))}`,
        detail:
          "The same workflow is deployable in one environment and rejected in another — a promote " +
          `along that path would break. Failing checks: ${codes.length > 0 ? codes.join(", ") : "see the validation view"}. ` +
          "Fix the failing environment's overrides/profiles or the policy selection until both admit.",
        link: `#/workflows/${workflowId}?env=${firstFailing.env}&section=validation`,
      });
    }

    // Policy-selection split (warning): different governance per environment. The map key
    // is the LOSSLESS JSON of the sorted id array (codex: a joined display string could
    // collapse distinct sets when an id itself contains ", "); display derives separately.
    const fingerprints = new Map<string, { ids: string[]; envs: string[] }>();
    for (const cell of resolved) {
      const ids = selectedPolicies(cell);
      const key = JSON.stringify(ids);
      const group = fingerprints.get(key) ?? { ids, envs: [] };
      group.envs.push(cell.env);
      fingerprints.set(key, group);
    }
    if (fingerprints.size > 1) {
      const display = (ids: string[]): string => (ids.length > 0 ? ids.join(", ") : "(none)");
      const parts = [...fingerprints.values()].map(
        (group) => `${envList(group.envs)}: ${display(group.ids)}`,
      );
      // Link the environment with the FEWEST policies — the likelier gap.
      const sparsest = [...fingerprints.values()].sort((a, b) => a.ids.length - b.ids.length)[0];
      insights.push({
        id: `crossenv:${workflowId}:policy`,
        severity: "warning",
        title: `${workflowId} runs under different policies per environment`,
        detail:
          `Selected policies differ — ${parts.join("; ")}. Governance that holds in one ` +
          "environment is not holding in another; align the validation targets or per-environment " +
          "workflow_profiles unless the split is intentional.",
        link: `#/workflows/${workflowId}?env=${sparsest.envs[0]}&section=policy`,
      });
    }

    // Secret-coverage split (warning): the SAME slot declared everywhere but configured in
    // only some environments. A slot absent from an environment's resolution entirely is a
    // WIRING difference (info): often a legitimate per-environment profile/provider swap,
    // but worth a visible pointer since the dependency sets differ (finder).
    const slots = new Map<string, { configured: string[]; missing: string[]; label: string }>();
    for (const cell of resolved) {
      for (const ref of secretRefs(cell)) {
        const key = `${ref.runtime_path ?? ""}|${ref.source_name ?? ""}`;
        const label = ref.source_name || ref.runtime_path || "secret";
        const slot = slots.get(key) ?? { configured: [], missing: [], label };
        (ref.configured ? slot.configured : slot.missing).push(cell.env);
        slots.set(key, slot);
      }
    }
    for (const [key, slot] of slots) {
      const declaring = [...slot.configured, ...slot.missing];
      const absent = resolved.map((cell) => cell.env).filter((e) => !declaring.includes(e));
      if (slot.configured.length > 0 && slot.missing.length > 0) {
        insights.push({
          id: `crossenv:${workflowId}:secret:${key}`,
          severity: "warning",
          title: `${slot.label} is configured in ${envList(slot.configured)} but not in ${envList(slot.missing)}`,
          detail:
            `${workflowId} declares this secret in ${envList(declaring)}, but it only resolves in ` +
            `${envList(slot.configured)} — runs in ${envList(slot.missing)} will fail at the first ` +
            "provider/Temporal call. Set the variable (or file) in the missing environment's profile.",
          link: `#/workflows/${workflowId}?env=${slot.missing[0]}&section=secrets`,
        });
      }
      if (absent.length > 0 && declaring.length > 0) {
        insights.push({
          id: `crossenv:${workflowId}:secret-wiring:${key}`,
          severity: "info",
          title: `${slot.label} is referenced in ${envList(declaring)} but absent from ${envList(absent)}'s resolution`,
          detail:
            "The environments resolve different secret dependency sets — usually a per-environment " +
            "profile/provider swap, which is fine when intentional. If both should use the same " +
            "provider, check the profile selection in the environment that lacks the reference.",
          link: `#/workflows/${workflowId}?env=${absent[0]}&section=secrets`,
        });
      }
    }
  }

  return { insights: sortInsights(insights), notes: [...new Set(notes)], pending: refreshing };
}
