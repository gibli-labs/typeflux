/**
 * Deploy-time worker-plan builder + admission (deployment tier, #687 slice 1; Python
 * `project/deployment.py` `build_project_deployment_plan` + the classification helpers).
 *
 * SCOPE: this is the SECRET-FREE deployment PLAN — the per-workflow worker descriptor a
 * promotion emits artifacts for: resolved task queue, the ConfigMap-safe environment
 * classification, the typed Secret references, the rendered worker command + in-image
 * project path, and the admitted composed policy identity. The Kubernetes manifests /
 * Dockerfiles rendered FROM this plan live in `deployment-render.ts` (#687 slice 2). The
 * plan is the admission gate the CLI's `deploy` runs and the shape the control-plane
 * bundle's `deployment_preview` projects.
 *
 * ADMISSION (D687-3, "no forked admission"): policy is composed + admitted through the SAME
 * {@link buildProjectPolicyRuntimeGuard} path the operate gate (`enforcePolicySelection`)
 * uses — compliance + transitive sub-workflow closure, fail-closed. The env-var
 * classification fails closed on an unclassified variable, and the one-worker-per-task-queue
 * rule guards accidental co-tenancy (both with the same escape flags as Python).
 *
 * EDITION-HONEST DIVERGENCE: Python's deploy path additionally DOWNGRADES a
 * `policy_temporal` "API key required" failure when the spec carries a typed secret ref
 * (the deployment provides the secret). The TS tier deliberately keeps ONE admission path —
 * the runtime guard the operate gate already uses — rather than fork a deploy-only
 * adjustment; a policy that mandates a Temporal API key is admitted identically at operate
 * and deploy time.
 */

import { createHash } from "node:crypto";
import { basename, isAbsolute, posix, relative, sep } from "node:path";

import { stringifyEnvValue } from "./environment-overlay.js";
import { assertConsistentComposedObservability } from "./observability-composition.js";
import {
  buildProjectPolicyRuntimeGuard,
  selectProjectPolicyIdsForWorkflow,
  walkSubworkflowClosure,
  type SubworkflowClosureSpecResolver,
} from "./project-enforcement.js";
import type { ComposedProjectPolicy } from "./policy-composition.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import { validateProjectBundle, withEnvironmentContext, type ProjectBundleSources } from "./project-validation.js";
import type { SecretValueSpec, TypefluxYamlSpec } from "./spec.js";

/** Matched against the substring after the image reference's last `@` (linear-time on attacker input). */
const SHA256_DIGEST_PATTERN = /^sha256:[0-9a-fA-F]{64}$/;
/**
 * The well-known all-zeros digest (#757 item 5). It is FORMAT-valid (`sha256:` + 64 hex), so the
 * digest-pin check passes — but it resolves to no published image and dies at pod scheduling
 * (ImagePullBackOff), AFTER the plan/PR/promote approval chain has rubber-stamped it. The plan
 * writer and the promote-time verifier reject it fail-closed unless the placeholder is opted in.
 */
export const PLACEHOLDER_IMAGE_DIGEST = `sha256:${"0".repeat(64)}`;

/** Whether an image reference pins the all-zeros placeholder digest (#757 item 5). */
export function isPlaceholderImageDigest(image: string): boolean {
  const at = image.lastIndexOf("@");
  return at >= 0 && image.slice(at + 1).toLowerCase() === PLACEHOLDER_IMAGE_DIGEST;
}
/**
 * Secret indicators anchored to the END of the name so operational variables that merely
 * contain a secret-like token (e.g. `API_KEY_ROTATION_DAYS`, `TOKEN_BUCKET_SIZE`) are not
 * mis-routed to Secret references (Python `_SECRET_LIKE_ENV_PATTERN`).
 */
const SECRET_LIKE_ENV_PATTERN =
  /(?:^|_)(?:API_?KEY|PUBLIC_?KEY|PRIVATE_?KEY|CLIENT_?KEY|SECRET(?:_?KEY)?|TOKEN|PASSWORD|CERT|KEY)$/i;
/** The always-ConfigMap-safe environment names (Python `_SAFE_CONFIG_ENV_NAMES`). */
const SAFE_CONFIG_ENV_NAMES: ReadonlySet<string> = new Set([
  "LANGFUSE_BASE_URL",
  "LANGFUSE_HOST",
  "LANGFUSE_PROMPT_LABEL",
  "TEMPORAL_ADDRESS",
  "TEMPORAL_NAMESPACE",
  "TEMPORAL_TASK_QUEUE",
  "TEMPORAL_TLS",
  "TYPEFLUX_ANTHROPIC_MODEL",
  "TYPEFLUX_DEPLOYMENT_ID",
  "TYPEFLUX_ENVIRONMENT",
  "TYPEFLUX_OPENAI_MODEL",
  "TYPEFLUX_TEMPORAL_REGION",
]);
/** Per-provider ConfigMap model key so in-container YAML interpolation reproduces the resolution. */
const PROVIDER_MODEL_ENV_NAMES: Readonly<Record<string, string>> = {
  openai: "TYPEFLUX_OPENAI_MODEL",
  anthropic: "TYPEFLUX_ANTHROPIC_MODEL",
};

