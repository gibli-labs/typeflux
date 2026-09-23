/**
 * Persona engine (#721 / #577 §5): pure, unit-tested derivations for the four read-only persona
 * landing views — governance, security posture, operations, executive. Every rollup and
 * percentage lives here (the `insights.ts` contract), never buried in JSX, so the numbers a
 * compliance owner, security reviewer, SRE, or executive reads are testable in isolation.
 *
 * Each view COMPOSES existing read-tier data (the Phase-0 query cache: bundle matrix, policy
 * definitions, cross-workflow executions) — no new fetch patterns. Where a control plane does
 * not report a field (a TS-CP bundle that omits a runtime knob), the derivation yields an
 * explicit `unknown`/absent state so the view degrades LOUDLY to "not reported by this control
 * plane" rather than a false-negative green.
 */

import type { Bundle, PolicyDefinition } from "./api";
import type { CoverageRow } from "./governance";
import { imagePinState, unconfiguredSecretRefs } from "./insights";
import type { Insight, SecretRef } from "./insights";
import { bundleSourceLinks } from "./links";
import type { SourceLinkPair } from "./links";
import type { ExecutionsCell } from "./queries";
import type { BundleCell } from "./queries";
import { statusRank } from "./runsFeed";

// ── Governance rollup ───────────────────────────────────────────────────────

/**
 * One workflow's governance verdict across its resolved environments:
 * - `governed`   — every resolved environment applies a policy;
 * - `partial`    — some resolved environments are covered, others are none;
 * - `ungoverned` — every resolved environment applies NO policy;
 * - `failed`     — a policy is selected somewhere but composition failed (nothing enforced);
 * - `unassessable` — the workflow resolves in no environment, so coverage is unknown.
 * `failed` outranks `partial`/`ungoverned`: an unenforced-but-attempted policy is the loud case.
 */
export type GovernanceStatus =
  | "governed"
  | "partial"
  | "ungoverned"
  | "failed"
  | "unassessable";

export interface GovernanceWorkflowRow {
  workflowId: string;
  status: GovernanceStatus;
  /** Environments (sorted) with no policy applied — the "none" list, per env. */
  ungovernedEnvs: string[];
  /** Environments (sorted) whose policy composition failed. */
  failedEnvs: string[];
}

export interface GovernanceRollup {
  workflowCount: number;
  /** Workflows with at least one resolved environment (the denominator for coverage). */
  assessedCount: number;
  governedCount: number;
  partialCount: number;
  ungovernedCount: number;
  failedCount: number;
  /** governed / assessed, 0–100; 100 ONLY when every assessed workflow is governed, else
   * floor-capped at 99; null when nothing is assessable. */
  coveredPct: number | null;
  /** Per-workflow verdicts (governance/partial/ungoverned first, then id) for the table. */
  rows: GovernanceWorkflowRow[];
}

/**
 * Coverage percentage with an honest 100% boundary (#721 F3): reserve 100 for the case where
 * EVERY assessed workflow is governed — otherwise `Math.round` promotes 999/1000 to a green
 * "100% / every workflow governed" narrative. Any shortfall floors and caps at 99 so a single
 * ungoverned workflow can never round away. Null when nothing is assessable.
 */
export function coveragePercent(governedCount: number, assessedCount: number): number | null {
  if (assessedCount === 0) return null;
  if (governedCount >= assessedCount) return 100;
  return Math.min(99, Math.floor((governedCount / assessedCount) * 100));
}

function governanceStatusOf(row: CoverageRow): GovernanceWorkflowRow {
  const entries = Object.entries(row.byEnv);
  const resolved = entries.filter(([, cell]) => cell.state !== "unresolvable");
  const ungovernedEnvs = resolved
    .filter(([, cell]) => cell.state === "none")
    .map(([env]) => env)
    .sort();
  const failedEnvs = resolved
    .filter(([, cell]) => cell.state === "composition_failed")
    .map(([env]) => env)
    .sort();
  const coveredEnvs = resolved.filter(([, cell]) => cell.state === "covered");
  let status: GovernanceStatus;
  if (resolved.length === 0) status = "unassessable";
  else if (failedEnvs.length > 0) status = "failed";
  else if (coveredEnvs.length === resolved.length) status = "governed";
  else if (coveredEnvs.length === 0) status = "ungoverned";
  else status = "partial";
  return { workflowId: row.workflowId, status, ungovernedEnvs, failedEnvs };
}

