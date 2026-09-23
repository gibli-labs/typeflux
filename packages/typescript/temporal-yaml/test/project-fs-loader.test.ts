import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import { loadProjectBundle, validateProjectBundle } from "../src/index.js";

const WORKFLOW = `
project: p
name: n
task_queue: base-queue
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
const STRICT = "name: strict\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";
const PROD_ENV = "name: prod\noverrides: { task_queue: prod-queue }\n";
const MANIFEST = (workflowLine: string) => `
version: "1"
name: acme
workflows:
  - ${workflowLine}
policies:
  strict: strict.policy.yaml
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [strict] }
`;

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

/** Write a set of `relativePath → content` files into a fresh temp project dir; return the dir. */
function writeProject(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-project-"));
  createdDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

describe("loadProjectBundle (#454 — filesystem → injected sources)", () => {
  it("reads the manifest + every referenced spec file into sources, usable end-to-end", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, path: review.yaml }"),
      "review.yaml": WORKFLOW,
      "strict.policy.yaml": STRICT,
      "prod.env.yaml": PROD_ENV,
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(project.name).toBe("acme");
    expect(Object.keys(sources.policies)).toEqual(["strict"]);
    expect(Object.keys(sources.environments)).toEqual(["prod"]);
    expect(sources.workflows["review"]).toContain("provider: { type: openai");
    // End-to-end: the loaded bundle validates clean.
    const report = validateProjectBundle(project, sources, { environmentId: "prod" });
    expect(report.ok).toBe(true);
    expect(report.resolvedWorkflows[0]?.taskQueue).toBe("prod-queue");
  });

  it("resolves a `directory` workflow via defaults.workflow_filename", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, directory: workflows/review }"),
      "workflows/review/typeflux.yaml": WORKFLOW, // the default workflow filename
      "strict.policy.yaml": STRICT,
      "prod.env.yaml": PROD_ENV,
    });
    const { sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.workflows["review"]).toContain("name: W");
  });

  it("omits an absent policy file so the validator reports missing_policy_source", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, path: review.yaml }"),
      "review.yaml": WORKFLOW,
      "prod.env.yaml": PROD_ENV,
      // strict.policy.yaml intentionally NOT written
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.policies).toEqual({}); // the missing policy file is omitted, not thrown
    const report = validateProjectBundle(project, sources, { environmentId: "prod" });
    expect(report.issues.some((issue) => issue.code === "missing_policy_source")).toBe(true);
  });

  it("omits an absent environment file so the validator reports missing_environment_source", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, path: review.yaml }"),
      "review.yaml": WORKFLOW,
      "strict.policy.yaml": STRICT,
      // prod.env.yaml intentionally NOT written
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.environments).toEqual({});
    const report = validateProjectBundle(project, sources);
    expect(report.issues.some((issue) => issue.code === "missing_environment_source")).toBe(true);
  });

  it("stores a malformed workflow file raw (no eager parse) → a reference-level parse issue, not a loader throw", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, path: review.yaml }"),
      "review.yaml": "::: not valid yaml :::",
      "strict.policy.yaml": STRICT,
      "prod.env.yaml": PROD_ENV,
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.workflows["review"]).toBe("::: not valid yaml :::"); // stored raw, loader did not throw
    // The validator parses at the reference level now (#565): invalid_workflow_yaml, and
    // resolution bails (Python parity) instead of double-reporting a resolution failure.
    const report = validateProjectBundle(project, sources, { environmentId: "prod" });
    expect(report.issues.some((issue) => issue.code === "invalid_workflow_yaml")).toBe(true);
    expect(report.resolvedWorkflows).toEqual([]);
  });

  it("treats a reference that points at a directory as a missing file (EISDIR)", () => {
    const dir = writeProject({
      "typeflux.project.yaml": `
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
policies:
  strict: policydir
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [strict] }
`,
      "review.yaml": WORKFLOW,
      "policydir/keep.txt": "", // makes `policydir` a directory → reading it as a file yields EISDIR
      "prod.env.yaml": PROD_ENV,
    });
    const { sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.policies).toEqual({}); // omitted, not thrown
  });

  it("treats a reference routed through a non-directory as a missing file (ENOTDIR; codex)", () => {
    const dir = writeProject({
      "typeflux.project.yaml": `
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
policies:
  strict: blocker/strict.yaml
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [strict] }
`,
      "review.yaml": WORKFLOW,
      blocker: "i am a regular file, not a directory", // so `blocker/strict.yaml` -> ENOTDIR
      "prod.env.yaml": PROD_ENV,
    });
    // Must NOT crash — the unreadable reference is omitted and surfaced by the validator.
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.policies).toEqual({});
    const report = validateProjectBundle(project, sources, { environmentId: "prod" });
    expect(report.issues.some((issue) => issue.code === "missing_policy_source")).toBe(true);
  });

  it("throws when the manifest itself does not exist", () => {
    const dir = writeProject({});
    expect(() => loadProjectBundle(join(dir, "typeflux.project.yaml"))).toThrow(/project manifest not found/);
  });

  it("propagates a parse error for a declared spec file that exists but is malformed", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, path: review.yaml }"),
      "review.yaml": WORKFLOW,
      "strict.policy.yaml": "name: strict\nproviders: ::: not valid :::\n",
      "prod.env.yaml": PROD_ENV,
    });
    expect(() => loadProjectBundle(join(dir, "typeflux.project.yaml"))).toThrow(/policy/i);
  });

  it("accepts an absolute manifest path and resolves references from its directory", () => {
    const dir = writeProject({
      "typeflux.project.yaml": MANIFEST("{ id: review, path: nested/review.yaml }"),
      "nested/review.yaml": WORKFLOW,
      "strict.policy.yaml": STRICT,
      "prod.env.yaml": PROD_ENV,
    });
    const { sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(sources.workflows["review"]).toContain("name: W");
  });
});