const RFC1123_CHARS_PATTERN = /[^a-z0-9-]+/g;
const RFC1123_DASH_PATTERN = /-+/g;
const SECRET_NAME_KEY_PATTERN = /[^A-Za-z0-9_.-]+/g;

/**
 * The preflight marker the generated worker command writes AFTER a successful preflight and
 * BEFORE it execs the poller (Python `_PREFLIGHT_MARKER_PATH`). Probes gate on it, so a
 * container is never reported healthy until its own preflight has passed. `/tmp` is the pod's
 * writable emptyDir under the read-only root filesystem.
 */
export const PREFLIGHT_MARKER_PATH = "/tmp/typeflux-preflight-ok";
/**
 * The stable TS worker entrypoint the rendered container command invokes — the edition's
 * layout-independent convention, paralleling Python's `python -m typeflux.project
 * run`. The reference `deploy/ts-yaml-worker` image family puts this bin on PATH.
 */
export const TS_WORKER_ENTRYPOINT = "typeflux-yaml-worker";
/** shlex-safe characters (Python `shlex.quote`): word chars plus `@%+=:,./-`; anything else is quoted. */
const SHLEX_SAFE_PATTERN = /^[\w@%+=:,./-]+$/;

/** POSIX-shell-quote a token exactly like Python `shlex.quote` (empty ⇒ `''`; safe ⇒ bare). */
function shQuote(value: string): string {
  if (value === "") return "''";
  if (SHLEX_SAFE_PATTERN.test(value)) return value;
  return "'" + value.replaceAll("'", `'"'"'`) + "'";
}

/** The deployment target — only Kubernetes today, kept as a field for cross-target parity. */
export type DeploymentTarget = "kubernetes";

/** Raised when a project deployment plan cannot be generated safely (Python `ProjectDeploymentError`). */
export class ProjectDeploymentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProjectDeploymentError";
  }
}

/** The admitted composed-policy identity a worker pins (Python `ProjectDeploymentPolicyIdentity`). */
export interface ProjectDeploymentPolicyIdentity {
  selected_policy_ids: string[];
  applied_policy_ids: string[];
  policy_names: string[];
  policy_hash: string;
}

/** A typed Secret env reference the worker mounts (Python `ProjectDeploymentSecretEnvRef`). */
export interface ProjectDeploymentSecretEnvRef {
  runtime_path: string;
  env_name: string;
  secret_name: string;
  secret_key: string;
  required: boolean;
  configured: boolean;
}

/** A typed Secret file reference the worker mounts (Python `ProjectDeploymentSecretFileRef`). */
export interface ProjectDeploymentSecretFileRef {
  runtime_path: string;
  mount_path: string;
  secret_name: string;
  secret_key: string;
  required: boolean;
  configured: boolean;
}

/** One resolved worker's secret-free deployment descriptor (Python `ProjectDeploymentWorkerPlan`). */
export interface ProjectDeploymentWorkerPlan {
  name: string;
  workflow_id: string;
  workflow_name: string;
  yaml_project: string;
  yaml_name: string;
  workflow_path: string;
  environment_id: string;
  environment_name: string;
  task_queue: string;
  /** Absolute path of the project manifest inside the worker image (Python `project_path_in_image`). */
  project_path_in_image: string;
  /** The rendered container command: `sh -c 'rm marker && <run> --preflight && touch marker && exec <run>'`. */
  command: string[];
  config_map_name: string;
  secret_name: string;
  config_map: Record<string, string>;
  secret_env: ProjectDeploymentSecretEnvRef[];
  secret_files: ProjectDeploymentSecretFileRef[];
  policy: ProjectDeploymentPolicyIdentity;
}

/** The project's secret-free deployment plan (Python `ProjectDeploymentPlan`). */
export interface ProjectDeploymentPlan {
  version: "1";
  target: DeploymentTarget;
  project_name: string;
  /** The resolved project manifest path (Python `project_manifest_path`; surfaced in K8s annotations). */
  project_manifest_path: string;
  /** Absolute path of the project manifest inside the worker image (Python `project_path_in_image`). */
  project_path_in_image: string;
  environment_id: string;
  environment_name: string;
  image: string;
  image_digest_pinned: boolean;
  workers: ProjectDeploymentWorkerPlan[];
}

/** A resolved workflow the deployment builder plans a worker for (the caller supplies resolution). */
export interface DeploymentResolvedWorkflow {
  /** The workflow's resolved spec under the target environment. */
  spec: TypefluxYamlSpec;
  /** The environment's display name (Python `resolved.environment.name`). */
  environmentName: string;
  /** The workflow file's manifest-relative (or absolute) path — surfaced for provenance. */
  workflowPath: string;
  /** The environment's variable map (raw literal values) — the classification input. */
  variables: Readonly<Record<string, string | number | boolean>>;
}

/** Resolves a project workflow's spec + environment metadata for the deployment builder. */
export type DeploymentWorkflowResolver = (workflowId: string) => DeploymentResolvedWorkflow | undefined;

