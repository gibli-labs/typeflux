/**
 * Insights engine: pure derivation of severity-ranked, deep-linked findings
 * from the control-plane contract payloads. This is what makes the console
 * a console rather than a JSON viewer — every rule answers something a
 * power user would otherwise have to dig for, and links to where the
 * evidence lives.
 */

import type { Bundle, DrainStatus, ValidationIssue, WorkflowSummary } from "./api";
import {
  definitionSourceLinks,
  manifestSourceLinks,
  type RepoProvenance,
  type SourceLinkPair,
} from "./links";

export type Severity = "critical" | "warning" | "info" | "ok";

export interface Insight {
  id: string;
  severity: Severity;
  title: string;
  detail: string;
  /** Hash route the insight links to (including section anchors). */
  link: string;
  /** The source-of-truth file backing this insight (#718 §A / #577 §6): its blob at the resolved
   * sha + commit history, rendered beside the console link via {@link SourceLinks}. Present (even
   * as `{}`) when the insight names a file — so an un-provenanced project degrades LOUDLY to the
   * "no repo provenance" note rather than silently omitting the affordance. */
  sourceLinks?: SourceLinkPair;
  /** An EXTERNAL evidence hand-off for insights whose evidence lives in a system that owns the
   * detail rather than a single repo file (#727 §7): the GitHub compare view for change-correlated
   * HEAD drift. Rendered as an {@link ExtLink} beside the console link. Distinct from
   * `sourceLinks` (a {blob,history} file pair), and host-gated by the caller — never a hand-rolled
   * href (#719/#722). */
  evidence?: { href: string; label: string };
}

const SEVERITY_RANK: Record<Severity, number> = { critical: 0, warning: 1, info: 2, ok: 3 };

export function sortInsights(insights: Insight[]): Insight[] {
  return [...insights].sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);
}

function workflowLink(workflowId: string, env: string, section?: string): string {
  const anchor = section ? `&section=${section}` : "";
  return `#/workflows/${workflowId}?env=${env}${anchor}`;
}

/** Validation issue codes that block resolution/deployment outright. */
const CRITICAL_ISSUE_CODES = new Set([
  "missing_workflow_file",
  "workflow_load_error",
  "workflow_resolution_error",
  "policy_admission_failure",
  "invalid_component_profile",
  "unknown_profile_reference",
  "invalid_profile_selection",
  "unknown_policy_reference",
  "unknown_environment_reference",
]);

export function issueSeverity(issue: Pick<ValidationIssue, "code">): Severity {
  return CRITICAL_ISSUE_CODES.has(issue.code) ? "critical" : "warning";
}

/**
 * File-class validation failures (#577 §10): a declared workflow whose YAML is missing, fails to
 * load, or is malformed. For these the remediation is a source file — the manifest entry that
 * declares the workflow and the expected workflow YAML — so the row earns the same GitHub
 * source-link affordance every other feed carries. Other issue classes (policy composition,
 * unknown references) point elsewhere and are excluded.
 */
const FILE_CLASS_ISSUE_CODES = new Set([
  "missing_workflow_file",
  "workflow_load_error",
  "invalid_workflow_yaml",
]);

/** The source links a file-class validation row carries: the project manifest (which declares the
 * workflow — always present, possibly the empty pair) and, when the workflow's manifest-relative
 * path is known, the expected workflow YAML file. */
export interface ValidationIssueLinks {
  manifest: SourceLinkPair;
  workflowFile?: SourceLinkPair;
}

