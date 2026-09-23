/**
 * Deployment PLANS — the in-repo, immutable, content-hashed YAML approval gate (deployment
 * tier, #687 slice 1; Python `project/deployments.py`). A plan pins the identity a promotion
 * emits artifacts for: the workflow's constant type + spec digest, target environment, the
 * composed policy hash, the digest-pinned image, and the structural preflight result. It
 * carries NO secret values, prompt text, or rendered config — identities and hashes only.
 *
 * EDITION-HONEST IDENTITY (D687-1): `workflow_type` is the constant generic type
 * ({@link YAML_WORKFLOW_TYPE}) — the ts-plan-argument architecture has no version-suffixed
 * type — so drift detection rides `spec_digest` ({@link workflowPlanDigest}) + the policy
 * hash, exactly like the drain/frozen-version machinery. `verify_deployment_plan`'s twin
 * therefore diffs `spec_digest`, `policy_hash`, and the structural preflight, and asserts
 * `workflow_type` CONSTANT (never version-suffixed). Cross-edition plan hashes differ by
 * construction (Python version-suffixes `workflow_type` and digests the Python plan tree);
 * the TS hash is deterministic within the edition (pin `now` for a fixed hash).
 *
 * Approval is the GitHub PR review that merges the file to main; git is the audit trail.
 * Promotion (the `typeflux-project deploy --apply <file>` verb) verifies the plan against the
 * currently-resolved bundle and fails closed on drift.
 */

import { execFileSync } from "node:child_process";
import { lstatSync, realpathSync } from "node:fs";
import { basename as pathBasename, dirname as pathDirname, join as pathJoin, relative as pathRelative, resolve as pathResolve } from "node:path";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { canonicalJson } from "@typeflux/temporal";
import { parseDocument, stringify as stringifyYaml } from "yaml";
import { z } from "zod";

import {
  assertClosureObservabilityConsistent,
  isPlaceholderImageDigest,
  PLACEHOLDER_IMAGE_DIGEST,
  ProjectDeploymentError,
  validateDeploymentImage,
} from "./deployment.js";
import { workflowPlanDigest } from "./frozen-version.js";
import { ProjectPolicyError } from "./policy-composition.js";
import { composeProjectPolicyIds } from "./project-resolve.js";
import {
  buildProjectPolicyRuntimeGuard,
  selectProjectPolicyIdsForWorkflow,
  type SubworkflowClosureSpecResolver,
} from "./project-enforcement.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import { validateProjectBundle, withEnvironmentContext, type ProjectBundleSources } from "./project-validation.js";
import { YAML_WORKFLOW_TYPE } from "./runtime.js";
import type { TypefluxYamlSpec } from "./spec.js";
import { workflowPlanFromSpec, type SubworkflowSpecResolver } from "./build-workflow.js";

export const PLAN_VERSION = "1" as const;
export const PLAN_DIR_NAME = "deployments";
/** The spec-digest algorithm the TS runtime stamps (see `workflowPlanDigest`); Python `SPEC_DIGEST_ALGORITHM`. */
const SPEC_DIGEST_ALGORITHM = "typeflux-yaml-plan-v1";
/** Bound plan-file reads like the spec loader (operator-trusted, but still amplification-guarded). */
const MAX_PLAN_BYTES = 1024 * 1024;

/** Git provenance of the resolving checkout (Python `PlanIdentityCode`). Absent in the TS tier today. */
export interface PlanIdentityCode {
  sha: string;
  branch?: string | null;
  repo_url?: string | null;
}

/** The frozen identity a plan pins (Python `PlanIdentity`). */
export interface PlanIdentity {
  workflow_id: string;
  workflow_name: string;
  workflow_type: string;
  spec_digest: string;
  spec_digest_algorithm: string;
  environment_id: string;
  /** Optional-or-null like the contract schema; the TS writer always writes `null`. */
  code?: PlanIdentityCode | null;
}