export interface BuildProjectDeploymentPlanOptions {
  environmentId: string;
  /** The workflow ids to plan; empty ⇒ every declared project workflow (Python parity). */
  workflowIds?: readonly string[];
  policyIds?: readonly string[];
  image: string;
  allowMutableImage?: boolean;
  /** Accept the all-zeros placeholder image digest (Python `allow_placeholder_image`; #757 item 5). */
  allowPlaceholderImage?: boolean;
  allowSharedTaskQueue?: boolean;
  /** Env-var names to force ConfigMap-safe (Python `--config-env`). */
  configEnvNames?: readonly string[];
  /** The resolved project manifest path (Python `project.manifest_path`); seeds the default in-image path. */
  manifestPath?: string;
  /**
   * The manifest path to RECORD in `project_manifest_path` (annotations + deployment-plan.json;
   * #757 item 1). Defaults to {@link manifestPath}. The CLI passes a manifest-dir-relative value
   * so committed artifacts are machine-independent — this field is provenance only, never part of
   * the plan hash or drift detection, so a relative default is a safe behavior change.
   */
  projectManifestPath?: string;
  /** Absolute in-image manifest path override (Python `--project-path-in-image`); default derives from `manifestPath`. */
  projectPathInImage?: string;
  target?: DeploymentTarget;
}

/**
 * Validate a deployment image reference and report whether it is digest-pinned (Python
 * `_validate_image`). A non-pinned tag fails closed unless `allowMutableImage` is set; the
 * all-zeros placeholder digest ({@link PLACEHOLDER_IMAGE_DIGEST}) fails closed unless
 * `allowPlaceholderImage` is set (#757 item 5) — it is format-valid but resolves to no image.
 */
export function validateDeploymentImage(
  image: string,
  options: { allowMutableImage?: boolean; allowPlaceholderImage?: boolean } = {},
): boolean {
  if (!image || image.trim() !== image) {
    throw new ProjectDeploymentError("deployment image must be non-empty and trimmed");
  }
  const at = image.lastIndexOf("@");
  const name = at >= 0 ? image.slice(0, at) : "";
  const digest = at >= 0 ? image.slice(at + 1) : "";
  const digestPinned = at >= 0 && name !== "" && SHA256_DIGEST_PATTERN.test(digest);
  if (!digestPinned && options.allowMutableImage !== true) {
    throw new ProjectDeploymentError(
      "Kubernetes deployment images must be pinned by digest " +
        "(expected ...@sha256:<64 hex chars>); pass --allow-mutable-image for dev/local output",
    );
  }
  if (options.allowPlaceholderImage !== true && isPlaceholderImageDigest(image)) {
    throw new ProjectDeploymentError(
      `deployment image '${image}' pins the well-known all-zeros placeholder digest ` +
        `(${PLACEHOLDER_IMAGE_DIGEST}), which resolves to no published image and fails at pod ` +
        "scheduling (ImagePullBackOff) after the plan/PR/promote approval chain; regenerate with a " +
        "real published digest, or pass --allow-placeholder-image (allowPlaceholderImage) to write a " +
        "placeholder plan intentionally",
    );
  }
  return digestPinned;
}

/** Order-preserving dedup (Python `_dedupe`). */
const dedupe = (values: readonly string[]): string[] => [...new Set(values)];

function selectedWorkflowIds(project: TypefluxProjectSpec, workflowIds: readonly string[]): string[] {
  if (workflowIds.length > 0) return dedupe(workflowIds);
  return project.workflows.map((workflow) => workflow.id);
}

/** RFC-1123-safe resource name, hashed-suffixed when too long (Python `_resource_name`). */
function resourceName(projectName: string, environmentId: string, workflowId: string): string {
  const base = `typeflux-${projectName}-${environmentId}-${workflowId}`.toLowerCase();
  let name = base.replace(RFC1123_CHARS_PATTERN, "-").replace(RFC1123_DASH_PATTERN, "-");
  // Linear-time dash trim: the anchored-alternation regex form is quadratic
  // on long all-dash runs (js/polynomial-redos).
  let start = 0;
  let end = name.length;
  while (start < end && name[start] === "-") start += 1;
  while (end > start && name[end - 1] === "-") end -= 1;
  name = name.slice(start, end);
  if (name === "") return "typeflux-worker";
  if (name.length <= 63) return name;
  const digest = createHash("sha256").update(name, "utf-8").digest("hex").slice(0, 8);
  return `${name.slice(0, 54).replace(/-+$/g, "")}-${digest}`;
}

/**
 * The default in-image manifest path (Python `_default_project_path_in_image`): `/app/<manifest
 * relative to CWD>`, falling back to `/app/<basename>` when the manifest is not under CWD. With no
 * manifest path known (direct builder callers), the generic default filename is used.
 */
function defaultProjectPathInImage(manifestPath: string | undefined): string {
  if (manifestPath === undefined || manifestPath === "") return "/app/typeflux.project.yaml";
  let rel = relative(process.cwd(), manifestPath);
  if (rel === "" || rel.startsWith("..") || isAbsolute(rel)) rel = basename(manifestPath);
  return `/app/${rel.split(sep).join("/")}`;
}

/** Reject a non-absolute / untrimmed in-image project path (Python `_validate_project_path_in_image`). */
function validateProjectPathInImage(value: string): string {
  if (!value || value.trim() !== value || !value.startsWith("/")) {
    throw new ProjectDeploymentError("--project-path-in-image must be an absolute image path");
  }
  return value;
}