/**
 * The GitHub source links for a file-class validation issue (#577 §10), host-gated exactly like
 * every other feed via the `links.ts` builders. `undefined` for every other issue class (those
 * point elsewhere — the row renders a plain dash). For a file-class issue the pairs follow the
 * app-wide {@link SourceLinks} convention: a possibly-EMPTY pair degrades LOUDLY to the explicit
 * "source link unavailable (no repo provenance)" note, never a silently blank cell — so a missing
 * link is distinguishable from an issue that never carries one.
 *
 * - `manifest`: the project manifest blob+history at the served sha — the file that declares the
 *   workflow entry. Always present for a file-class issue (empty without provenance).
 * - `workflowFile`: the expected workflow YAML at its manifest-relative path, resolved from the
 *   workflow summary the issue's `reference` (the workflow id) names. Only workflows declared with
 *   an explicit `path` are link-safe; a `directory`-declared workflow's filename depends on the
 *   project's `defaults.workflow_filename`, which the read tier does not surface, so it is omitted
 *   rather than guessing a possibly-wrong path (the manifest pair still carries the loud note).
 */
export function validationIssueSourceLinks(
  issue: Pick<ValidationIssue, "code" | "reference">,
  project: RepoProvenance | undefined,
  workflows: Pick<WorkflowSummary, "id" | "path">[],
): ValidationIssueLinks | undefined {
  if (!FILE_CLASS_ISSUE_CODES.has(issue.code)) return undefined;
  const links: ValidationIssueLinks = { manifest: manifestSourceLinks(project) };
  const workflow = workflows.find((candidate) => candidate.id === issue.reference);
  if (workflow?.path) {
    links.workflowFile = definitionSourceLinks(project, workflow.path);
  }
  return links;
}

/** A declared secret reference — untyped in the schema, so the console reads it defensively.
 * `runtime_path` + `source_name` identify one slot; `configured` is its resolved state. */
export interface SecretRef {
  runtime_path?: string;
  source_name?: string;
  configured?: boolean;
}

/**
 * The declared secret references that resolve to nothing in this environment (`configured ===
 * false`). The ONE place "which secrets are unconfigured" is decided (#721 F7), shared by the
 * Overview insight feed and the security persona's Secrets dimension so the two can never
 * disagree on the verdict — callers format their own display (insight id vs cell label) from it.
 */
export function unconfiguredSecretRefs(bundle: Bundle): SecretRef[] {
  return ((bundle.secret_references ?? []) as SecretRef[]).filter((ref) => ref.configured === false);
}

/** Digest-pin state of a resolved bundle's previewed worker image: `pinned` (reproducible),
 * `mutable` (a tag — must be pinned), or `none` (no deployment preview to check). The one place
 * image-pin state is read, shared by the Overview insight and the persona Image-pin dimension. */
export type ImagePinState = "pinned" | "mutable" | "none";

export function imagePinState(bundle: Bundle): ImagePinState {
  const preview = bundle.deployment_preview as
    | { image_digest_pinned?: boolean }
    | null
    | undefined;
  if (!preview || preview.image_digest_pinned === undefined) return "none";
  return preview.image_digest_pinned ? "pinned" : "mutable";
}