/** The composed policy a plan pins (Python `PlanPolicy`). */
export interface PlanPolicy {
  selected_policy_ids: string[];
  applied_policy_ids: string[];
  policy_hash: string;
}

/** The structural preflight snapshot a plan records (Python `PlanPreflight`). */
export interface PlanPreflight {
  ok: boolean;
  issue_codes: string[];
}

/** The image + preflight a plan pins (Python `PlanDeployment`). */
export interface PlanDeployment {
  image: string;
  image_digest_pinned: boolean;
  preflight: PlanPreflight;
}

/** An immutable, content-hashed deployment plan (Python `DeploymentPlan`). */
export interface DeploymentPlan {
  plan_version: typeof PLAN_VERSION;
  plan_hash: string;
  generated_at: string;
  identity: PlanIdentity;
  policy: PlanPolicy;
  deployment: PlanDeployment;
}

/** One drifted field between a plan and the current resolution (Python `PlanMismatch`). */
export interface PlanMismatch {
  path: string;
  plan_value: unknown;
  current_value: unknown;
}

/** The result of verifying a plan against the current resolution (Python `PlanVerification`). */
export interface PlanVerification {
  ok: boolean;
  mismatches: PlanMismatch[];
}

/** Resolves a workflow's spec under an environment (+ its sub-workflow resolver for the plan digest). */
export type DeploymentPlanResolver = (
  workflowId: string,
  environmentId: string,
) => { spec: TypefluxYamlSpec; subworkflows?: SubworkflowSpecResolver } | undefined;

/** The filename-friendly identifier `workflow.env.<hash12>` (Python `DeploymentPlan.plan_id`). */
export function deploymentPlanId(plan: DeploymentPlan): string {
  return `${plan.identity.workflow_id}.${plan.identity.environment_id}.${plan.plan_hash.slice(0, 12)}`;
}

/** Serialize a plan to its on-disk YAML form (Python `DeploymentPlan.to_yaml`). */
export function deploymentPlanToYaml(plan: DeploymentPlan): string {
  return stringifyYaml(plan, { sortMapEntries: false });
}

/** Canonical-JSON sha256 with `plan_hash` AND `generated_at` zeroed — same algorithm as policy
 * hashing (Python `_compute_plan_hash`). `generated_at` is run-provenance: hashing it would give
 * the identical composition a different hash/filename per run (#798). */
function computePlanHash(payload: DeploymentPlan): string {
  const zeroed = { ...payload, plan_hash: "", generated_at: "" };
  return createHash("sha256").update(canonicalJson(zeroed), "utf-8").digest("hex");
}

/**
 * The manifest id charset (`_ID_PATTERN` in both editions' project specs): alnum start, then
 * alnum / `_` / `.` / `-`. Plan identity ids are constrained to it at LOAD time so a tampered
 * plan cannot smuggle shell metacharacters into `plan_id`-derived surfaces (filenames, the
 * console's `promote_command`) — the whole charset is shell-safe by construction.
 */
const SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

const planIdentityCodeSchema = z
  .object({
    sha: z.string(),
    branch: z.string().nullable().optional(),
    repo_url: z.string().nullable().optional(),
  })
  .strict();

// The contract's plan schemas, field-for-field and extra-forbid (Python pydantic
// `extra="forbid"`): unknown keys, missing fields, and wrong types are all rejected at load.
const planIdentitySchema = z
  .object({
    workflow_id: z.string().regex(SAFE_ID_PATTERN, "workflow_id must match the manifest id charset"),
    workflow_name: z.string(),
    workflow_type: z.string(),
    spec_digest: z.string(),
    spec_digest_algorithm: z.string(),
    environment_id: z.string().regex(SAFE_ID_PATTERN, "environment_id must match the manifest id charset"),
    code: planIdentityCodeSchema.nullable().optional(),
  })
  .strict();

const planPolicySchema = z
  .object({
    selected_policy_ids: z.array(z.string()),
    applied_policy_ids: z.array(z.string()),
    policy_hash: z.string(),
  })
  .strict();