const GOVERNANCE_STATUS_RANK: Record<GovernanceStatus, number> = {
  failed: 0,
  ungoverned: 1,
  partial: 2,
  unassessable: 3,
  governed: 4,
};

/**
 * The coverage rollup a compliance owner leads with: how many workflows are under policy, how
 * many are not, and the exact none-list — derived from the SAME resolved-bundle coverage the
 * Governance page's matrix uses, so the headline number and the grid can never disagree.
 */
export function deriveGovernanceRollup(coverage: CoverageRow[]): GovernanceRollup {
  const rows = coverage.map(governanceStatusOf).sort(
    (a, b) =>
      GOVERNANCE_STATUS_RANK[a.status] - GOVERNANCE_STATUS_RANK[b.status] ||
      a.workflowId.localeCompare(b.workflowId),
  );
  const governedCount = rows.filter((row) => row.status === "governed").length;
  const partialCount = rows.filter((row) => row.status === "partial").length;
  const ungovernedCount = rows.filter((row) => row.status === "ungoverned").length;
  const failedCount = rows.filter((row) => row.status === "failed").length;
  const assessedCount = rows.filter((row) => row.status !== "unassessable").length;
  return {
    workflowCount: rows.length,
    assessedCount,
    governedCount,
    partialCount,
    ungovernedCount,
    failedCount,
    coveredPct: coveragePercent(governedCount, assessedCount),
    rows,
  };
}

// ── Security posture ────────────────────────────────────────────────────────

/**
 * A security control's state in one resolved bundle:
 * - `on`  — the control is present and enabled (redaction on, TLS on, image digest-pinned);
 * - `off` — present but disabled/weak (redaction off, mutable image, a missing secret);
 * - `na`  — not applicable (no secrets declared, no deployment preview to pin);
 * - `unknown` — the control plane did not report this field (loud "not reported" degradation).
 */
export type PostureState = "on" | "off" | "na" | "unknown";

export interface SecurityDimension {
  key: string;
  state: PostureState;
  detail: string;
}

export interface SecurityRow {
  workflowId: string;
  env: string;
  resolved: boolean;
  dimensions: SecurityDimension[];
  /** Source-of-truth links for the workflow's own YAML/code file, or `{}` (loud unavailable). */
  sourceLinks: SourceLinkPair;
}