export function deriveBundleInsights(workflowId: string, env: string, bundle: Bundle): Insight[] {
  const insights: Insight[] = [];

  for (const record of unconfiguredSecretRefs(bundle)) {
    insights.push({
      id: `${workflowId}:secret:${record.source_name ?? "unknown"}`,
      severity: "critical",
      title: `Secret ${record.source_name ?? "(unknown)"} is not configured`,
      detail:
        "A declared secret reference resolves to nothing in this environment; " +
        "the worker will fail preflight.",
      link: workflowLink(workflowId, env, "secrets"),
    });
  }

  if (!bundle.policy) {
    insights.push({
      id: `${workflowId}:policy:none`,
      severity: "warning",
      title: "No project policy applied",
      detail:
        "This workflow resolves without any policy bundle — provider, endpoint, " +
        "and worker constraints are unenforced.",
      link: workflowLink(workflowId, env, "policy"),
    });
  } else if (bundle.policy.applied_policy_ids.length === 0) {
    insights.push({
      id: `${workflowId}:policy:failed`,
      severity: "critical",
      title: "Policy composition failed",
      detail:
        `Selected policies (${bundle.policy.selected_policy_ids.join(", ")}) could not be ` +
        "composed; see validation checks for the conflict.",
      link: workflowLink(workflowId, env, "validation"),
    });
  }

  // Risk tiers (#300): the bundle's additive `risk_tier` posture — read defensively (the
  // pinned client types may predate the field). Surface the effective tier and any
  // UNSATISFIED macro requirements as severity; control NAMES only, never prompt/secret
  // content (the backend contributor is redaction-exempt for exactly this subset).
  const riskTier = (
    bundle as {
      risk_tier?: {
        effective?: string;
        requirements?: { name?: string; satisfied?: boolean }[];
        cascade?: { lifted_by?: string; effective?: string; requirements?: { name?: string; satisfied?: boolean }[] };
      };
    }
  ).risk_tier;
  if (riskTier && typeof riskTier.effective === "string") {
    // The top-level posture IS the enforced one (`effective` is always what admission
    // enforces — cascade-lifted when a child raises it), so the primary message reads the
    // top-level requirements; the cascade sentence only names WHO lifted the tier (its
    // requirement list mirrors the top level, so re-listing it would duplicate names).
    const unsatisfiedOf = (reqs: { name?: string; satisfied?: boolean }[] | undefined) =>
      (Array.isArray(reqs) ? reqs : [])
        .filter((req) => req && req.satisfied === false && typeof req.name === "string")
        .map((req) => req.name as string);
    const unsatisfied = unsatisfiedOf(riskTier.requirements);
    // Defensive: a foreign payload could carry cascade-only failures — they still gate.
    const cascadeUnsatisfied = unsatisfiedOf(riskTier.cascade?.requirements);
    const cascadeNote =
      riskTier.cascade && typeof riskTier.cascade.lifted_by === "string"
        ? ` Lifted to '${riskTier.cascade.effective ?? riskTier.effective}' by sub-workflow '${riskTier.cascade.lifted_by}'.`
        : "";
    // A cascade lift to `prohibited` denies admission just like the workflow's own tier.
    const denied =
      riskTier.effective === "prohibited" || riskTier.cascade?.effective === "prohibited";
    // ANY unsatisfied requirement means admission DENIAL (the check fails closed), so it
    // gets the same severity class as policy composition failures (`policy:failed`) —
    // critical, never a soft warning that undersells a blocked deploy.
    const severity: Severity =
      denied || unsatisfied.length > 0 || cascadeUnsatisfied.length > 0 ? "critical" : "info";
    insights.push({
      id: `${workflowId}:risk-tier`,
      severity,
      title: `Risk tier: ${riskTier.effective}`,
      detail: denied
        ? `Effective risk tier is 'prohibited' — admission is denied for this workflow.${cascadeNote}`
        : unsatisfied.length > 0 || cascadeUnsatisfied.length > 0
          ? `Tier '${riskTier.effective}' requires ${[...new Set([...unsatisfied, ...cascadeUnsatisfied])].join(", ")} — unsatisfied, so admission fails closed.${cascadeNote}`
          : `Tier '${riskTier.effective}' is satisfied by its own controls.${cascadeNote}`,
      link: workflowLink(workflowId, env, "policy"),
    });
  }

  if (bundle.validation && !bundle.validation.ok) {
    const issues = bundle.validation.issues ?? [];
    const critical = issues.filter((issue) => issueSeverity(issue) === "critical").length;
    insights.push({
      id: `${workflowId}:validation`,
      severity: critical > 0 ? "critical" : "warning",
      title: `Validation reports ${issues.length} issue${issues.length === 1 ? "" : "s"}`,
      detail: issues
        .slice(0, 3)
        .map((issue) => `${issue.code}: ${issue.message}`)
        .join(" · "),
      link: workflowLink(workflowId, env, "validation"),
    });
  }

  const preview = bundle.deployment_preview as
    | { error?: string }
    | null
    | undefined;
  if (preview?.error) {
    insights.push({
      id: `${workflowId}:deployment:error`,
      severity: "warning",
      title: "Deployment preview failed",
      detail: preview.error,
      link: workflowLink(workflowId, env, "deployment"),
    });
  } else if (imagePinState(bundle) === "mutable") {
    insights.push({
      id: `${workflowId}:deployment:mutable-image`,
      severity: "warning",
      title: "Deployment image is not digest-pinned",
      detail:
        "The previewed image is a mutable reference; production deployments " +
        "must pin by sha256 digest.",
      link: workflowLink(workflowId, env, "deployment"),
    });
  }

  const review = bundle.lifecycle?.review;
  if (review && review.invalid_user_decision === "warn") {
    insights.push({
      id: `${workflowId}:review:warn-mode`,
      severity: "info",
      title: "Review gate continues on invalid decisions",
      detail:
        "invalid_user_decision is 'warn': an unrecognized decision logs and falls " +
        "through to the next step. Use 'fail' for fail-closed review gates.",
      link: workflowLink(workflowId, env, "lifecycle"),
    });
  }
  // Multi-gate workflows (#55 slice 4): the bundle's additive `gates` list — read defensively
  // (the pinned client types may predate the field) and surface the same warn-mode insight
  // per gate, named by its id.
  const gates = (bundle.lifecycle as { gates?: { id?: string; invalid_user_decision?: string }[] } | undefined)
    ?.gates;
  for (const gate of Array.isArray(gates) ? gates : []) {
    if (gate && typeof gate.id === "string" && gate.invalid_user_decision === "warn") {
      insights.push({
        id: `${workflowId}:review:${gate.id}:warn-mode`,
        severity: "info",
        title: `Review gate '${gate.id}' continues on invalid decisions`,
        detail:
          "invalid_user_decision is 'warn': an unrecognized decision logs and falls " +
          "through to the next step. Use 'fail' for fail-closed review gates.",
        link: workflowLink(workflowId, env, "lifecycle"),
      });
    }
  }

  return sortInsights(insights);
}