const planPreflightSchema = z.object({ ok: z.boolean(), issue_codes: z.array(z.string()) }).strict();

const planDeploymentSchema = z
  .object({
    image: z.string(),
    image_digest_pinned: z.boolean(),
    preflight: planPreflightSchema,
  })
  .strict();

const deploymentPlanSchema = z
  .object({
    plan_version: z.literal(PLAN_VERSION),
    plan_hash: z.string().regex(/^[0-9a-f]{64}$/, "plan_hash must be a sha256 hex digest"),
    generated_at: z.string(),
    identity: planIdentitySchema,
    policy: planPolicySchema,
    deployment: planDeploymentSchema,
  })
  .strict();

/** The digest of the currently-resolved plan for a workflow (Python `bundle.workflow.spec_digest`). */
function currentSpecDigest(resolved: { spec: TypefluxYamlSpec; subworkflows?: SubworkflowSpecResolver }): string {
  const plan = workflowPlanFromSpec(resolved.spec, resolved.subworkflows !== undefined ? { subworkflows: resolved.subworkflows } : {});
  return workflowPlanDigest(plan);
}

/** The composed policy hash currently selected for a workflow, or "" when none is selected. */
function currentPolicyHash(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  workflowId: string,
  environmentId: string,
  selectedPolicyIds: readonly string[],
): string {
  const selected = selectProjectPolicyIdsForWorkflow(project, { environmentId, workflowId, explicitPolicyIds: selectedPolicyIds });
  if (selected.length === 0) return "";
  try {
    return composeProjectPolicyIds(project, sources, selected).policyHash;
  } catch (error) {
    // Only a policy-composition failure (unknown id, broken extends closure, merge conflict)
    // degrades to an empty hash — drift then names policy.policy_hash. Anything else is a
    // programming error and must surface, not masquerade as policy drift.
    if (error instanceof ProjectPolicyError) return "";
    throw error;
  }
}

/** Sorted-unique structural preflight issue codes (Python `PlanPreflight.issue_codes`). */
function preflightFor(project: TypefluxProjectSpec, sources: ProjectBundleSources): PlanPreflight {
  const report = validateProjectBundle(project, sources);
  const codes = [...new Set(report.issues.map((issue) => issue.code))].sort();
  return { ok: report.ok, issue_codes: codes };
}

export interface WriteDeploymentPlanOptions {
  workflowId: string;
  environmentId: string;
  image: string;
  policyIds?: readonly string[];
  allowMutableImage?: boolean;
  /** Accept the all-zeros placeholder image digest at plan-write (Python `allow_placeholder_image`; #757 item 5). */
  allowPlaceholderImage?: boolean;
  /** The absolute directory to write the plan under (Python `<manifest_dir>/deployments` or `out_dir`). */
  planDir: string;
  /** Fixed timestamp for deterministic hashing/tests (Python `now`); defaults to `new Date()`. */
  now?: Date;
}

/**
 * Resolve the bundle, ADMIT the composition, and write the immutable file (Python
 * `write_deployment_plan`, whose `resolve_workflow_bundle` enforces admission). A plan file is
 * an APPROVAL artifact: writing one runs the same compliance + transitive-closure admission the
 * deploy/operate gates run — under the target environment's variable overlay — and fails closed
 * (`ProjectPolicyEnforcementError`) for a non-compliant workflow, so a direct caller of this
 * export cannot mint an approvable file that deployment generation would reject. Refuses to
 * overwrite an existing plan file whose content hash differs (plans are immutable — a new
 * composition is a new file). Returns the written path and the composed plan.
 */