/** Read a nested runtime section defensively — a TS-CP bundle may omit it entirely. */
function runtimeSection(bundle: Bundle, key: string): Record<string, unknown> | undefined {
  const value = (bundle.runtime as Record<string, unknown> | undefined)?.[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function boolDimension(
  key: string,
  raw: unknown,
  onDetail: string,
  offDetail: string,
): SecurityDimension {
  if (raw === true) return { key, state: "on", detail: onDetail };
  if (raw === false) return { key, state: "off", detail: offDetail };
  return {
    key,
    state: "unknown",
    detail: "This control plane does not report this control for the resolved bundle.",
  };
}

function secretsDimension(bundle: Bundle): SecurityDimension {
  const refs = (bundle.secret_references ?? []) as SecretRef[];
  if (refs.length === 0) {
    return {
      key: "secrets",
      state: "na",
      detail: "No secret references are declared for this workflow in this environment.",
    };
  }
  // The unconfigured-secret verdict is owned by insights.ts so the security dimension and the
  // Overview insight feed can never disagree on WHICH secrets are missing (#721 F7).
  const missing = unconfiguredSecretRefs(bundle);
  if (missing.length === 0) {
    return {
      key: "secrets",
      state: "on",
      detail: `All ${refs.length} declared secret reference${refs.length === 1 ? "" : "s"} resolve to a configured value.`,
    };
  }
  return {
    key: "secrets",
    state: "off",
    detail: `${missing.length} of ${refs.length} secret reference${refs.length === 1 ? "" : "s"} are not configured (${missing
      .map((ref) => ref.source_name || ref.runtime_path || "secret")
      .join(", ")}); the worker fails preflight.`,
  };
}

function imageDimension(bundle: Bundle): SecurityDimension {
  // Image-pin state is owned by insights.ts (#721 F7), shared with the Overview insight feed.
  const state = imagePinState(bundle);
  if (state === "none") {
    return {
      key: "image_pin",
      state: "na",
      detail:
        "No deployment preview is resolved here, so there is no worker image to check for a digest pin.",
    };
  }
  return boolDimension(
    "image_pin",
    state === "pinned",
    "The previewed worker image is pinned to a content digest (reproducible).",
    "The previewed worker image is a mutable tag — pin it by sha256 digest.",
  );
}

/**
 * Per-(workflow, environment) security posture from resolved bundles: redaction, execution
 * manifest, Temporal TLS, provider-key configuration, secret configured-state, and image pinning
 * — each read defensively so an absent field degrades to `unknown` (not a false pass). One row
 * per resolved bundle; unresolvable cells are surfaced separately (resolved=false) so an outage
 * is never a green posture.
 */
export function deriveSecurityPosture(cells: BundleCell[]): SecurityRow[] {
  return [...cells]
    .sort(
      (a, b) => a.workflowId.localeCompare(b.workflowId) || a.env.localeCompare(b.env),
    )
    .map((cell) => {
      const bundle = cell.bundle;
      if (!bundle || cell.error) {
        return {
          workflowId: cell.workflowId,
          env: cell.env,
          resolved: false,
          dimensions: [],
          sourceLinks: {},
        };
      }
      const observability = runtimeSection(bundle, "observability");
      const temporal = runtimeSection(bundle, "temporal");
      const provider = runtimeSection(bundle, "provider");
      const dimensions: SecurityDimension[] = [
        boolDimension(
          "redaction",
          observability?.["redaction_enabled"],
          "Trace payload redaction is enabled for this workflow.",
          "Trace payload redaction is OFF — prompt/response payloads reach the observer unredacted.",
        ),
        boolDimension(
          "manifest",
          observability?.["execution_manifest"],
          "The execution manifest is written for run↔trace correlation.",
          "The execution manifest is off — runs cannot be correlated to a manifest hash.",
        ),
        boolDimension(
          "tls",
          temporal?.["tls_enabled"],
          "The Temporal transport uses TLS.",
          "The Temporal transport does not use TLS (expected for a local dev cluster).",
        ),
        boolDimension(
          "provider_key",
          provider?.["api_key_configured"],
          "The AI provider API key resolves from a configured secret.",
          "The AI provider API key is not configured in this environment.",
        ),
        secretsDimension(bundle),
        imageDimension(bundle),
      ];
      return {
        workflowId: cell.workflowId,
        env: cell.env,
        resolved: true,
        dimensions,
        sourceLinks: bundleSourceLinks(bundle),
      };
    });
}

export interface SecurityRollup {
  resolvedRows: number;
  unresolvedRows: number;
  redactionOff: number;
  tlsOff: number;
  missingSecrets: number;
  unpinnedImages: number;
  providerKeyMissing: number;
}

/** The headline counts a security reviewer scans first: how many resolved bundles have a weak
 * control. Only `off` states count (never `unknown`/`na` — an unreported control is not a
 * proven weakness, and is surfaced as its own explicit state in the table). */
export function deriveSecurityRollup(rows: SecurityRow[]): SecurityRollup {
  const resolved = rows.filter((row) => row.resolved);
  const off = (key: string) =>
    resolved.filter((row) => row.dimensions.some((d) => d.key === key && d.state === "off")).length;
  return {
    resolvedRows: resolved.length,
    unresolvedRows: rows.length - resolved.length,
    redactionOff: off("redaction"),
    tlsOff: off("tls"),
    missingSecrets: off("secrets"),
    unpinnedImages: off("image_pin"),
    providerKeyMissing: off("provider_key"),
  };
}

/** A policy-enforced security control, from a policy DEFINITION's rule sections (#188/#577 §2):
 * redaction requirement, module import allowlist, artifact source allowlist. Presence-based —
 * the effective merged values stay the backend's job. */
export interface PolicySecurityControl {
  policyId: string;
  policyName: string;
  /** True when redaction is required for this policy — either directly
   * (`observability.redaction.required`) OR gated behind a risk tier
   * (`risk_tiers.<tier>.require_redaction`). */
  redactionRequired: boolean;
  /** True when the redaction requirement comes ONLY from a risk tier, never a direct
   * requirement. Which workflows a tier applies to is workflow-dependent (the effective tier is
   * `max(declared, min_tier)` per `policy_enforcement.evaluate_risk_tier`), so the honest
   * per-POLICY answer is "required (risk-tier)" rather than a flat "required" (#721 F4). */
  redactionTierGated: boolean;
  hasImportAllowlist: boolean;
  hasArtifactAllowlist: boolean;
  sourceLinks: SourceLinkPair;
}

function isPresent(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return true; // an explicit [] is a deny-all — present
  if (typeof value === "object") return Object.keys(value as object).length > 0;
  return true;
}

export function derivePolicySecurityControl(
  definition: PolicyDefinition,
  sourceLinks: SourceLinkPair,
): PolicySecurityControl {
  const rules = (definition.rules ?? {}) as Record<string, unknown>;
  const observability = (rules["observability"] ?? {}) as Record<string, unknown>;
  const redaction = (observability["redaction"] ?? {}) as Record<string, unknown>;
  const imports = (rules["imports"] ?? {}) as Record<string, unknown>;
  const artifacts = (rules["artifacts"] ?? {}) as Record<string, unknown>;
  // A risk-tier block (`risk_tiers.<tier>`) can demand redaction even when the direct
  // `observability.redaction.required` is null — the bundled base policy does exactly this
  // (`policy_gated.require_redaction: true`), and #300 fails closed on it. `risk_tiers` also
  // carries non-tier scalars (`min_tier`, `require_declared`); only its object tier blocks count.
  const riskTiers = (rules["risk_tiers"] ?? {}) as Record<string, unknown>;
  const directRedaction = redaction["required"] === true;
  const tierRedaction = Object.values(riskTiers).some(
    (tier) =>
      tier !== null &&
      typeof tier === "object" &&
      !Array.isArray(tier) &&
      (tier as Record<string, unknown>)["require_redaction"] === true,
  );
  return {
    policyId: definition.id,
    policyName: definition.name,
    redactionRequired: directRedaction || tierRedaction,
    redactionTierGated: !directRedaction && tierRedaction,
    hasImportAllowlist:
      isPresent(imports["allowed_module_roots"]) ||
      isPresent(imports["allow_provider_class"]) ||
      isPresent(imports["allow_absolute_activity_modules"]),
    hasArtifactAllowlist: isPresent(artifacts["allowed_sources"]),
    sourceLinks,
  };
}

// ── Operations ──────────────────────────────────────────────────────────────

export interface OperationsSummary {
  totalWorkflows: number;
  /** Workflows whose executions were readable (no per-workflow error). */
  readableWorkflows: number;
  /** Workflows whose executions could not be read (Temporal unreachable) — outage honesty. */
  unavailableWorkflows: string[];
  totalExecutions: number;
  failing: number;
  running: number;
  completed: number;
  /** Executions on a workflow type other than the current spec digest — the drain signal
   * (#191), derived from the execution records themselves, no extra fan-out. */
  oldVersionRuns: number;
}

/**
 * The failure-first operational rollup an SRE leads with, composed over the SAME cross-workflow
 * executions matrix the Runs surface renders (#589) — no new fetch. `failing` counts terminal
 * failure states (failed/terminated/timed-out/canceled) via the shared `statusRank`; drain is
 * read off `current_version` per record rather than a separate versions fan-out.
 */
export function deriveOperationsSummary(cells: ExecutionsCell[]): OperationsSummary {
  const unavailableWorkflows = cells
    .filter((cell) => cell.error)
    .map((cell) => cell.workflowId)
    .sort();
  let failing = 0;
  let running = 0;
  let completed = 0;
  let oldVersionRuns = 0;
  let totalExecutions = 0;
  for (const cell of cells) {
    for (const record of cell.executions) {
      totalExecutions += 1;
      const rank = statusRank(record.status);
      if (rank === 0) failing += 1;
      else if (rank === 1) running += 1;
      else if (rank === 3) completed += 1;
      // rank 2 (e.g. waiting_review) is neither failing/running/completed and is not a headline.
      if (record.current_version === false) oldVersionRuns += 1;
    }
  }
  return {
    totalWorkflows: cells.length,
    readableWorkflows: cells.length - unavailableWorkflows.length,
    unavailableWorkflows,
    totalExecutions,
    failing,
    running,
    completed,
    oldVersionRuns,
  };
}

// ── Executive rollup ────────────────────────────────────────────────────────

export interface ExecutiveRollup {
  workflowCount: number;
  environmentCount: number;
  policyCount: number;
  /** % of assessable workflows fully under policy (from the governance rollup). */
  underPolicyPct: number | null;
  governedWorkflows: number;
  ungovernedWorkflows: number;
  /** Workflows validating cleanly in the selected environment. */
  validatingWorkflows: number;
  workflowsWithIssues: number;
  /** Workflows unresolvable in the selected environment (an integration/config gap). */
  unresolvableWorkflows: number;
  /** Combined CRITICAL drift count (#721 F5): cross-env admission splits + version/governance
   * environment drift + drifted approved plans — the same criticals the Drift page renders.
   * Prompt/registry drift is warning-class and stays on the Drift page, uncounted here. */
  driftCount: number;
  /** Workflows carrying a human review gate — the review load. */
  reviewGateWorkflows: number;
}

/**
 * The executive "critical drift" headline (#721 F5): the count of CRITICAL findings across the
 * three drift engines a promote actually breaks on — cross-environment admission splits, pairwise
 * version/governance environment drift, and drifted approved deployment plans. Each source is the
 * SAME derivation its operator surface renders (no forked logic); this only filters to critical
 * and sums, so the executive number can never exceed what the Drift page shows. Prompt-registry
 * drift is warning-class by construction and is deliberately excluded.
 */
export function deriveCriticalDrift(input: {
  crossEnv: Insight[];
  environmentDrift: Insight[];
  planDrift: Insight[];
}): number {
  const criticals = (insights: Insight[]) =>
    insights.filter((insight) => insight.severity === "critical").length;
  return (
    criticals(input.crossEnv) + criticals(input.environmentDrift) + criticals(input.planDrift)
  );
}

/** Does the selected-environment bundle carry a human review gate (single or multi-gate)? */
function hasReviewGate(bundle: Bundle | undefined): boolean {
  if (!bundle) return false;
  const lifecycle = bundle.lifecycle as
    | { review?: unknown; gates?: unknown[] }
    | null
    | undefined;
  if (!lifecycle) return false;
  if (lifecycle.review) return true;
  return Array.isArray(lifecycle.gates) && lifecycle.gates.length > 0;
}

/**
 * The plain-language leadership rollup (#577 §5): counts and percentages an executive reads
 * without digests — governance coverage, validation health, drift, review load. Composes the
 * governance rollup and the combined critical-drift count (see {@link deriveCriticalDrift}) so the
 * executive numbers agree with the operator surfaces they summarize.
 */
export function deriveExecutiveRollup(input: {
  workflowCount: number;
  environmentCount: number;
  policyCount: number;
  governance: GovernanceRollup;
  driftCount: number;
  /** The selected-environment bundle per workflow (for validation + review load). */
  selectedEnvCells: BundleCell[];
}): ExecutiveRollup {
  const { governance } = input;
  const resolved = input.selectedEnvCells.filter((cell) => cell.bundle && !cell.error);
  const validatingWorkflows = resolved.filter((cell) => cell.bundle?.validation?.ok).length;
  const workflowsWithIssues = resolved.length - validatingWorkflows;
  const unresolvableWorkflows = input.selectedEnvCells.length - resolved.length;
  const reviewGateWorkflows = resolved.filter((cell) => hasReviewGate(cell.bundle)).length;
  return {
    workflowCount: input.workflowCount,
    environmentCount: input.environmentCount,
    policyCount: input.policyCount,
    underPolicyPct: governance.coveredPct,
    governedWorkflows: governance.governedCount,
    ungovernedWorkflows: governance.ungovernedCount + governance.partialCount + governance.failedCount,
    validatingWorkflows,
    workflowsWithIssues,
    unresolvableWorkflows,
    driftCount: input.driftCount,
    reviewGateWorkflows,
  };
}
