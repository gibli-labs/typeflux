import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { parseAllDocuments } from "yaml";
import { afterAll, describe, expect, it } from "vitest";

import { renderProjectDeploymentPlan, type ProjectDeploymentPlan } from "../src/index.js";

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

function outputDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-render-"));
  createdDirs.push(dir);
  return dir;
}

const SECRETS = "typeflux-acme-prod-review-secrets";

/** A synthetic plan exercising secret env refs, a file secret, config-map data, and a punctuated name. */
function samplePlan(overrides: Partial<ProjectDeploymentPlan["workers"][number]> = {}): ProjectDeploymentPlan {
  return {
    version: "1",
    target: "kubernetes",
    project_name: "Acme Project!",
    project_manifest_path: "/repo/typeflux.project.yaml",
    project_path_in_image: "/app/typeflux.project.yaml",
    environment_id: "prod",
    environment_name: "Production",
    image: "registry.example/worker@sha256:" + "a".repeat(64),
    image_digest_pinned: true,
    workers: [
      {
        name: "typeflux-acme-prod-review",
        workflow_id: "review",
        workflow_name: "Review",
        yaml_project: "p",
        yaml_name: "n",
        workflow_path: "review.yaml",
        environment_id: "prod",
        environment_name: "Production",
        task_queue: "wf-queue",
        project_path_in_image: "/app/typeflux.project.yaml",
        command: ["sh", "-c", "rm -f /tmp/typeflux-preflight-ok && typeflux-yaml-worker /app/typeflux.project.yaml"],
        config_map_name: "typeflux-acme-prod-review-config",
        secret_name: SECRETS,
        config_map: { TEMPORAL_TASK_QUEUE: "wf-queue", TEMPORAL_ADDRESS: "localhost:7233" },
        secret_env: [
          {
            runtime_path: "runtime.provider.api_key",
            env_name: "OPENAI_API_KEY",
            secret_name: SECRETS,
            secret_key: "OPENAI_API_KEY",
            required: true,
            configured: true,
          },
        ],
        secret_files: [
          {
            runtime_path: "runtime.temporal.tls.client_cert",
            mount_path: "/etc/tls/client.pem",
            secret_name: SECRETS,
            secret_key: "runtime_temporal_tls_client_cert",
            required: true,
            configured: true,
          },
        ],
        policy: {
          selected_policy_ids: ["only_mini"],
          applied_policy_ids: ["only_mini"],
          policy_names: ["only_mini"],
          policy_hash: "a".repeat(64),
        },
        ...overrides,
      },
    ],
  };
}

function render(plan: ProjectDeploymentPlan): { dir: string; read: (name: string) => string } {
  const dir = outputDir();
  renderProjectDeploymentPlan(plan, dir);
  return { dir, read: (name) => readFileSync(join(dir, name), "utf-8") };
}