export function writeDeploymentPlan(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  resolve: DeploymentPlanResolver,
  options: WriteDeploymentPlanOptions,
): { path: string; plan: DeploymentPlan } {
  const { workflowId, environmentId } = options;
  // Reuse the same digest-pin + placeholder-digest enforcement as `deploy`; same posture. A plan
  // pinning the all-zeros placeholder is refused at WRITE time (#757 item 5) unless opted in, so an
  // approval file can never carry an image that only fails at pod scheduling.
  const imageDigestPinned = validateDeploymentImage(options.image, {
    allowMutableImage: options.allowMutableImage === true,
    allowPlaceholderImage: options.allowPlaceholderImage === true,
  });

  const resolved = resolve(workflowId, environmentId);
  if (resolved === undefined) {
    throw new ProjectDeploymentError(
      `cannot resolve workflow '${workflowId}' under environment '${environmentId}'; refusing to write a deployment plan`,
    );
  }
  const selectedPolicyIds = selectProjectPolicyIdsForWorkflow(project, {
    environmentId,
    workflowId,
    explicitPolicyIds: options.policyIds ?? [],
  });
  if (selectedPolicyIds.length === 0) {
    throw new ProjectDeploymentError(
      `workflow '${workflowId}' resolves under no policy in environment '${environmentId}'; ` +
        "refusing to write a deployment plan without a policy",
    );
  }
  // ADMISSION (review item 3): the same guard path the deploy/operate gates use — compliance +
  // sub-workflow closure, fail-closed — evaluated under the target ENVIRONMENT's variable
  // overlay (review item 4: env-backed policy values must resolve against the deployment
  // environment, not the operator's shell). The resolver doubles as the closure resolver.
  const closureResolver: SubworkflowClosureSpecResolver = (siblingId) =>
    siblingId === workflowId ? resolved.spec : resolve(siblingId, environmentId)?.spec;
  const admit = () =>
    buildProjectPolicyRuntimeGuard(project, sources, {
      spec: resolved.spec,
      workflowId,
      environmentId,
      explicitPolicyIds: options.policyIds ?? [],
      resolveSubworkflowSpec: closureResolver,
    });
  const environment = Object.hasOwn(sources.environments, environmentId) ? sources.environments[environmentId] : undefined;
  const guard = environment !== undefined ? withEnvironmentContext(environment, undefined, admit) : admit();
  // `selectedPolicyIds` is non-empty, so the guard is always defined here.
  const composed = guard!.policy;
  // #757 review: the plan file is the APPROVAL artifact, so the #756 closure
  // observability-consistency gate runs at write time too — a parent-langfuse/child-langsmith
  // composition must fail authoring (ObservabilityCompositionError), never mint an approvable
  // file that crash-loops at pod boot. Direct API callers (not just the CLI's build-first path)
  // hit this gate.
  assertClosureObservabilityConsistent(resolved.spec, workflowId, closureResolver);
  const preflight = preflightFor(project, sources);
  if (!preflight.ok) {
    throw new ProjectDeploymentError(
      "project fails structural preflight; refusing to write a plan " +
        `(issue codes: ${preflight.issue_codes.join(", ") || "unknown"})`,
    );
  }

  const identity: PlanIdentity = {
    workflow_id: workflowId,
    workflow_name: resolved.spec.workflow.name,
    workflow_type: YAML_WORKFLOW_TYPE,
    spec_digest: currentSpecDigest(resolved),
    spec_digest_algorithm: SPEC_DIGEST_ALGORITHM,
    environment_id: environmentId,
    // Git provenance is not modeled in the TS tier yet; the contract permits null.
    code: null,
  };
  const policy: PlanPolicy = {
    selected_policy_ids: composed.selectedPolicyIds,
    applied_policy_ids: composed.appliedPolicyIds,
    policy_hash: composed.policyHash,
  };
  const deployment: PlanDeployment = { image: options.image, image_digest_pinned: imageDigestPinned, preflight };

  const generatedAt = (options.now ?? new Date()).toISOString();
  const skeleton: DeploymentPlan = {
    plan_version: PLAN_VERSION,
    plan_hash: "",
    generated_at: generatedAt,
    identity,
    policy,
    deployment,
  };
  const plan: DeploymentPlan = { ...skeleton, plan_hash: computePlanHash(skeleton) };

  mkdirSync(options.planDir, { recursive: true });
  const path = join(options.planDir, `${deploymentPlanId(plan)}.yaml`);
  if (existsSync(path)) {
    const existing = loadDeploymentPlan(path);
    if (existing.plan_hash !== plan.plan_hash) {
      throw new ProjectDeploymentError(
        `deployment plan ${path} already exists with a different content hash ` +
          `(${existing.plan_hash.slice(0, 12)} vs ${plan.plan_hash.slice(0, 12)}); plans are immutable — ` +
          "a new composition is a new file",
      );
    }
    // Same content: re-writing is a harmless no-op.
  }
  writeFileSync(path, deploymentPlanToYaml(plan), "utf-8");
  return { path, plan };
}

