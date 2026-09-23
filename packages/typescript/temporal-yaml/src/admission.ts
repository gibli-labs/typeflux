/**
 * Spec ADMISSION seam (#298 Phase B, design D298-3; Python `project/admission.py`).
 *
 * {@link admitSpec} decides whether a spec — one authored by an agent or submitted from
 * outside the trusted filesystem — may run under a project's governance. It is NOT a
 * parallel enforcement engine: it parses the spec with the same bounded loader every
 * workflow uses ({@link loadYamlSpec}, `MAX_YAML_BYTES`), resolves the composed policy
 * the target (environment, workflow slot) would apply, and runs the EXISTING
 * deterministic check pipeline ({@link validatePolicyCompliance} + composition ceilings
 * + {@link validateSubworkflowClosurePolicy}). The result is a typed {@link AdmissionReport}
 * of the same per-check shape the validate report already produces.
 *
 * Origin is one bit (design D298-2). `origin: "external"` is the hostile-input posture.
 * In Python this is where a spec that declares `activities.modules` or a custom
 * `class:` is rejected structurally (arbitrary code execution). The TS SDK loads no
 * modules — providers/registries/observers/moderators are injected — and its spec
 * loader already REJECTS those `class:`/`modules:` surfaces at parse (#496), so by the
 * time a spec parses there is no import surface left to police. The
 * `admission_external_modules_forbidden` check therefore reports PASSED (structurally
 * satisfied by the injection model), keeping the report shape at parity with Python.
 *
 * There is no CLI convention in the TS packages (the enforcement API is
 * injection-based), so this exported function IS the seam; a control-plane upload
 * endpoint is deferred (Phase C) until a consumer exists.
 */

import { loadYamlSpec, type LoadYamlSpecOptions } from "./loader.js";
import type { PolicyValidationCheck } from "./policy-enforcement.js";
import { validatePolicyCompliance } from "./policy-enforcement.js";
import {
  riskTierBindingVerdict,
  type SelectProjectPolicyIdsOptions,
  type SubworkflowClosureSpecResolver,
  selectProjectPolicyIdsForWorkflow,
  validateSubworkflowClosurePolicy,
} from "./project-enforcement.js";
import { composeProjectPolicyIds, type ProjectPolicySources } from "./project-resolve.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import type { TypefluxYamlSpec } from "./spec.js";

export type SpecOrigin = "operator" | "external";

/** The typed outcome of {@link admitSpec} (Python `AdmissionReport`). */
export interface AdmissionReport {
  /** True only when no check failed. */
  admitted: boolean;
  specOrigin: SpecOrigin;
  environmentId: string;
  /** The resolved target slot (the explicit `workflowId` option, else the spec's own `name`). */
  workflowId?: string;
  policyHash?: string;
  /** The per-check evidence, reusing the enforcement check shape. */
  checks: PolicyValidationCheck[];
  /**
   * The parsed spec that was evaluated (absent when parse failed) — build the runtime
   * FROM this exact artifact so what runs is what was admitted (Python's report `spec`,
   * which additionally carries the provenance the `AdmissionContributor` stamps).
   */
  spec?: TypefluxYamlSpec;
}

export interface AdmitSpecOptions {
  project: TypefluxProjectSpec;
  /** The loaded policy specs the composition needs (Python's on-disk policy files). */
  policies: ProjectPolicySources;
  environmentId: string;
  origin: SpecOrigin;
  /** The target validation slot whose bound policies govern the submission (D298-2). */
  workflowId?: string;
  /** An explicit policy-id override, replacing the target-derived selection. */
  explicitPolicyIds?: readonly string[];
  /**
   * Environment-overlay + interpolation inputs applied to the submitted spec at parse
   * (mirrors Python's environment overlay). Omit for a self-contained submission.
   */
  loadOptions?: LoadYamlSpecOptions;
  /**
   * Resolves a referenced sub-workflow's spec for closure admission (#55 §9). Omit
   * for a non-composed submission or when child resolution is unavailable.
   */
  resolveSubworkflowSpec?: SubworkflowClosureSpecResolver;
}

const passed = (code: string, details?: Record<string, unknown>): PolicyValidationCheck => ({
  code,
  status: "passed",
  ...(details !== undefined ? { details } : {}),
});
const failed = (code: string, message: string): PolicyValidationCheck => ({ code, status: "failed", message });
const skipped = (code: string, message: string): PolicyValidationCheck => ({ code, status: "skipped", message });

