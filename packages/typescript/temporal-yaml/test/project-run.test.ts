import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterAll, afterEach, describe, expect, it } from "vitest";

import { loadProjectBundle, planResolverFor, resolveProjectWorkflow } from "../src/index.js";

// The interpolated field under test is the workflow's `task_queue` (a plain string
// resolved through the full loader chain, unlike prompt bodies which are left verbatim).
const WORKFLOW = (queueExpr: string): string => `
project: p
name: n
task_queue: ${queueExpr}
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

const MANIFEST = `
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
environments:
  prod: prod.env.yaml
`;

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

// Every case sets a POISON value in the REAL process env; the hermetic assertions prove it
// never reaches interpolated output. Clean it up so cases don't cross-contaminate.
const POISON_KEYS = ["TF760_QUEUE", "TF760_MISSING"];
afterEach(() => {
  for (const key of POISON_KEYS) delete process.env[key];
});

function writeProject(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-run-"));
  createdDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

function setup(queueExpr: string, environmentYaml = "name: prod\n"): ReturnType<typeof loadProjectBundle> {
  const dir = writeProject({
    "typeflux.project.yaml": MANIFEST,
    "review.yaml": WORKFLOW(queueExpr),
    "prod.env.yaml": environmentYaml,
  });
  return loadProjectBundle(join(dir, "typeflux.project.yaml"));
}

describe("resolveProjectWorkflow env hook (#760)", () => {
  it("defaults to process.env when env is omitted (zero behavior change)", () => {
    process.env["TF760_QUEUE"] = "from-shell";
    const { project, sources } = setup("${TF760_QUEUE}");
    const resolved = resolveProjectWorkflow(project, sources, "review", "prod");
    expect(resolved?.spec.task_queue).toBe("from-shell");
  });

  it("interpolates against the injected env, not the operator's shell (hermetic poison)", () => {
    // Poison in the real process env must NOT reach the resolved bytes...
    process.env["TF760_QUEUE"] = "POISON";
    const { project, sources } = setup("${TF760_QUEUE}");
    // ...a var present ONLY in the injected env resolves.
    const resolved = resolveProjectWorkflow(project, sources, "review", "prod", { TF760_QUEUE: "hermetic" });
    expect(resolved?.spec.task_queue).toBe("hermetic");
  });

  it("does not fall back to process.env for a key absent from the injected env", () => {
    // Present in the shell, absent from the injected map, no default: must error exactly as
    // an unset shell variable would — no silent process.env fallback.
    process.env["TF760_MISSING"] = "POISON";
    const { project, sources } = setup("${TF760_MISSING}");
    expect(() => resolveProjectWorkflow(project, sources, "review", "prod", {})).toThrow(
      /missing environment variable: TF760_MISSING/,
    );
  });

  it("treats an injected empty-string value as SET, not missing", () => {
    // `${VAR:-fallback}` with VAR injected as "" must resolve to "" — an empty string is a
    // value, not an absent variable (the `x or default` trap).
    const { project, sources } = setup("${TF760_QUEUE:-fallback}");
    const resolved = resolveProjectWorkflow(project, sources, "review", "prod", { TF760_QUEUE: "" });
    expect(resolved?.spec.task_queue).toBe("");
  });

  it("layers the environment's variables overlay OVER the injected base (overlay wins)", () => {
    process.env["TF760_QUEUE"] = "POISON";
    const { project, sources } = setup("${TF760_QUEUE}", "name: prod\nvariables: { TF760_QUEUE: from-overlay }\n");
    const resolved = resolveProjectWorkflow(project, sources, "review", "prod", { TF760_QUEUE: "from-base" });
    expect(resolved?.spec.task_queue).toBe("from-overlay");
  });

  it("resolves a base-only key when no overlay shadows it", () => {
    const { project, sources } = setup("${TF760_QUEUE}", "name: prod\nvariables: { OTHER: x }\n");
    const resolved = resolveProjectWorkflow(project, sources, "review", "prod", { TF760_QUEUE: "from-base" });
    expect(resolved?.spec.task_queue).toBe("from-base");
  });
});

describe("planResolverFor env hook (#760)", () => {
  it("threads the injected env through the plan resolver", () => {
    process.env["TF760_QUEUE"] = "POISON";
    const { project, sources } = setup("${TF760_QUEUE}");
    const resolve = planResolverFor(project, sources, { TF760_QUEUE: "hermetic" });
    const resolved = resolve("review", "prod");
    expect(resolved?.spec.task_queue).toBe("hermetic");
  });

  it("defaults to process.env when env is omitted", () => {
    process.env["TF760_QUEUE"] = "from-shell";
    const { project, sources } = setup("${TF760_QUEUE}");
    const resolve = planResolverFor(project, sources);
    expect(resolve("review", "prod")?.spec.task_queue).toBe("from-shell");
  });

  it("threads the injected env into the SUB-WORKFLOW closure (hermetic children)", () => {
    // A hermetically-resolved parent's child must resolve under the SAME injected env —
    // the poison in the real process env must never reach a child's resolved bytes.
    process.env["TF760_QUEUE"] = "POISON";
    const dir = writeProject({
      "typeflux.project.yaml": `
version: "1"
name: acme
workflows:
  - { id: parent, path: parent.yaml }
  - { id: child, path: child.yaml }
environments:
  prod: prod.env.yaml
`,
      "parent.yaml": `
project: p
name: parent
task_queue: parent-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: []
workflow:
  name: Parent
  input: schemas:In
  output: schemas:Out
  steps:
    - id: run_child
      workflow: child
`,
      "child.yaml": WORKFLOW("${TF760_QUEUE}"),
      "prod.env.yaml": "name: prod\n",
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    const resolve = planResolverFor(project, sources, { TF760_QUEUE: "hermetic-child" });
    const resolved = resolve("parent", "prod");
    expect(resolved?.spec.task_queue).toBe("parent-queue");
    // The closure resolver the plan digest folds in resolves the child from the
    // injected env, not the operator's shell.
    expect(resolved?.subworkflows?.specFor("child")?.task_queue).toBe("hermetic-child");
  });
});