/**
 * Strict-parse a plan file's YAML into a {@link DeploymentPlan} (Python `load_deployment_plan`).
 * Validation is contract-strict (every field, unknown keys rejected, identity ids constrained to
 * the shell-safe manifest charset), and the content hash is RECOMPUTED and compared to the stored
 * `plan_hash` — a post-approval edit (image, policy ids, digest) with the stale hash left in
 * place is rejected as tampering, so `--apply` promotion can never consume tampered values.
 */
export function loadDeploymentPlan(path: string): DeploymentPlan {
  const text = readFileSync(path, "utf-8");
  if (Buffer.byteLength(text, "utf-8") > MAX_PLAN_BYTES) {
    throw new ProjectDeploymentError(`deployment plan ${path} exceeds the ${MAX_PLAN_BYTES} byte limit`);
  }
  const doc = parseDocument(text, { uniqueKeys: true });
  const fatal = [...doc.errors, ...doc.warnings.filter((warning) => warning.code === "DUPLICATE_KEY")];
  if (fatal.length > 0) {
    throw new ProjectDeploymentError(`deployment plan ${path} failed to parse: ${fatal[0]?.message}`);
  }
  const raw: unknown = doc.toJS();
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new ProjectDeploymentError(`deployment plan ${path} is not a YAML mapping`);
  }
  const parsed = deploymentPlanSchema.safeParse(raw);
  if (!parsed.success) {
    throw new ProjectDeploymentError(`deployment plan ${path} failed validation:\n${z.prettifyError(parsed.error)}`);
  }
  const plan = parsed.data as DeploymentPlan;
  // INTEGRITY (review item 1): recompute the canonical hash over the loaded content (hash field
  // zeroed). A mismatch means the file was edited after it was written — plans are immutable.
  const recomputed = computePlanHash(plan);
  if (recomputed !== plan.plan_hash) {
    throw new ProjectDeploymentError(
      `deployment plan ${path} failed its integrity check: plan_hash ${plan.plan_hash.slice(0, 12)} does not ` +
        `match the file content (recomputed ${recomputed.slice(0, 12)}); the file was modified after it was ` +
        "written — plans are immutable, regenerate a new plan instead of editing one",
    );
  }
  return plan;
}

/** One file under a `deployments/` dir: a loaded plan, or the load error for a malformed file. */
export interface DeploymentPlanDirEntry {
  file: string;
  plan?: DeploymentPlan;
  error?: string;
}

/**
 * Read every `*.yaml` under a plan dir, keeping malformed files VISIBLE as error entries
 * (filename + load error) rather than silently dropping them — the control plane surfaces
 * each so an operator sees a corrupt/tampered plan file instead of a shorter listing.
 */
export function readDeploymentPlanDir(planDir: string): DeploymentPlanDirEntry[] {
  if (!existsSync(planDir)) return [];
  const entries: DeploymentPlanDirEntry[] = [];
  for (const file of readdirSync(planDir).filter((name) => name.endsWith(".yaml")).sort()) {
    try {
      entries.push({ file, plan: loadDeploymentPlan(join(planDir, file)) });
    } catch (error) {
      entries.push({ file, error: error instanceof Error ? error.message : String(error) });
    }
  }
  return entries;
}