/**
 * The rendered container command (Python `_worker_command`): runs the TS worker entrypoint through
 * preflight, writes the marker, then execs the poller — all under one `sh -c` so probes gate on the
 * same preflight. The marker is removed first so a restarted container never inherits a stale one.
 */
function workerCommand(options: {
  projectPathInImage: string;
  workflowId: string;
  environmentId: string;
  policyIds: readonly string[];
  expectedPolicyHash: string;
}): string[] {
  const run = [
    TS_WORKER_ENTRYPOINT,
    options.projectPathInImage,
    "--workflow",
    options.workflowId,
    "--environment",
    options.environmentId,
  ];
  for (const policyId of options.policyIds) run.push("--policy", policyId);
  run.push("--expect-policy-hash", options.expectedPolicyHash);
  const quotedRun = run.map(shQuote).join(" ");
  return [
    "sh",
    "-c",
    `rm -f ${PREFLIGHT_MARKER_PATH}` +
      ` && ${quotedRun} --preflight` +
      ` && touch ${PREFLIGHT_MARKER_PATH}` +
      ` && exec ${quotedRun}`,
  ];
}

const isSecretValueSpec = (value: unknown): value is SecretValueSpec =>
  typeof value === "object" && value !== null && Object.hasOwn(value, "value_from");

/** `runtime.temporal.tls` env value (Python `_tls_env_value`): true unless tls is exactly `false`. */
function tlsEnvValue(spec: TypefluxYamlSpec): string {
  return spec.runtime.temporal.tls !== false ? "true" : "false";
}

/** Normalize + safety-check a secret file mount path (Python `_safe_secret_mount_path`). */
function safeSecretMountPath(pathValue: string, runtimePath: string): string {
  if (!isAbsolute(pathValue)) {
    throw new ProjectDeploymentError(`secret file reference for ${runtimePath} must use an absolute mount path`);
  }
  if (pathValue.split("/").includes("..")) {
    throw new ProjectDeploymentError(`secret file reference for ${runtimePath} must use a normalized file path`);
  }
  const normalized = posix.normalize(pathValue);
  if (normalized !== pathValue || normalized === "/") {
    throw new ProjectDeploymentError(`secret file reference for ${runtimePath} must use a normalized file path`);
  }
  return normalized;
}

/** A Kubernetes-safe Secret key from a runtime path (Python `_secret_file_key`). */
function secretFileKey(runtimePath: string): string {
  const key = runtimePath.replace(SECRET_NAME_KEY_PATTERN, "_").replace(/^_+|_+$/g, "");
  return key === "" ? "secret" : key;
}

/** The typed secret slots the deployment mounts (Python `_secret_references`, the fixed slot walk). */
function specSecretReferences(
  spec: TypefluxYamlSpec,
  secretName: string,
): { env: ProjectDeploymentSecretEnvRef[]; files: ProjectDeploymentSecretFileRef[] } {
  const records: Array<[string, SecretValueSpec]> = [];
  const append = (runtimePath: string, value: unknown): void => {
    if (isSecretValueSpec(value)) {
      records.push([runtimePath, value]);
      return;
    }
    if (typeof value === "string" && value !== "") {
      throw new ProjectDeploymentError(
        `deployment generation requires typed secret references for ${runtimePath}; use value_from.env or value_from.file`,
      );
    }
  };
  const temporal = spec.runtime.temporal;
  append("runtime.temporal.api_key", temporal.api_key);
  const tls = temporal.tls;
  if (typeof tls === "object" && tls !== null) {
    append("runtime.temporal.tls.server_root_ca_cert", (tls as Record<string, unknown>)["server_root_ca_cert"]);
    append("runtime.temporal.tls.client_cert", (tls as Record<string, unknown>)["client_cert"]);
    append("runtime.temporal.tls.client_private_key", (tls as Record<string, unknown>)["client_private_key"]);
  }
  append("runtime.provider.api_key", spec.runtime.provider.api_key);
  // Spec-declared observability credentials (#793): typed value_from references, same
  // contract as api_key; the standard-name scaffold (observabilitySecretReferences) skips
  // any env name these claim, so declared and fallback surfaces never duplicate.
  // Optional FILE sources are skipped like the custom-config ones: the secret volume's
  // items list has no optional handling, so scaffolding one would make an intentionally-
  // omitted key block the mount while runtime falls back to env (codex).
  const appendObservabilitySecret = (runtimePath: string, value: unknown): void => {
    if (
      typeof value === "object" &&
      value !== null &&
      (value as { value_from?: { file?: string; required?: boolean } }).value_from?.file !== undefined &&
      (value as { value_from: { required?: boolean } }).value_from.required === false
    ) {
      return;
    }
    append(runtimePath, value);
  };
  const declaredLangfuse = spec.runtime.observability?.langfuse;
  if (declaredLangfuse !== undefined) {
    appendObservabilitySecret("runtime.observability.langfuse.public_key", declaredLangfuse.public_key);
    appendObservabilitySecret("runtime.observability.langfuse.secret_key", declaredLangfuse.secret_key);
  }
  const declaredLangsmith = spec.runtime.observability?.langsmith;
  if (declaredLangsmith !== undefined) {
    appendObservabilitySecret("runtime.observability.langsmith.api_key", declaredLangsmith.api_key);
  }
  // AES-256-GCM payload codec key material (#188): one secret slot per declared key so the
  // generated deployment scaffolds/injects the AES key a codec-enabled worker needs at
  // preflight/startup. The runtime_path + env-var name (source.env) match exactly what
  // secret-references reports and what buildPayloadCodec resolves at runtime (Python parity).
  const codec = temporal.payload_codec;
  if (codec !== undefined) {
    for (const key of codec.keys) {
      append(`runtime.temporal.payload_codec.keys[${key.id}].value_from`, { value_from: key.value_from });
    }
  }

  const env: ProjectDeploymentSecretEnvRef[] = [];
  const files: ProjectDeploymentSecretFileRef[] = [];
  for (const [runtimePath, value] of [...records].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    const source = value.value_from;
    const required = source.required !== false; // absent/true ⇒ required; only explicit false relaxes.
    if (source.env !== undefined) {
      env.push({
        runtime_path: runtimePath,
        env_name: source.env,
        secret_name: secretName,
        secret_key: source.env,
        required,
        configured: true,
      });
    } else if (source.file !== undefined) {
      files.push({
        runtime_path: runtimePath,
        mount_path: safeSecretMountPath(source.file, runtimePath),
        secret_name: secretName,
        secret_key: secretFileKey(runtimePath),
        required,
        configured: true,
      });
    }
  }
  return { env, files };
}

