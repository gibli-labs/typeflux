/**
 * `typeflux-yaml-worker` preflight (#687 review item 2): preflight must compose the SAME
 * options assembly the live worker runs with — the OOTB env-keyed provider/registry wiring
 * included — and the bindings module must be optional (only what MUST be code needs it).
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, afterEach, describe, expect, it } from "vitest";

import { main } from "../src/worker-entry.js";

// The bindings module is dynamic-imported from disk and itself imports zod, so it must live
// UNDER the package (module resolution walks up to this package's node_modules) — not the OS
// tmpdir, where `import "zod"` would not resolve.
const packageTestDir = dirname(fileURLToPath(import.meta.url));
const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

const ENV_KEYS = [
  "TYPEFLUX_YAML_PATH",
  "TYPEFLUX_WORKER_BINDINGS",
  "OPENAI_API_KEY",
  "LANGFUSE_PUBLIC_KEY",
  "LANGFUSE_SECRET_KEY",
] as const;
const savedEnv = new Map(ENV_KEYS.map((key) => [key, process.env[key]]));
afterEach(() => {
  for (const key of ENV_KEYS) {
    const saved = savedEnv.get(key);
    if (saved === undefined) delete process.env[key];
    else process.env[key] = saved;
  }
});

/** An OOTB openai spec: vendor provider (no injected transport), inline registry. */
const SPEC = `
project: p
name: n
task_queue: entry-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: "Summarize: {{text}}" } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

/** A bindings module carrying ONLY schemas — no provider/transports/registry (those auto-wire). */
const SCHEMAS_ONLY_BINDINGS = `
import { z } from "zod";
export const schemas = {
  "schemas:In": z.object({ text: z.string() }),
  "schemas:Out": z.object({ summary: z.string() }),
};
`;

function writeFixture(): string {
  const dir = mkdtempSync(join(packageTestDir, "tmp-worker-entry-"));
  createdDirs.push(dir);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "typeflux.yaml"), SPEC);
  writeFileSync(join(dir, "bindings.mjs"), SCHEMAS_ONLY_BINDINGS);
  return dir;
}

/** A minimal project whose ENVIRONMENT declares the OOTB credential — the worker must
 *  adopt the overlay (Bugbot #706) so preflight/policy agree with deploy admission. */
function writeProjectFixture(): string {
  const dir = mkdtempSync(join(packageTestDir, "tmp-worker-entry-project-"));
  createdDirs.push(dir);
  writeFileSync(join(dir, "review.yaml"), SPEC);
  writeFileSync(join(dir, "bindings.mjs"), SCHEMAS_ONLY_BINDINGS);
  writeFileSync(
    join(dir, "prod.env.yaml"),
    "name: prod\nvariables:\n  OPENAI_API_KEY: env-file-key\n",
  );
  writeFileSync(
    join(dir, "typeflux.project.yaml"),
    'version: "1"\nname: acme\nworkflows:\n  - { id: review, path: review.yaml }\nenvironments:\n  prod: prod.env.yaml\n',
  );
  return dir;
}

/** A langfuse-tracing spec + a project whose policy REQUIRES observability (#756). */
const SPEC_LANGFUSE = `
project: p
name: n
task_queue: entry-queue
runtime:
  temporal: {}
  observability:
    type: langfuse
  registry: { type: inline, prompts: { p/x: "Summarize: {{text}}" } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

/** A project whose selected policy sets observability.required — the regulated tier (#756). */
function writeRequiredObservabilityProject(): string {
  const dir = mkdtempSync(join(packageTestDir, "tmp-worker-entry-obs-"));
  createdDirs.push(dir);
  writeFileSync(join(dir, "review.yaml"), SPEC_LANGFUSE);
  writeFileSync(join(dir, "bindings.mjs"), SCHEMAS_ONLY_BINDINGS);
  writeFileSync(
    join(dir, "require_obs.policy.yaml"),
    "name: require_obs\nobservability: { required: true, allowed_backends: [langfuse] }\n",
  );
  writeFileSync(join(dir, "prod.env.yaml"), "name: prod\nvariables: {}\n");
  writeFileSync(
    join(dir, "typeflux.project.yaml"),
    'version: "1"\nname: acme\nworkflows:\n  - { id: review, path: review.yaml }\n' +
      "policies:\n  require_obs: require_obs.policy.yaml\n" +
      "environments:\n  prod: prod.env.yaml\n" +
      "validation:\n  targets:\n    prod-review: { workflows: [review], environment: prod, policies: [require_obs] }\n",
  );
  return dir;
}

describe("typeflux-yaml-worker fails closed on required-observability with absent creds (#756)", () => {
  const projectArgs = (dir: string) => [
    join(dir, "typeflux.project.yaml"),
    "--workflow",
    "review",
    "--environment",
    "prod",
    "--policy",
    "require_obs",
    "--preflight",
  ];

  it("refuses preflight — before the readiness marker — naming the langfuse env vars", async () => {
    const dir = writeRequiredObservabilityProject();
    delete process.env.TYPEFLUX_YAML_PATH;
    process.env.TYPEFLUX_WORKER_BINDINGS = join(dir, "bindings.mjs");
    process.env.OPENAI_API_KEY = "sk-test"; // the openai auto-wire must not be what fails
    delete process.env.LANGFUSE_PUBLIC_KEY;
    delete process.env.LANGFUSE_SECRET_KEY;
    // The preflight route traverses the ONE assembleYamlRuntime gate (#756): autoWire fails to
    // resolve the observer from the absent keys, so assembly refuses BEFORE the readiness
    // marker, naming exactly what to export — a required-observability deployment can never
    // pass preflight and then run untraced at poll time.
    await expect(main(projectArgs(dir))).rejects.toThrow(
      /observability\.required.*LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY/s,
    );
  });

  it("preflights green once the langfuse credentials are present", async () => {
    const dir = writeRequiredObservabilityProject();
    delete process.env.TYPEFLUX_YAML_PATH;
    process.env.TYPEFLUX_WORKER_BINDINGS = join(dir, "bindings.mjs");
    process.env.OPENAI_API_KEY = "sk-test";
    process.env.LANGFUSE_PUBLIC_KEY = "pk-test";
    process.env.LANGFUSE_SECRET_KEY = "sk-langfuse-test";
    await expect(main(projectArgs(dir))).resolves.toBe(0);
  });
});

describe("typeflux-yaml-worker project mode adopts the environment overlay (#706 Bugbot)", () => {
  it("preflights green when the credential lives in the environment file, not the host env", async () => {
    const dir = writeProjectFixture();
    delete process.env.OPENAI_API_KEY; // the host shell does NOT carry it
    delete process.env.TYPEFLUX_YAML_PATH;
    process.env.TYPEFLUX_WORKER_BINDINGS = join(dir, "bindings.mjs");
    const code = await main([
      join(dir, "typeflux.project.yaml"),
      "--workflow",
      "review",
      "--environment",
      "prod",
      "--preflight",
    ]);
    expect(code).toBe(0);
    // The overlay persists for the worker's lifetime (the pod-ConfigMap analogue).
    expect(process.env.OPENAI_API_KEY).toBe("env-file-key");
  });
});

describe("typeflux-yaml-worker --preflight (#687 review — one options assembly with the live path)", () => {
  it("auto-wires the openai provider from OPENAI_API_KEY alone (schemas-only bindings, no provider code)", async () => {
    const dir = writeFixture();
    process.env["TYPEFLUX_YAML_PATH"] = join(dir, "typeflux.yaml");
    process.env["TYPEFLUX_WORKER_BINDINGS"] = join(dir, "bindings.mjs");
    process.env["OPENAI_API_KEY"] = "sk-test-preflight";
    await expect(main(["--preflight"])).resolves.toBe(0);
  });

  it("fails preflight — BEFORE the readiness marker — when the OOTB credential is missing", async () => {
    const dir = writeFixture();
    process.env["TYPEFLUX_YAML_PATH"] = join(dir, "typeflux.yaml");
    process.env["TYPEFLUX_WORKER_BINDINGS"] = join(dir, "bindings.mjs");
    delete process.env["OPENAI_API_KEY"];
    // The auto-wiring (providerTransportsFromSpec) throws its missing-credential error on the
    // PREFLIGHT path too — a supported OOTB deployment can never pass preflight and then
    // crash-loop on the same missing key at poll time.
    await expect(main(["--preflight"])).rejects.toThrow(/OPENAI_API_KEY/);
  });

  it("treats the bindings module as optional: absent, the failure is the precise schemas error", async () => {
    const dir = writeFixture();
    process.env["TYPEFLUX_YAML_PATH"] = join(dir, "typeflux.yaml");
    delete process.env["TYPEFLUX_WORKER_BINDINGS"];
    process.env["OPENAI_API_KEY"] = "sk-test-preflight";
    // Schemas are the one piece that MUST be code (the TS↔Python importlib divergence):
    // with no bindings the spec's schema refs fail by name, never silently.
    await expect(main(["--preflight"])).rejects.toThrow(/resolver\.schemas/);
  });
});
