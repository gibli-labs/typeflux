/**
 * Kubernetes artifact rendering (deployment tier, #687 slice 2; Python
 * `project/deployment.py` `render_project_deployment_plan` + the `_kubernetes_*` helpers).
 *
 * Renders a {@link ProjectDeploymentPlan} to four deterministic, SECRET-FREE artifacts under an
 * output directory:
 *
 *   - `deployment-plan.json`   — the plan itself, sorted-key JSON (the identity record).
 *   - `kubernetes.yaml`        — ConfigMap + Deployment per worker (NO Secret manifests, so it is
 *                                safe to `kubectl apply` repeatedly without clobbering real secrets).
 *   - `secret.scaffold.yaml`   — BLANK Secret templates the operator provisions from a secret manager.
 *   - `secrets.env.example`    — a dotenv-style secret key checklist.
 *
 * The Deployment mirrors the Python renderer field-for-field: non-root pod security context, the
 * three preflight-marker probes ({@link PREFLIGHT_MARKER_PATH}), the read-only root filesystem +
 * dropped capabilities container context, the Downward-API placement env, and RFC-1123-sanitized
 * naming/labels. Byte-identity with Python is NOT a goal (YAML emitters differ); structural +
 * behavioral parity is. That parity includes the ConfigMap contents the plan carries: a DECLARED
 * environment variable overrides the computed fixed keys (TEMPORAL_*, TYPEFLUX_EXPECTED_POLICY_HASH)
 * exactly as Python's `_config_map_entries` first loop does — see `configMapEntries` in
 * `deployment.ts`.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, statSync, writeFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { stringify as stringifyYaml } from "yaml";

import {
  PREFLIGHT_MARKER_PATH,
  ProjectDeploymentError,
  type ProjectDeploymentPlan,
  type ProjectDeploymentWorkerPlan,
} from "./deployment.js";

const KUBERNETES_LABEL_CHARS_PATTERN = /[^A-Za-z0-9_.-]+/g;
const KUBERNETES_LABEL_EDGE_PATTERN = /^[^A-Za-z0-9]+|[^A-Za-z0-9]+$/g;
const LABEL_STRIP_PATTERN = /^[-_.]+|[-_.]+$/g;

/** The kind of artifact a rendered file is (Python `ProjectDeploymentRenderedFile.kind`). */
export type RenderedFileKind = "plan" | "kubernetes" | "secret_template";

/** One rendered artifact + its content digest (Python `ProjectDeploymentRenderedFile`). */
export interface ProjectDeploymentRenderedFile {
  path: string;
  kind: RenderedFileKind;
  sha256: string;
}

/** The result of rendering a plan's artifacts (Python `ProjectDeploymentRenderResult`). */
export interface ProjectDeploymentRenderResult {
  output_dir: string;
  files: ProjectDeploymentRenderedFile[];
}