/** Secret env refs derived from secret-like ENVIRONMENT VARIABLE names (Python `_environment_secret_references`). */
function environmentSecretReferences(
  variables: Readonly<Record<string, string | number | boolean>>,
  secretName: string,
  existingEnvNames: ReadonlySet<string>,
  configEnvNames: ReadonlySet<string>,
): ProjectDeploymentSecretEnvRef[] {
  const refs: ProjectDeploymentSecretEnvRef[] = [];
  for (const key of Object.keys(variables).sort()) {
    if (existingEnvNames.has(key) || configEnvNames.has(key) || !SECRET_LIKE_ENV_PATTERN.test(key)) continue;
    refs.push({
      runtime_path: `environment.variables.${key}`,
      env_name: key,
      secret_name: secretName,
      secret_key: key,
      required: true,
      configured: true,
    });
  }
  return refs;
}

/**
 * The credential env NAMES a rendered worker needs for each observability backend (#757 item 3).
 * NAMES only, per the Secret-scaffold convention (the renderer never emits a value). `custom`
 * transports carry no platform-known credentials, so they get no scaffold — the code that injects
 * the custom transport owns its own secrets. Mirrors what `langfuseObserverFromSpec` /
 * `langsmithObserverFromSpec` read from the process environment at runtime.
 */
/** The env-fallback scaffold's runtime_path is the CANONICAL spec slot (#793) — the same
 * path a declared runtime.observability.<backend>.* reference carries, so the two surfaces
 * name one slot, not a synthesized pseudo-path. */
const OBSERVABILITY_FALLBACK_SLOT_PATHS: Readonly<Record<string, string>> = {
  LANGFUSE_PUBLIC_KEY: "runtime.observability.langfuse.public_key",
  LANGFUSE_SECRET_KEY: "runtime.observability.langfuse.secret_key",
  LANGSMITH_API_KEY: "runtime.observability.langsmith.api_key",
};

const OBSERVABILITY_BACKEND_SECRET_ENV: Readonly<Record<string, readonly string[]>> = {
  langfuse: ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],
  langsmith: ["LANGSMITH_API_KEY"],
};

/**
 * Secret env refs for the resolved spec's observability backend credentials (#757 item 3). WITHOUT
 * this the renderer scaffolds ONLY typed spec secrets, so a worker declaring
 * `runtime.observability.type: langfuse|langsmith` carries NO credential surface — and because the
 * SDK observer "degrades to an untraced run with one warning" when the keys are absent, a
 * required-observability deployment would come up HEALTHY and run SILENTLY UNTRACED (the exact
 * gap #756's runtime gate closes at run time). We model the backend's credential NAMES into the
 * worker's Secret surface so the render's secretKeyRefs + scaffold + env checklist name them.
 *
 * `required` mirrors whether the effective composed policy marks observability as REQUIRED
 * (`observability.required: true`, #756): a policy-required backend's creds are a hard secretKeyRef
 * (missing ⇒ pod fails the observability gate), otherwise they are optional (missing ⇒ untraced
 * warning only). Names already present as typed/env secret refs are skipped (no duplication).
 */