/** Every VALID plan under a project's `deployments/` dir (Python `list_deployment_plans`). Malformed
 * files are omitted here; use {@link readDeploymentPlanDir} to surface them as error entries. */
export function listDeploymentPlans(planDir: string): DeploymentPlan[] {
  return readDeploymentPlanDir(planDir)
    .map((entry) => entry.plan)
    .filter((plan): plan is DeploymentPlan => plan !== undefined);
}

/**
 * The SYNTHETIC mismatch path the promote gate emits for a placeholder-digest plan (#757 item 5).
 * Not a literal field diff: like the control plane's `resolution`/`parse` synthetic entries,
 * `plan_value` carries the literal offending image and `current_value` the human explanation.
 * Renderers that special-case explanations (the CLI drift printer) key off this code.
 */
export const PLACEHOLDER_IMAGE_MISMATCH_PATH = "deployment.image_placeholder" as const;

export interface VerifyDeploymentPlanOptions {
  /** Accept the all-zeros placeholder image digest at promote/verify time (Python `allow_placeholder_image`; #757 item 5). */
  allowPlaceholderImage?: boolean;
}

/**
 * Compare a plan against the currently-resolved bundle and fail closed on drift (Python
 * `verify_deployment_plan`). Diffs `spec_digest` ({@link workflowPlanDigest}), `policy_hash`,
 * and the structural preflight; asserts `workflow_type` CONSTANT. Never a partial pass — any
 * named mismatch flips `ok` to false.
 *
 * PROMOTE GATE (#757 item 5): a plan pinning the well-known all-zeros placeholder digest is
 * refused here too, so a placeholder plan that slipped past write-time (e.g. authored before the
 * guard, or with the digest zeroed by a bad edit) still cannot promote — it would only fail at pod
 * scheduling (ImagePullBackOff). The refusal carries its OWN synthetic check identity
 * (`deployment.image_placeholder`, following the `resolution`/`parse` synthetic-path precedent):
 * `plan_value` is the literal offending image and `current_value` the explanation — never a prose
 * blob in a literal diff slot, so generic diff renderers stay coherent. Pass
 * `allowPlaceholderImage` to promote a placeholder plan intentionally.
 */
export function verifyDeploymentPlan(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  resolve: DeploymentPlanResolver,
  plan: DeploymentPlan,
  options: VerifyDeploymentPlanOptions = {},
): PlanVerification {
  const resolved = resolve(plan.identity.workflow_id, plan.identity.environment_id);
  if (resolved === undefined) {
    throw new ProjectDeploymentError(
      `cannot resolve workflow '${plan.identity.workflow_id}' under environment '${plan.identity.environment_id}' to verify its plan`,
    );
  }
  const mismatches: PlanMismatch[] = [];
  if (options.allowPlaceholderImage !== true && isPlaceholderImageDigest(plan.deployment.image)) {
    // A distinct synthetic check code (like the CP's `resolution`/`parse` entries): the literal
    // offending value rides `plan_value`, the explanation rides `current_value` — the sibling
    // literal-diff paths (spec_digest, workflow_type, policy_hash) keep same-typed values.
    mismatches.push({
      path: PLACEHOLDER_IMAGE_MISMATCH_PATH,
      plan_value: plan.deployment.image,
      current_value:
        `the all-zeros placeholder digest (${PLACEHOLDER_IMAGE_DIGEST}) resolves to no published image ` +
        "and fails at pod scheduling (ImagePullBackOff); regenerate the plan with a real published " +
        "digest, or pass --allow-placeholder-image (allowPlaceholderImage) to promote it intentionally",
    });
  }
  const digest = currentSpecDigest(resolved);
  if (digest !== plan.identity.spec_digest) {
    mismatches.push({ path: "identity.spec_digest", plan_value: plan.identity.spec_digest, current_value: digest });
  }
  // The ts-plan-argument type is constant (D687-1): a plan whose type is not the generic
  // type is drift (e.g. a hand-edited/foreign plan), named exactly like Python's diff.
  if (YAML_WORKFLOW_TYPE !== plan.identity.workflow_type) {
    mismatches.push({ path: "identity.workflow_type", plan_value: plan.identity.workflow_type, current_value: YAML_WORKFLOW_TYPE });
  }
  const policyHash = currentPolicyHash(project, sources, plan.identity.workflow_id, plan.identity.environment_id, plan.policy.selected_policy_ids);
  if (policyHash !== plan.policy.policy_hash) {
    mismatches.push({ path: "policy.policy_hash", plan_value: plan.policy.policy_hash, current_value: policyHash });
  }
  const structural = validateProjectBundle(project, sources);
  if (!structural.ok) {
    mismatches.push({ path: "preflight.ok", plan_value: true, current_value: false });
  }
  return { ok: mismatches.length === 0, mismatches };
}