describe("renderProjectDeploymentPlan (#687 slice 2)", () => {
  it("writes exactly the four secret-free artifacts", () => {
    const { dir, read } = render(samplePlan());
    expect(readdirSync(dir).sort()).toEqual([
      "deployment-plan.json",
      "kubernetes.yaml",
      "secret.scaffold.yaml",
      "secrets.env.example",
    ]);
    // kubernetes.yaml never carries Secret manifests (safe to re-apply).
    expect(read("kubernetes.yaml")).not.toContain("kind: Secret");
  });

  it("renders the Deployment with the preflight-marker probes, non-root context, and RFC-1123 labels", () => {
    const { read } = render(samplePlan());
    const docs = parseAllDocuments(read("kubernetes.yaml")).map((doc) => doc.toJS() as Record<string, unknown>);
    expect(docs.map((doc) => doc.kind)).toEqual(["ConfigMap", "Deployment"]);
    const deployment = docs.find((doc) => doc.kind === "Deployment") as Record<string, unknown>;
    const podSpec = (deployment.spec as { template: { spec: Record<string, unknown> } }).template.spec;
    const container = (podSpec.containers as Array<Record<string, unknown>>)[0]!;

    // The three probes gate on the preflight marker.
    for (const probe of ["startupProbe", "readinessProbe", "livenessProbe"] as const) {
      expect((container[probe] as { exec: { command: string[] } }).exec.command).toEqual([
        "sh",
        "-c",
        "test -f /tmp/typeflux-preflight-ok",
      ]);
    }
    // Container hardening + non-root pod security context.
    expect(container.securityContext).toMatchObject({
      allowPrivilegeEscalation: false,
      readOnlyRootFilesystem: true,
      capabilities: { drop: ["ALL"] },
    });
    expect(podSpec.securityContext).toMatchObject({ runAsNonRoot: true, runAsUser: 10001, seccompProfile: { type: "RuntimeDefault" } });
    expect(podSpec.terminationGracePeriodSeconds).toBe(45);

    // The punctuated project name is sanitized into a valid label value.
    const labels = (deployment.metadata as { labels: Record<string, string> }).labels;
    expect(labels["app.kubernetes.io/part-of"]).toBe("Acme-Project");
    expect(labels["typeflux.io/environment"]).toBe("prod");
    // Downward-API placement env + the container image are stamped.
    const env = container.env as Array<{ name: string; value?: string; valueFrom?: unknown }>;
    expect(env.find((e) => e.name === "TYPEFLUX_RUNTIME_PLATFORM")?.value).toBe("kubernetes");
    expect(env.find((e) => e.name === "TYPEFLUX_K8S_POD_NAME")?.valueFrom).toBeDefined();
  });

  it("mounts secret env refs and file secrets, with the file secret volume + subPath mount", () => {
    const { read } = render(samplePlan());
    const deployment = parseAllDocuments(read("kubernetes.yaml"))
      .map((doc) => doc.toJS() as Record<string, unknown>)
      .find((doc) => doc.kind === "Deployment") as Record<string, unknown>;
    const podSpec = (deployment.spec as { template: { spec: Record<string, unknown> } }).template.spec;
    const container = (podSpec.containers as Array<Record<string, unknown>>)[0]!;

    const secretEnv = (container.env as Array<{ name: string; valueFrom?: { secretKeyRef?: { name: string; key: string; optional: boolean } } }>).find(
      (e) => e.name === "OPENAI_API_KEY",
    );
    expect(secretEnv?.valueFrom?.secretKeyRef).toMatchObject({ name: SECRETS, key: "OPENAI_API_KEY", optional: false });

    const mounts = container.volumeMounts as Array<{ name: string; mountPath: string; subPath?: string; readOnly?: boolean }>;
    expect(mounts).toContainEqual({ name: "tmp", mountPath: "/tmp" });
    expect(mounts).toContainEqual({
      name: "typeflux-secret-files",
      mountPath: "/etc/tls/client.pem",
      subPath: "runtime_temporal_tls_client_cert",
      readOnly: true,
    });
    const volumes = podSpec.volumes as Array<Record<string, unknown>>;
    expect(volumes.find((v) => v.name === "typeflux-secret-files")).toMatchObject({
      secret: { secretName: SECRETS, items: [{ key: "runtime_temporal_tls_client_cert", path: "runtime_temporal_tls_client_cert" }] },
    });
  });

  it("renders a BLANK Secret scaffold and a secrets.env checklist", () => {
    const { read } = render(samplePlan());
    const scaffold = read("secret.scaffold.yaml");
    expect(scaffold).toMatch(/WARNING: these are BLANK placeholder values/);
    const secretDoc = parseAllDocuments(scaffold)
      .map((doc) => doc.toJS() as Record<string, unknown>)
      .find((doc) => doc?.kind === "Secret") as Record<string, unknown>;
    expect(secretDoc.type).toBe("Opaque");
    // Both the env secret and the file secret keys are present and blank.
    expect(secretDoc.stringData).toEqual({ OPENAI_API_KEY: "", runtime_temporal_tls_client_cert: "" });

    const envExample = read("secrets.env.example");
    expect(envExample).toContain("OPENAI_API_KEY=");
    expect(envExample).toContain("file secret key runtime_temporal_tls_client_cert mounts to /etc/tls/client.pem");
  });

  it("emits a no-secrets scaffold when a worker needs no Secret material", () => {
    const { read } = render(samplePlan({ secret_env: [], secret_files: [] }));
    expect(read("secret.scaffold.yaml")).toContain("# No Secret values are required for this deployment.");
  });

  it("renders deployment-plan.json as sorted-key JSON that round-trips", () => {
    const plan = samplePlan();
    const { read } = render(plan);
    const parsed = JSON.parse(read("deployment-plan.json"));
    expect(parsed.project_path_in_image).toBe("/app/typeflux.project.yaml");
    expect(parsed.workers[0].workflow_id).toBe("review");
    // sort_keys=True: top-level keys are alphabetical.
    const keys = Object.keys(parsed);
    expect(keys).toEqual([...keys].sort());
  });
});