function observabilitySecretReferences(
  spec: TypefluxYamlSpec,
  policy: ComposedProjectPolicy,
  secretName: string,
  existingEnvNames: ReadonlySet<string>,
): ProjectDeploymentSecretEnvRef[] {
  // `type or ""` collapses an interpolated empty type to "no backend" (policy-enforcement parity).
  const backend = spec.runtime.observability?.type || "";
  const names = OBSERVABILITY_BACKEND_SECRET_ENV[backend];
  if (names === undefined) return [];
  const obsPolicy = policy.payload["observability"];
  const required =
    typeof obsPolicy === "object" && obsPolicy !== null && !Array.isArray(obsPolicy)
      ? (obsPolicy as Record<string, unknown>)["required"] === true
      : false;
  // #793: a spec-declared credential CLAIMS its canonical slot whatever its source name —
  // scaffolding the standard fallback name for a claimed slot would render a required
  // secretKeyRef for a key nothing populates (CreateContainerConfigError at rollout).
  // Only a NON-EMPTY literal ("" is the documented `${VAR:-}` unset sentinel) or a
  // REQUIRED reference claims: an optional reference whose source is absent falls back
  // to the standard env var at runtime, so its fallback ref must stay scaffolded (codex).
  const claimsSlot = (value: string | { value_from: { required?: boolean | undefined } } | undefined): boolean => {
    if (typeof value === "string") return value !== "";
    if (typeof value === "object" && value !== null) return value.value_from.required !== false;
    return false;
  };
  const declaredLangfuse = spec.runtime.observability?.langfuse;
  const declaredLangsmith = spec.runtime.observability?.langsmith;
  const slotValues = [
    ["runtime.observability.langfuse.public_key", declaredLangfuse?.public_key],
    ["runtime.observability.langfuse.secret_key", declaredLangfuse?.secret_key],
    ["runtime.observability.langsmith.api_key", declaredLangsmith?.api_key],
  ] as const;
  const declaredSlots = new Set<string>(slotValues.filter(([, value]) => claimsSlot(value)).map(([path]) => path));
  // A declared-but-OPTIONAL reference keeps its fallback ref but demotes it to optional:
  // runtime resolution can succeed via EITHER source, so a hard secretKeyRef on the
  // standard name would block a pod whose custom optional var is the populated one (codex).
  const optionalDeclaredSlots = new Set<string>(
    slotValues
      .filter(([, value]) => typeof value === "object" && value !== null && value.value_from.required === false)
      .map(([path]) => path),
  );
  const refs: ProjectDeploymentSecretEnvRef[] = [];
  for (const envName of names) {
    if (existingEnvNames.has(envName)) continue;
    const fallbackSlot = OBSERVABILITY_FALLBACK_SLOT_PATHS[envName];
    if (fallbackSlot !== undefined && declaredSlots.has(fallbackSlot)) continue;
    const fallbackRequired = required && !(fallbackSlot !== undefined && optionalDeclaredSlots.has(fallbackSlot));
    refs.push({
      runtime_path: fallbackSlot ?? `runtime.observability.${backend}.${envName.toLowerCase()}`,
      env_name: envName,
      secret_name: secretName,
      secret_key: envName,
      required: fallbackRequired,
      configured: true,
    });
  }
  return refs;
}

const sortSecretEnvRefs = (refs: ProjectDeploymentSecretEnvRef[]): ProjectDeploymentSecretEnvRef[] =>
  [...refs].sort((a, b) =>
    a.runtime_path < b.runtime_path
      ? -1
      : a.runtime_path > b.runtime_path
        ? 1
        : a.env_name < b.env_name
          ? -1
          : a.env_name > b.env_name
            ? 1
            : 0,
  );

/** The ConfigMap-safe entries for a worker (Python `_config_map_entries`), sorted by key. */
function configMapEntries(
  spec: TypefluxYamlSpec,
  variables: Readonly<Record<string, string | number | boolean>>,
  policy: ComposedProjectPolicy,
  secretEnv: readonly ProjectDeploymentSecretEnvRef[],
  configEnvNames: ReadonlySet<string>,
): Record<string, string> {
  const secretEnvNames = new Set(secretEnv.map((ref) => ref.env_name));
  const safeNames = new Set([...SAFE_CONFIG_ENV_NAMES, ...configEnvNames]);
  const temporal = spec.runtime.temporal;
  const entries: Record<string, string> = {
    TEMPORAL_ADDRESS: temporal.address ?? "localhost:7233",
    TEMPORAL_NAMESPACE: temporal.namespace ?? "default",
    TEMPORAL_TASK_QUEUE: spec.task_queue,
    TEMPORAL_TLS: tlsEnvValue(spec),
    TYPEFLUX_EXPECTED_POLICY_HASH: policy.policyHash,
  };
  // PARITY (#687 review): a DECLARED environment variable OVERRIDES the computed fixed
  // entries above — Python's first `_config_map_entries` loop (`profile_variable_names`,
  // the profile-declared names) assigns unconditionally; only its second loop (variables
  // sourced from env FILES, which the TS environment spec does not model) skips existing
  // keys. TS `variables` IS the declared map, so every safe, non-secret declared variable
  // wins here — otherwise the same manifest yields different runtime config per edition.
  for (const key of Object.keys(variables).sort()) {
    if (secretEnvNames.has(key) || !safeNames.has(key)) continue;
    const value = variables[key];
    if (value !== undefined) entries[key] = stringifyEnvValue(value);
  }
  const provider = spec.runtime.provider;
  const modelEnv = PROVIDER_MODEL_ENV_NAMES[provider.type];
  if (modelEnv !== undefined && provider.model !== undefined && !Object.hasOwn(entries, modelEnv)) {
    entries[modelEnv] = provider.model;
  }
  const registry = spec.runtime.registry;
  if (registry.host !== undefined && !Object.hasOwn(entries, "LANGFUSE_HOST")) entries["LANGFUSE_HOST"] = registry.host;
  if (registry.label !== undefined && !Object.hasOwn(entries, "LANGFUSE_PROMPT_LABEL")) {
    entries["LANGFUSE_PROMPT_LABEL"] = registry.label;
  }
  return Object.fromEntries(Object.keys(entries).sort().map((key) => [key, entries[key]!]));
}

