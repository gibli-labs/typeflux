import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { loadProjectBundle } from "@typeflux/temporal-yaml";
import { afterAll, describe, expect, it } from "vitest";

import { annotationsPath, loadProjectAnnotations, readProjectAnnotations } from "../src/annotations.js";
import { ProjectControlPlane } from "../src/project-control-plane.js";

const MANIFEST = `
version: "1"
name: annotations-demo
workflows:
  - { id: review, path: review.yaml }
`;
const WORKFLOW = `
project: p
name: n
task_queue: wf-queue
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

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

/** A project on disk with an optional `.typeflux/annotations.yaml`. Returns the manifest path. */
function project(annotations?: string): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-cp-annot-"));
  createdDirs.push(dir);
  const files: Record<string, string> = {
    "typeflux.project.yaml": MANIFEST,
    "review.yaml": WORKFLOW,
  };
  if (annotations !== undefined) files[".typeflux/annotations.yaml"] = annotations;
  for (const [relative, content] of Object.entries(files)) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return join(dir, "typeflux.project.yaml");
}

describe("insight annotations projection (#733)", () => {
  it("resolves the file under .typeflux/ beside the manifest", () => {
    const manifest = project();
    expect(annotationsPath(manifest)).toBe(join(dirname(manifest), ".typeflux", "annotations.yaml"));
  });

  it("parses valid entries in file order, with optional fields and an expired-but-served ack", () => {
    const manifest = project(
      [
        "annotations:",
        '  - insight_id_pattern: "policy.drift.*"',
        "    reason: tracked upstream",
        '    tracked_in: "https://github.com/acme/infra/issues/412"',
        "  - insight_id_pattern: runtime.pin.exact",
        "    reason: accepted for the migration window",
        "    expires: 2026-01-01",
        "",
      ].join("\n"),
    );
    const projection = loadProjectAnnotations(manifest);
    expect(projection.annotations.map((a) => a.insight_id_pattern)).toEqual([
      "policy.drift.*",
      "runtime.pin.exact",
    ]);
    // Absent optionals are OMITTED (parity with Python exclude_none), not undefined-valued.
    expect(projection.annotations[0]).toEqual({
      insight_id_pattern: "policy.drift.*",
      reason: "tracked upstream",
      tracked_in: "https://github.com/acme/infra/issues/412",
    });
    // An expired ack is SERVED with its expiry (rendering is the console's job, slice 3).
    expect(projection.annotations[1]).toEqual({
      insight_id_pattern: "runtime.pin.exact",
      reason: "accepted for the migration window",
      expires: "2026-01-01",
    });
  });

  it("absent file is an empty projection, not an error", () => {
    const result = readProjectAnnotations(project());
    expect(result.annotations.annotations).toEqual([]);
    expect(result.error).toBeUndefined();
  });

  it("explicit null optionals are treated as absent (Python pydantic parity)", () => {
    // A YAML author writing `tracked_in:` (or an explicit null) must not invalidate the
    // file on this edition when Python serves it fine (codex parity finding).
    const result = readProjectAnnotations(
      project(
        "annotations:\n  - insight_id_pattern: drift-*\n    reason: known drift\n    tracked_in: null\n    expires:\n",
      ),
    );
    expect(result.error).toBeUndefined();
    expect(result.annotations.annotations).toHaveLength(1);
    const entry = result.annotations.annotations[0]!;
    expect(entry.tracked_in).toBeUndefined();
    expect(entry.expires).toBeUndefined();
  });

  it("YAML merge keys expand (strict_safe_load / temporal-yaml loader parity)", () => {
    // The anchor lives on the first LIST entry (a top-level anchors block would be an unknown
    // key — extra-forbid in BOTH editions); the second entry `<<`-merges its shared fields.
    const result = readProjectAnnotations(
      project(
        "annotations:\n  - &base\n    insight_id_pattern: drift-a\n    reason: known drift, tracked\n    tracked_in: https://github.com/acme/flows/issues/1\n  - <<: *base\n    insight_id_pattern: drift-b\n",
      ),
    );
    expect(result.error).toBeUndefined();
    expect(result.annotations.annotations.map((entry) => entry.insight_id_pattern)).toEqual([
      "drift-a",
      "drift-b",
    ]);
    expect(result.annotations.annotations[0]!.tracked_in).toBe(
      "https://github.com/acme/flows/issues/1",
    );
  });

  it("empty / comments-only file is empty, not malformed", () => {
    const result = readProjectAnnotations(project("# nothing acknowledged yet\n"));
    expect(result.annotations.annotations).toEqual([]);
    expect(result.error).toBeUndefined();
  });

  it("an empty annotations list is valid", () => {
    const result = readProjectAnnotations(project("annotations: []\n"));
    expect(result.annotations.annotations).toEqual([]);
    expect(result.error).toBeUndefined();
  });

  it.each([
    ["unparseable YAML", "annotations: [oops\n"],
    ["a bare list, not the mapping schema", "- insight_id_pattern: x\n  reason: y\n"],
    ["an unknown top-level key", "annotation:\n  - insight_id_pattern: x\n    reason: y\n"],
    ["an unknown entry key", "annotations:\n  - insight_id_pattern: x\n    reason: y\n    note: nope\n"],
    ["a missing reason", "annotations:\n  - insight_id_pattern: x\n"],
    ["an empty pattern", 'annotations:\n  - insight_id_pattern: "  "\n    reason: y\n'],
    ["a non-URL tracked_in", "annotations:\n  - insight_id_pattern: x\n    reason: y\n    tracked_in: not-a-url\n"],
    ["a non-date expires", "annotations:\n  - insight_id_pattern: x\n    reason: y\n    expires: someday\n"],
  ])("malformed file (%s) is an empty projection AND a parse error", (_label, body) => {
    const result = readProjectAnnotations(project(body));
    expect(result.annotations.annotations).toEqual([]); // fail-closed, never a partial parse
    expect(result.error).toBeDefined();
  });

  it("a malformed file surfaces as a validation issue on validate(), forcing ok:false", () => {
    const manifest = project("annotation:\n  - insight_id_pattern: x\n    reason: y\n");
    const cp = new ProjectControlPlane(loadProjectBundle(manifest), { manifestPath: manifest });
    const report = cp.validate();
    const issues = report.issues.filter((i) => i.code === "invalid_annotations_file");
    expect(issues).toHaveLength(1);
    expect(report.ok).toBe(false);
    expect(issues[0]!.reference).toBe("annotations.yaml");
    expect(issues[0]!.path).toBe(annotationsPath(manifest));
    expect(issues[0]!.message).toContain("annotations 'annotations.yaml' failed to load:");
  });

  it("a valid file adds no validation issue", () => {
    const manifest = project("annotations:\n  - insight_id_pattern: x\n    reason: y\n");
    const cp = new ProjectControlPlane(loadProjectBundle(manifest), { manifestPath: manifest });
    const report = cp.validate();
    expect(report.issues.some((i) => i.code === "invalid_annotations_file")).toBe(false);
    expect(cp.annotations().annotations).toEqual([{ insight_id_pattern: "x", reason: "y" }]);
  });
});