/**
 * Decide whether `specText` may be admitted under `project` in `options.environmentId`.
 * Never throws for a governance violation — every outcome is a check in the returned
 * report (only a caller error, e.g. an unknown environment in `loadOptions`, propagates).
 */
export function admitSpec(specText: string, options: AdmitSpecOptions): AdmissionReport {
  const { project, policies, environmentId, origin, workflowId, explicitPolicyIds } = options;
  const checks: PolicyValidationCheck[] = [];

  // An EXPLICIT slot must exist in the manifest — proceeding without it would skip
  // that slot's configuration exactly like the manifest path never would. Only an
  // INFERRED slot (the spec's own name) may be absent: the new-workflow submission case.
  if (workflowId !== undefined && !project.workflows.some((workflow) => workflow.id === workflowId)) {
    checks.push(
      failed(
        "admission_unknown_workflow",
        `workflow '${workflowId}' is not declared in the project manifest; declared: ${project.workflows
          .map((workflow) => workflow.id)
          .sort()
          .join(", ")}`,
      ),
    );
    return { admitted: false, specOrigin: origin, environmentId, workflowId, checks };
  }

  // ── parse (bounded loader; env overlay when supplied) ──────────────────────
  let spec: TypefluxYamlSpec;
  try {
    spec = loadYamlSpec(specText, options.loadOptions ?? {});
  } catch (error) {
    checks.push(failed("admission_spec_shape", `spec could not be parsed for admission: ${errorMessage(error)}`));
    return { admitted: false, specOrigin: origin, environmentId, ...(workflowId !== undefined ? { workflowId } : {}), checks };
  }
  checks.push(passed("admission_spec_shape"));

  // ── external-origin structural gate (structurally satisfied in the TS injection model) ──
  if (origin === "external") {
    checks.push(
      passed(
        "admission_external_modules_forbidden",
        {
          note:
            "the TS SDK injects providers/registries/observers/moderators and rejects class:/modules: at spec load (#496), so an external submission carries no module-import surface",
        },
      ),
    );
  }

  // ── composed policy binding (design D298-2) ────────────────────────────────
  // `||` (not `??`) mirrors Python `workflow_id or spec.name` — an empty-string slot
  // falls back to the spec name (the truthiness-porting trap), so selection + the
  // closure root agree across editions.
  const targetSlot = workflowId || spec.name;
  const selectOptions: SelectProjectPolicyIdsOptions = {
    environmentId,
    workflowId: targetSlot,
    ...(explicitPolicyIds !== undefined ? { explicitPolicyIds } : {}),
  };
  const selected = selectProjectPolicyIdsForWorkflow(project, selectOptions);
  let policyHash: string | undefined;
  if (selected.length === 0) {
    if (origin === "external") {
      // Fail-closed: an ungoverned external spec must not be admitted.
      checks.push(
        failed(
          "admission_policy_selection",
          "external-origin admission requires a governing policy; bind one via a validation.targets entry for the workflow slot, or pass an explicit policy id",
        ),
      );
    } else {
      checks.push(skipped("admission_policy_selection", "no project policies selected for this workflow/environment"));
    }
  } else {
    try {
      const policy = composeProjectPolicyIds(project, policies, selected);
      policyHash = policy.policyHash;
      checks.push(
        passed("admission_policy_selection", {
          selected_policy_ids: policy.selectedPolicyIds,
          applied_policy_ids: policy.appliedPolicyIds,
          policy_hash: policy.policyHash,
        }),
      );
      checks.push(...validatePolicyCompliance(spec, policy));
      // #788: an elevated declared tier (closure-aware) must actually be enforced by
      // the composed policy — admission shares the binding verdict with validation
      // and the runtime guard (Python parity).
      checks.push(
        riskTierBindingVerdict(spec, selected, policy.payload, options.resolveSubworkflowSpec, targetSlot),
      );
      const closure = validateSubworkflowClosurePolicy(spec, policy, options.resolveSubworkflowSpec, targetSlot);
      if (closure !== undefined) checks.push(closure);
    } catch (error) {
      checks.push(failed("policy_composition", errorMessage(error)));
    }
  }

  const admitted = !checks.some((check) => check.status === "failed");
  return {
    admitted,
    specOrigin: origin,
    environmentId,
    workflowId: targetSlot,
    ...(policyHash !== undefined ? { policyHash } : {}),
    checks,
    spec,
  };
}

const errorMessage = (error: unknown): string => (error instanceof Error ? error.message : String(error));