/** Fail closed on any environment variable that is neither ConfigMap-safe nor a Secret ref
 * (Python `_validate_environment_variables_classified`). */
function assertEnvironmentVariablesClassified(
  variables: Readonly<Record<string, string | number | boolean>>,
  configMap: Readonly<Record<string, string>>,
  secretEnv: readonly ProjectDeploymentSecretEnvRef[],
): void {
  const classified = new Set([...Object.keys(configMap), ...secretEnv.map((ref) => ref.env_name)]);
  const unclassified = Object.keys(variables).filter((key) => !classified.has(key)).sort();
  if (unclassified.length > 0) {
    throw new ProjectDeploymentError(
      "project deployment cannot classify environment variable(s) as safe ConfigMap values or Secret refs: " +
        unclassified.join(", "),
    );
  }
}

/**
 * Compose + admit the workflow's policy through the unified runtime-guard path (Python
 * `_admit_policy`, minus the temporal-api-key deploy adjustment — see the module note). At
 * least one policy must be selected; admission fails closed via the guard.
 */
function admitPolicy(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  spec: TypefluxYamlSpec,
  workflowId: string,
  environmentId: string,
  policyIds: readonly string[],
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver,
): ComposedProjectPolicy {
  const selected = selectProjectPolicyIdsForWorkflow(project, { environmentId, workflowId, explicitPolicyIds: policyIds });
  if (selected.length === 0) {
    throw new ProjectDeploymentError(
      `project deployment requires at least one selected policy for environment '${environmentId}' workflow '${workflowId}'`,
    );
  }
  // The guard composes, runs compliance + transitive closure, and throws
  // ProjectPolicyEnforcementError on any failure — the SAME gate the operate tier uses.
  // It runs under the RESOLVED ENVIRONMENT's variable overlay (review item 4; Python
  // `_admit_policy` wraps composition + checks in `project_environment_context`), so an
  // env-backed policy value (a required api key, a region variable) is evaluated against the
  // deployment target's environment file, not the operator's shell.
  const admit = () =>
    buildProjectPolicyRuntimeGuard(project, sources, {
      spec,
      workflowId,
      environmentId,
      explicitPolicyIds: policyIds,
      resolveSubworkflowSpec,
    });
  const environment = Object.hasOwn(sources.environments, environmentId) ? sources.environments[environmentId] : undefined;
  const guard = environment !== undefined ? withEnvironmentContext(environment, undefined, admit) : admit();
  // `selected` is non-empty, so the guard is always defined here.
  return guard!.policy;
}

/**
 * Run the #756 closure observability-consistency gate at plan BUILD (#757 review): a composed
 * worker builds ONE observer — the parent's — so a child declaring a DIFFERENT real backend
 * (parent langfuse + child langsmith) must fail HERE, at authoring time, with the same
 * {@link assertConsistentComposedObservability} error the worker boot raises — not pass
 * plan/PR/promote cleanly (each spec individually policy-compliant, only the parent's creds
 * scaffolded) and then crash-loop at pod boot on `ObservabilityCompositionError`. With
 * consistency verified, the parent's backend IS the closure's single effective backend, which is
 * exactly what {@link observabilitySecretReferences} scaffolds credentials for. Unresolvable
 * refs are skipped here — policy closure admission already fails them closed.
 */
export function assertClosureObservabilityConsistent(
  spec: TypefluxYamlSpec,
  workflowId: string,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver,
): void {
  const children: Array<{ id: string; spec: TypefluxYamlSpec }> = [];
  for (const { ref, child } of walkSubworkflowClosure(spec, resolveSubworkflowSpec, workflowId)) {
    if (child !== undefined) children.push({ id: ref, spec: child });
  }
  assertConsistentComposedObservability({ id: workflowId, spec }, children);
}