/** Recursively key-sort a JSON value so serialization is deterministic (Python `sort_keys=True`). */
function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = sortKeysDeep((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

/** A sorted-key JSON serialization of the plan, trailing newline (Python `_render_plan_json`). */
function renderPlanJson(plan: ProjectDeploymentPlan): string {
  return JSON.stringify(sortKeysDeep(plan), null, 2) + "\n";
}

/** An RFC-1123-safe label value, hashed-suffixed when too long (Python `_label_value`). */
function labelValue(value: string): string {
  let label = value.replace(KUBERNETES_LABEL_CHARS_PATTERN, "-").replace(LABEL_STRIP_PATTERN, "");
  label = label.replace(KUBERNETES_LABEL_EDGE_PATTERN, "");
  if (label === "") return "typeflux";
  if (label.length <= 63) return label;
  const digest = createHash("sha256").update(label, "utf-8").digest("hex").slice(0, 8);
  return `${label.slice(0, 54).replace(/[-_.]+$/g, "")}-${digest}`;
}

/** The six standard worker labels (Python `_worker_labels`). */
function workerLabels(plan: ProjectDeploymentPlan, worker: ProjectDeploymentWorkerPlan, component: string): Record<string, string> {
  return {
    "app.kubernetes.io/name": worker.name,
    "app.kubernetes.io/component": labelValue(component),
    "app.kubernetes.io/part-of": labelValue(plan.project_name),
    "typeflux.io/project": labelValue(plan.project_name),
    "typeflux.io/environment": labelValue(plan.environment_id),
    "typeflux.io/workflow": labelValue(worker.workflow_id),
  };
}

/** Object metadata: name + labels + provenance annotations (Python `_kubernetes_metadata`). */
function kubernetesMetadata(
  plan: ProjectDeploymentPlan,
  worker: ProjectDeploymentWorkerPlan,
  name: string,
  component: string,
): Record<string, unknown> {
  return {
    name,
    labels: workerLabels(plan, worker, component),
    annotations: {
      "typeflux.io/project-manifest-path": plan.project_manifest_path,
      "typeflux.io/project-path-in-image": plan.project_path_in_image,
      "typeflux.io/workflow-path": worker.workflow_path,
      "typeflux.io/policy-hash": worker.policy.policy_hash,
    },
  };
}

/** A fresh probe command per call so the YAML emitter never anchors a shared array (Python `_preflight_marker_probe_command`). */
function preflightProbeCommand(): string[] {
  return ["sh", "-c", `test -f ${PREFLIGHT_MARKER_PATH}`];
}

/** Sort a `Record<string,string>` by key (Python `dict(sorted(...))`). */
function sortedRecord(record: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.keys(record).sort().map((key) => [key, record[key]!]));
}

/** The Downward-API + placement env every worker container carries (Python `_runtime_placement_env`). */
function runtimePlacementEnv(plan: ProjectDeploymentPlan, worker: ProjectDeploymentWorkerPlan): Array<Record<string, unknown>> {
  return [
    { name: "TYPEFLUX_RUNTIME_PLATFORM", value: "kubernetes" },
    { name: "TYPEFLUX_K8S_NAMESPACE", valueFrom: { fieldRef: { fieldPath: "metadata.namespace" } } },
    { name: "TYPEFLUX_K8S_POD_NAME", valueFrom: { fieldRef: { fieldPath: "metadata.name" } } },
    { name: "TYPEFLUX_K8S_POD_UID", valueFrom: { fieldRef: { fieldPath: "metadata.uid" } } },
    { name: "TYPEFLUX_K8S_NODE_NAME", valueFrom: { fieldRef: { fieldPath: "spec.nodeName" } } },
    { name: "TYPEFLUX_K8S_SERVICE_ACCOUNT", valueFrom: { fieldRef: { fieldPath: "spec.serviceAccountName" } } },
    { name: "TYPEFLUX_K8S_DEPLOYMENT_NAME", value: worker.name },
    { name: "TYPEFLUX_K8S_WORKER_NAME", value: worker.name },
    { name: "TYPEFLUX_CONTAINER_IMAGE", value: plan.image },
  ];
}

/** The ConfigMap document for a worker (Python `_kubernetes_config_map`). */
function kubernetesConfigMap(plan: ProjectDeploymentPlan, worker: ProjectDeploymentWorkerPlan): Record<string, unknown> {
  return {
    apiVersion: "v1",
    kind: "ConfigMap",
    metadata: kubernetesMetadata(plan, worker, worker.config_map_name, "config"),
    data: sortedRecord(worker.config_map),
  };
}

/** The BLANK Secret template document for a worker (Python `_kubernetes_secret_template`). */
function kubernetesSecretTemplate(plan: ProjectDeploymentPlan, worker: ProjectDeploymentWorkerPlan): Record<string, unknown> {
  const keys = [
    ...new Set([...worker.secret_env.map((ref) => ref.secret_key), ...worker.secret_files.map((ref) => ref.secret_key)]),
  ].sort();
  return {
    apiVersion: "v1",
    kind: "Secret",
    metadata: kubernetesMetadata(plan, worker, worker.secret_name, "secrets"),
    type: "Opaque",
    stringData: Object.fromEntries(keys.map((key) => [key, ""])),
  };
}

/** The Deployment document for a worker (Python `_kubernetes_deployment`). */
function kubernetesDeployment(plan: ProjectDeploymentPlan, worker: ProjectDeploymentWorkerPlan): Record<string, unknown> {
  const labels = workerLabels(plan, worker, "worker");
  const volumeMounts: Array<Record<string, unknown>> = [{ name: "tmp", mountPath: "/tmp" }];
  const env: Array<Record<string, unknown>> = runtimePlacementEnv(plan, worker);
  const container: Record<string, unknown> = {
    name: "worker",
    image: plan.image,
    imagePullPolicy: "IfNotPresent",
    command: [...worker.command],
    securityContext: {
      allowPrivilegeEscalation: false,
      readOnlyRootFilesystem: true,
      capabilities: { drop: ["ALL"] },
    },
    envFrom: [{ configMapRef: { name: worker.config_map_name } }],
    startupProbe: { exec: { command: preflightProbeCommand() }, failureThreshold: 12, periodSeconds: 10, timeoutSeconds: 10 },
    readinessProbe: { exec: { command: preflightProbeCommand() }, initialDelaySeconds: 5, periodSeconds: 15, timeoutSeconds: 5 },
    livenessProbe: { exec: { command: preflightProbeCommand() }, initialDelaySeconds: 30, periodSeconds: 30, timeoutSeconds: 5 },
    resources: { requests: { cpu: "500m", memory: "512Mi" }, limits: { cpu: "2", memory: "2Gi" } },
    volumeMounts,
  };
  for (const ref of worker.secret_env) {
    env.push({
      name: ref.env_name,
      valueFrom: { secretKeyRef: { name: ref.secret_name, key: ref.secret_key, optional: !ref.required } },
    });
  }
  container["env"] = env;
  for (const ref of worker.secret_files) {
    volumeMounts.push({
      name: "typeflux-secret-files",
      mountPath: ref.mount_path,
      subPath: ref.secret_key,
      readOnly: true,
    });
  }

  const volumes: Array<Record<string, unknown>> = [{ name: "tmp", emptyDir: {} }];
  if (worker.secret_files.length > 0) {
    volumes.push({
      name: "typeflux-secret-files",
      secret: {
        secretName: worker.secret_name,
        items: worker.secret_files.map((ref) => ({ key: ref.secret_key, path: ref.secret_key })),
      },
    });
  }

  return {
    apiVersion: "apps/v1",
    kind: "Deployment",
    metadata: kubernetesMetadata(plan, worker, worker.name, "worker"),
    spec: {
      replicas: 1,
      selector: { matchLabels: labels },
      template: {
        metadata: { labels },
        spec: {
          terminationGracePeriodSeconds: 45,
          securityContext: {
            runAsNonRoot: true,
            runAsUser: 10001,
            runAsGroup: 10001,
            fsGroup: 10001,
            seccompProfile: { type: "RuntimeDefault" },
          },
          containers: [container],
          volumes,
        },
      },
    },
  };
}

/** Multi-document YAML with an explicit `---` start marker per doc (Python `yaml.safe_dump_all(..., explicit_start=True)`). */
function dumpAllYaml(docs: Array<Record<string, unknown>>): string {
  return docs.map((doc) => "---\n" + stringifyYaml(doc, { sortMapEntries: false })).join("");
}

/** The `kubernetes.yaml` body: ConfigMap + Deployment per worker, no Secret manifests (Python `_render_kubernetes_yaml`). */
function renderKubernetesYaml(plan: ProjectDeploymentPlan): string {
  const docs: Array<Record<string, unknown>> = [];
  for (const worker of plan.workers) {
    docs.push(kubernetesConfigMap(plan, worker));
    docs.push(kubernetesDeployment(plan, worker));
  }
  return dumpAllYaml(docs);
}

/** The `secret.scaffold.yaml` body: header + BLANK Secret templates (Python `_render_secret_scaffold_yaml`). */
function renderSecretScaffoldYaml(plan: ProjectDeploymentPlan): string {
  const header =
    "# Typeflux deployment Secret scaffold.\n" +
    "# WARNING: these are BLANK placeholder values. Do NOT `kubectl apply` this\n" +
    "# over a populated Secret — it would erase real secret values. Provision\n" +
    "# these Secrets from your secret manager; kubernetes.yaml references them by\n" +
    "# name and never defines or overwrites them.\n";
  const docs = plan.workers
    .filter((worker) => worker.secret_env.length > 0 || worker.secret_files.length > 0)
    .map((worker) => kubernetesSecretTemplate(plan, worker));
  if (docs.length === 0) return header + "# No Secret values are required for this deployment.\n";
  return header + dumpAllYaml(docs);
}

/** The `secrets.env.example` body: a dotenv secret-key checklist (Python `_render_secret_env_example`). */
function renderSecretEnvExample(plan: ProjectDeploymentPlan): string {
  const lines = [
    "# Typeflux deployment secret template",
    `# project=${plan.project_name}`,
    `# environment=${plan.environment_id}`,
    "# Populate these values in your secret manager. Do not commit real secrets.",
    "# For local smoke tests with kubectl --from-env-file, use unquoted KEY=value",
    "# lines or generate a kubectl-only env file with a dotenv parser first.",
    "",
  ];
  for (const worker of plan.workers) {
    lines.push(`# ${worker.workflow_id}: Kubernetes Secret ${worker.secret_name}`);
    for (const ref of worker.secret_env) lines.push(`${ref.secret_key}=`);
    for (const fileRef of worker.secret_files) {
      lines.push(
        `# file secret key ${fileRef.secret_key} mounts to ${fileRef.mount_path}; ` +
          "put the file contents in the Kubernetes Secret template",
      );
    }
    if (worker.secret_env.length === 0 && worker.secret_files.length === 0) lines.push("# no secret values required");
    lines.push("");
  }
  return lines.join("\n").replace(/\s+$/, "") + "\n";
}

/** Write one artifact + record its digest (Python `_write_rendered_file`). */
function writeRenderedFile(path: string, content: string, kind: RenderedFileKind): ProjectDeploymentRenderedFile {
  writeFileSync(path, content, "utf-8");
  return { path, kind, sha256: createHash("sha256").update(content, "utf-8").digest("hex") };
}

/**
 * Render the four deterministic, secret-free deployment artifacts for a plan under `outputDir`
 * (Python `render_project_deployment_plan`). Creates the directory if needed; refuses a path that
 * exists and is not a directory. Returns the resolved output dir and the written-file digests.
 */
export function renderProjectDeploymentPlan(plan: ProjectDeploymentPlan, outputDir: string): ProjectDeploymentRenderResult {
  const outputPath = resolvePath(outputDir);
  if (existsSync(outputPath) && !statSync(outputPath).isDirectory()) {
    throw new ProjectDeploymentError(`deployment output path is not a directory: ${outputPath}`);
  }
  mkdirSync(outputPath, { recursive: true });
  const files: ProjectDeploymentRenderedFile[] = [
    writeRenderedFile(resolvePath(outputPath, "deployment-plan.json"), renderPlanJson(plan), "plan"),
    writeRenderedFile(resolvePath(outputPath, "kubernetes.yaml"), renderKubernetesYaml(plan), "kubernetes"),
    writeRenderedFile(resolvePath(outputPath, "secret.scaffold.yaml"), renderSecretScaffoldYaml(plan), "secret_template"),
    writeRenderedFile(resolvePath(outputPath, "secrets.env.example"), renderSecretEnvExample(plan), "secret_template"),
  ];
  return { output_dir: outputPath, files };
}