/** The versioned workflow types still carrying running executions on something OTHER than the
 * current spec's type, and the total run count across them. The ONE place "which old versions are
 * not yet drained" is decided (#577 §1), shared by the per-workflow drain insight
 * ({@link deriveDrainInsights}) and the project Drift page's version-drain class
 * ({@link deriveDrainDrift}) so the two never disagree on what counts as un-drained. */
export interface DrainStale {
  staleTypes: string[];
  staleRuns: number;
}

export function drainStale(drain: DrainStatus): DrainStale {
  const staleTypes = Object.keys(drain.running).filter(
    (workflowType) => workflowType !== drain.current_workflow_type,
  );
  return { staleTypes, staleRuns: staleTypes.reduce((sum, key) => sum + drain.running[key], 0) };
}

export function deriveDrainInsights(
  workflowId: string,
  env: string,
  drain: DrainStatus,
): Insight[] {
  const insights: Insight[] = [];
  const { staleTypes, staleRuns } = drainStale(drain);
  if (staleTypes.length > 0) {
    insights.push({
      id: `${workflowId}:drain:stale`,
      severity: "warning",
      title: `Version drift: ${staleRuns} execution${staleRuns === 1 ? "" : "s"} on ${staleTypes.length} old version${staleTypes.length === 1 ? "" : "s"}`,
      detail:
        "Executions are still running on workflow types other than the current spec " +
        "digest. Old versions are not safe to decommission until drained.",
      link: `#/workflows/${workflowId}/versions?env=${env}`,
    });
  } else {
    insights.push({
      id: `${workflowId}:drain:ok`,
      severity: "ok",
      title: drain.total_running > 0 ? "All executions on the current version" : "Drained",
      detail:
        drain.total_running > 0
          ? `${drain.total_running} running execution${drain.total_running === 1 ? "" : "s"}, all on ${drain.current_workflow_type}.`
          : "No old-version executions are running; previous versions are safe to decommission.",
      link: `#/workflows/${workflowId}/versions?env=${env}`,
    });
  }
  return insights;
}