/** The #790 merged-plan gate: `undefined` when the plan file's exact bytes exist at its
 * path on the remote default branch, else a human-readable refusal (Python
 * `verify_plan_merged_to_default_branch`). Git-native and hermetic — plans are immutable
 * and content-hashed, so byte-identity on `origin/<default>` is equivalent to "this
 * exact reviewed artifact was merged". */
export function verifyPlanMergedToDefaultBranch(planPath: string, projectRoot: string): string | undefined {
  // realpath (not resolve): git prints canonical paths, and e.g. macOS tmpdirs are
  // symlinks (/var -> /private/var) — a non-canonical path makes relative() escape.
  // Anchor EVERYTHING to the project manifest's checkout: a plan path (absolute, or
  // with a symlinked directory component) must never redirect git into another repo.
  const anchoredRoot = realpathSync(pathResolve(projectRoot));
  const supplied = pathResolve(planPath);
  let absolute: string;
  try {
    if (lstatSync(supplied).isSymbolicLink()) {
      // A symlinked plan would redirect verification to WHATEVER repo the target
      // lives in — verify only real files at their supplied path.
      return (
        `--require-merged-plan: plan ${supplied} is a symlink; promote the real plan ` +
        "file so the gate verifies this repository's merged artifact."
      );
    }
    // Canonicalize the PARENT only (macOS tmpdirs are symlinks: /var -> /private/var);
    // the file itself was just proven to be a real file.
    absolute = pathJoin(realpathSync(pathDirname(supplied)), pathBasename(supplied));
  } catch {
    return `--require-merged-plan: plan ${supplied} does not exist locally.`;
  }
  const git = (args: string[], cwd: string): { code: number; out: Buffer } => {
    try {
      return { code: 0, out: execFileSync("git", ["-C", cwd, ...args], { stdio: ["ignore", "pipe", "pipe"] }) };
    } catch (error) {
      return { code: 1, out: Buffer.from(String((error as { stdout?: Buffer }).stdout ?? "")) };
    }
  };
  const top = git(["rev-parse", "--show-toplevel"], anchoredRoot);
  if (top.code !== 0 || top.out.toString().trim() === "") {
    return (
      `--require-merged-plan: the project manifest at ${anchoredRoot} is not inside a git ` +
      "checkout, so the merged-plan gate cannot verify plans. Promote from the repository " +
      "clone (CI), or drop the flag for non-repo promote flows."
    );
  }
  const topLevel = realpathSync(top.out.toString().trim());
  const relToRepo = pathRelative(topLevel, absolute);
  if (relToRepo.startsWith("..") || relToRepo === "") {
    return (
      `--require-merged-plan: plan ${absolute} resolves outside the project's git checkout ` +
      `(${topLevel}) — the gate verifies only this repository's merged artifacts.`
    );
  }
  // Reject symlinked DIRECTORY components inside the checkout: a local alias
  // (deployments -> approved) would otherwise verify a different merged path than the
  // one the operator supplied. Walk the supplied (uncanonicalized) ancestors up to the
  // repo root; the root's canonical prefix (macOS /var -> /private/var) sits above the
  // repo and is never visited.
  let probe = pathDirname(supplied);
  const rejectComponent = (component: string): string =>
    `--require-merged-plan: plan path component ${component} is a symlink — supply the ` +
    "plan's real repository path so the gate verifies the merged artifact at that " +
    "exact path.";
  for (;;) {
    let probeReal: string;
    try {
      probeReal = realpathSync(probe);
    } catch {
      break;
    }
    if (probeReal === topLevel) {
      // `ln -s . alias` (an IN-repo symlink back to the root) must still be rejected;
      // a checkout root reached through an OUTSIDE symlinked spelling (macOS /var) is fine.
      let parentReal = "";
      try {
        parentReal = realpathSync(pathDirname(probe));
      } catch {
        break;
      }
      if (
        lstatSync(probe).isSymbolicLink() &&
        (parentReal === topLevel || !pathRelative(topLevel, parentReal).startsWith(".."))
      ) {
        return rejectComponent(probe);
      }
      break;
    }
    if (pathDirname(probe) === probe) break;
    if (lstatSync(probe).isSymbolicLink()) {
      return rejectComponent(probe);
    }
    probe = pathDirname(probe);
  }

  const head = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], topLevel);
  if (head.code !== 0 || head.out.toString().trim() === "") {
    return (
      "--require-merged-plan: the remote default branch is unknown (refs/remotes/origin/HEAD " +
      "is unset). Run `git remote set-head origin --auto` and retry."
    );
  }
  const defaultRef = head.out.toString().trim();
  const rel = pathRelative(topLevel, absolute).split("\\").join("/");
  const merged = git(["ls-tree", defaultRef, "--", rel], topLevel);
  if (merged.code !== 0 || merged.out.toString().trim() === "") {
    return (
      `--require-merged-plan: plan ${rel} does not exist on the default branch (${defaultRef}) — ` +
      "it has not been merged. Open a PR with the plan file, merge it, then promote."
    );
  }
  // <mode> <type> <oid>\t<path>: the merged entry must be a REGULAR file blob — a
  // merged symlink whose target text matches would otherwise satisfy the OID compare.
  const entryFields = merged.out.toString().trim().split(/\s+/);
  if (entryFields.length < 3 || !(entryFields[0] as string).startsWith("100") || entryFields[1] !== "blob") {
    return (
      `--require-merged-plan: plan ${rel} on ${defaultRef} is not a regular file — merge ` +
      "the plan as a plain file, not a symlink."
    );
  }
  const mergedOid = entryFields[2] as string;
  // A content FILTER on the plan path (LFS, custom filter.*.clean) would let a locally
  // edited file hash to the merged blob while different bytes get promoted — refuse
  // filtered plan paths outright. eol/autocrlf ride the `text` attribute and stay fine.
  const filterAttr = git(["check-attr", "filter", "--", rel], topLevel);
  const filterLine = filterAttr.out.toString().trim();
  if (filterAttr.code === 0 && filterLine !== "" && !filterLine.endsWith(": unspecified") && !filterLine.endsWith(": unset")) {
    return (
      `--require-merged-plan: plan ${rel} carries a git content filter ` +
      `(${filterLine.split(": ").pop() as string}) — the gate cannot prove the promoted bytes ` +
      "were merged. Store plans unfiltered."
    );
  }
  // Compare blob OIDs with the clean filter applied (--path) rather than raw bytes:
  // a checkout with eol/autocrlf line-ending conversion must not fail a clean,
  // merged plan.
  const local = git(["hash-object", "--path", rel, absolute], topLevel);
  if (local.code !== 0 || local.out.toString().trim() !== mergedOid) {
    return (
      `--require-merged-plan: plan ${rel} differs from the version merged on ${defaultRef} — ` +
      "the local file is not the reviewed artifact. Promote the merged plan bytes " +
      "(git checkout the file from the default branch)."
    );
  }
  return undefined;
}