function workerPlan(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  workflowId: string,
  environmentId: string,
  resolved: DeploymentResolvedWorkflow,
  policyIds: readonly string[],
  configEnvNames: ReadonlySet<string>,
  projectPathInImage: string,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver,
): ProjectDeploymentWorkerPlan {
  const { spec, variables } = resolved;
  const policy = admitPolicy(project, sources, spec, workflowId, environmentId, policyIds, resolveSubworkflowSpec);
  // #757 review: observability closure consistency runs AFTER policy admission (policy errors take
  // precedence; unresolved refs already failed closed) and BEFORE the credential scaffolding below,
  // so the scaffold provably keys off the verified single effective backend.
  assertClosureObservabilityConsistent(spec, workflowId, resolveSubworkflowSpec);
  const name = resourceName(project.name, environmentId, workflowId);
  const configMapName = `${name}-config`;
  const secretName = `${name}-secrets`;
  const { env: specEnv, files: secretFiles } = specSecretReferences(spec, secretName);
  const existingEnvNames = new Set(specEnv.map((ref) => ref.env_name));
  const envRefs = environmentSecretReferences(variables, secretName, existingEnvNames, configEnvNames);
  // Observability creds (#757 item 3) are appended AFTER the typed + env-derived refs and skip any
  // name already claimed by them, so a spec that also declares LANGFUSE_SECRET_KEY as an env var is
  // not double-referenced.
  const claimedEnvNames = new Set([...existingEnvNames, ...envRefs.map((ref) => ref.env_name)]);
  const observabilityRefs = observabilitySecretReferences(spec, policy, secretName, claimedEnvNames);
  const secretEnv = sortSecretEnvRefs([...specEnv, ...envRefs, ...observabilityRefs]);
  const configMap = configMapEntries(spec, variables, policy, secretEnv, configEnvNames);
  assertEnvironmentVariablesClassified(variables, configMap, secretEnv);
  const command = workerCommand({
    projectPathInImage,
    workflowId,
    environmentId,
    policyIds: policy.selectedPolicyIds,
    expectedPolicyHash: policy.policyHash,
  });
  return {
    name,
    workflow_id: workflowId,
    workflow_name: spec.workflow.name,
    yaml_project: spec.project,
    yaml_name: spec.name,
    workflow_path: resolved.workflowPath,
    environment_id: environmentId,
    environment_name: resolved.environmentName,
    task_queue: spec.task_queue,
    project_path_in_image: projectPathInImage,
    command,
    config_map_name: configMapName,
    secret_name: secretName,
    config_map: configMap,
    secret_env: secretEnv,
    secret_files: secretFiles,
    policy: {
      selected_policy_ids: policy.selectedPolicyIds,
      applied_policy_ids: policy.appliedPolicyIds,
      policy_names: policy.policyNames,
      policy_hash: policy.policyHash,
    },
  };
}

/**
 * Build the deterministic, secret-free deployment plan for a project's resolved workers
 * (Python `build_project_deployment_plan`, slice-1 subset — no artifact rendering). The
 * caller supplies `resolveWorkflow`, which resolves each workflow's spec + environment
 * metadata under the target environment (and doubles as the sub-workflow closure resolver).
 */
export function buildProjectDeploymentPlan(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  options: BuildProjectDeploymentPlanOptions,
  resolveWorkflow: DeploymentWorkflowResolver,
): ProjectDeploymentPlan {
  const imageDigestPinned = validateDeploymentImage(options.image, {
    allowMutableImage: options.allowMutableImage === true,
    allowPlaceholderImage: options.allowPlaceholderImage === true,
  });
  const ids = selectedWorkflowIds(project, options.workflowIds ?? []);
  const report = validateProjectBundle(project, sources);
  if (!report.ok) {
    const messages = report.issues.map((issue) => issue.message).join("; ");
    throw new ProjectDeploymentError(`project deployment references are invalid: ${messages}`);
  }
  const policyIds = options.policyIds ?? [];
  const configEnvNames = new Set(options.configEnvNames ?? []);
  const inImagePath = validateProjectPathInImage(
    options.projectPathInImage ?? defaultProjectPathInImage(options.manifestPath),
  );
  const resolveSubworkflowSpec: SubworkflowClosureSpecResolver = (siblingId) => resolveWorkflow(siblingId)?.spec;

  const workers: ProjectDeploymentWorkerPlan[] = [];
  const taskQueues = new Map<string, string>();
  for (const workflowId of ids) {
    const resolved = resolveWorkflow(workflowId);
    if (resolved === undefined) {
      throw new ProjectDeploymentError(`project deployment references an unknown workflow: '${workflowId}'`);
    }
    if (options.allowSharedTaskQueue !== true) {
      const previous = taskQueues.get(resolved.spec.task_queue);
      if (previous !== undefined) {
        throw new ProjectDeploymentError(
          `project deployment would create separate workers for workflows '${previous}' and '${workflowId}' on ` +
            `shared task queue '${resolved.spec.task_queue}'; pass --allow-shared-task-queue to allow this`,
        );
      }
      taskQueues.set(resolved.spec.task_queue, workflowId);
    }
    workers.push(
      workerPlan(project, sources, workflowId, options.environmentId, resolved, policyIds, configEnvNames, inImagePath, resolveSubworkflowSpec),
    );
  }
  if (workers.length === 0) {
    throw new ProjectDeploymentError("project deployment selected no workflows to plan");
  }
  workers.sort((a, b) => (a.workflow_id < b.workflow_id ? -1 : a.workflow_id > b.workflow_id ? 1 : 0));
  return {
    version: "1",
    target: options.target ?? "kubernetes",
    project_name: project.name,
    project_manifest_path: options.projectManifestPath ?? options.manifestPath ?? "",
    project_path_in_image: inImagePath,
    environment_id: options.environmentId,
    environment_name: workers[0]!.environment_name,
    image: options.image,
    image_digest_pinned: imageDigestPinned,
    workers,
  };
}
