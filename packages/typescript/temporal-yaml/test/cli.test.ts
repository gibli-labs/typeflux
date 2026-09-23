import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { parseAllDocuments } from "yaml";
import { afterAll, describe, expect, it } from "vitest";

import { main, type CliIo } from "../src/cli.js";

/** The all-zeros placeholder digest image (#757 item 5). */
const PLACEHOLDER_IMAGE = `registry.example/worker@sha256:${"0".repeat(64)}`;

const DIGEST = "@sha256:" + "c".repeat(64);
const IMAGE = `registry.example/worker:1${DIGEST}`;

const WORKFLOW = (extraStep = false) => `
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
  steps: [{ id: s, activity: a }${extraStep ? ", { id: s2, activity: a }" : ""}]
`;
const MANIFEST = `
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
policies:
  only_mini: only_mini.policy.yaml
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [only_mini] }
`;
const POLICY = "name: only_mini\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

function writeProject(workflow = WORKFLOW()): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-cli-"));
  createdDirs.push(dir);
  const files: Record<string, string> = {
    "typeflux.project.yaml": MANIFEST,
    "review.yaml": workflow,
    "only_mini.policy.yaml": POLICY,
    "prod.env.yaml": "name: prod\n",
  };
  for (const [relative, content] of Object.entries(files)) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

/** A capturing IO seam so the CLI is driven end-to-end in-process. */
function capture(): { io: CliIo; out: string[]; err: string[] } {
  const out: string[] = [];
  const err: string[] = [];
  return { io: { out: (line) => out.push(line), err: (line) => err.push(line) }, out, err };
}

describe("typeflux-project deploy (#687 D687-2)", () => {
  it("writes a plan with --plan-out, verifies it with --plan, and fails closed on drift", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");

    // 1. --plan-out writes an immutable plan file under the named directory.
    const planOut = capture();
    expect(
      await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--plan-out", "deployments", "--json"], planOut.io),
    ).toBe(0);
    const written = readdirSync(join(dir, "deployments")).filter((name) => name.endsWith(".yaml"));
    expect(written).toHaveLength(1);
    const planRel = `deployments/${written[0]}`;

    // 2. --apply applies the approved plan: verifies it against the current resolution: clean.
    const verify = capture();
    expect(await main(["deploy", manifest, "--apply", planRel, "--json"], verify.io)).toBe(0);
    expect(verify.err).toEqual([]);

    // 3. Drift: edit the spec, then the same --apply promote fails closed naming the digest.
    writeFileSync(join(dir, "review.yaml"), WORKFLOW(true));
    const drift = capture();
    expect(await main(["deploy", manifest, "--apply", planRel], drift.io)).toBe(3); // drift verdict (#818)
    expect(drift.err.join("\n")).toMatch(/drifted from current resolution/);
    expect(drift.err.join("\n")).toMatch(/identity\.spec_digest/);
  });

  it("requires --environment and --image without --apply", async () => {
    const dir = writeProject();
    const cap = capture();
    expect(await main(["deploy", join(dir, "typeflux.project.yaml"), "--workflow", "review"], cap.io)).toBe(2);
    expect(cap.err.join("\n")).toMatch(/requires --environment and --image/);
  });

  it("wires --project-path-in-image through into the plan, worker command, and rendered annotations", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const inImage = "/srv/app/typeflux.project.yaml";
    const cap = capture();
    expect(
      await main(
        ["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--project-path-in-image", inImage, "--output", "out", "--json"],
        cap.io,
      ),
    ).toBe(0);
    const plan = JSON.parse(cap.out.join("\n"));
    expect(plan.project_path_in_image).toBe(inImage);
    expect(plan.workers[0].project_path_in_image).toBe(inImage);
    // The rendered worker command targets the overridden in-image path.
    expect(plan.workers[0].command.join(" ")).toContain(inImage);
    // The rendered manifest carries the in-image path annotation.
    expect(readFileSync(join(dir, "out", "kubernetes.yaml"), "utf-8")).toContain(inImage);
  });

  it("rejects a non-absolute --project-path-in-image", async () => {
    const dir = writeProject();
    const cap = capture();
    expect(
      await main(
        ["deploy", join(dir, "typeflux.project.yaml"), "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--project-path-in-image", "srv/app"],
        cap.io,
      ),
    ).toBe(1);
    expect(cap.err.join("\n")).toMatch(/must be an absolute image path/);
  });

  it("--apply promote honors the approved plan's mutable image without re-passing the escape flag (#704)", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const mutableImage = "registry.example/worker:latest";

    // Write an approved plan with a mutable image (escape flag at plan time).
    const planOut = capture();
    expect(
      await main(
        ["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", mutableImage, "--allow-mutable-image", "--plan-out", "deployments", "--json"],
        planOut.io,
      ),
    ).toBe(0);
    const written = readdirSync(join(dir, "deployments")).filter((name) => name.endsWith(".yaml"));
    expect(written).toHaveLength(1);

    // Promote WITHOUT --allow-mutable-image: the plan file is authoritative for mutability.
    const promote = capture();
    expect(await main(["deploy", manifest, "--apply", `deployments/${written[0]}`, "--json"], promote.io)).toBe(0);
    expect(promote.err).toEqual([]);
  });

  it("renders the four Kubernetes artifacts with --output and reports their digests", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const cap = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--output", "out"], cap.io)).toBe(0);

    const outDir = join(dir, "out");
    expect(readdirSync(outDir).sort()).toEqual([
      "deployment-plan.json",
      "kubernetes.yaml",
      "secret.scaffold.yaml",
      "secrets.env.example",
    ]);
    expect(cap.out.join("\n")).toMatch(/Wrote deployment artifacts to/);
    expect(cap.out.join("\n")).toMatch(/kubernetes:.*sha256=[0-9a-f]{64}/);

    // The manifest parses and carries the probe marker, non-root context, and standard labels.
    const docs = parseAllDocuments(readFileSync(join(outDir, "kubernetes.yaml"), "utf-8")).map((doc) => doc.toJS());
    const deployment = docs.find((doc) => doc.kind === "Deployment");
    expect(deployment).toBeDefined();
    const container = deployment.spec.template.spec.containers[0];
    expect(container.startupProbe.exec.command).toEqual(["sh", "-c", "test -f /tmp/typeflux-preflight-ok"]);
    expect(container.securityContext.readOnlyRootFilesystem).toBe(true);
    expect(deployment.spec.template.spec.securityContext.runAsNonRoot).toBe(true);
    expect(deployment.metadata.labels["app.kubernetes.io/part-of"]).toBe("acme");
    // The container command runs preflight, writes the marker, then execs the worker.
    expect(container.command[2]).toMatch(/rm -f \/tmp\/typeflux-preflight-ok/);
    expect(container.command[2]).toMatch(/--preflight/);
    expect(container.command[2]).toMatch(/touch \/tmp\/typeflux-preflight-ok/);
  });

  it("rejects an unknown command and unknown flags", async () => {
    expect(await main(["frobnicate"], capture().io)).toBe(2);
    const cap = capture();
    expect(await main(["deploy", "m.yaml", "--wat"], cap.io)).toBe(2);
    expect(cap.err.join("\n")).toMatch(/unknown flag: --wat/);
  });

  it("records a portable (manifest-relative) project_manifest_path by default; absolute only with the flag (#757 item 1)", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");

    // Default: the manifest's basename — machine- and cwd-independent, no operator path leaked.
    const def = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--output", "out", "--json"], def.io)).toBe(0);
    expect(JSON.parse(def.out.join("\n")).project_manifest_path).toBe("typeflux.project.yaml");
    const yaml = readFileSync(join(dir, "out", "kubernetes.yaml"), "utf-8");
    expect(yaml).toContain("typeflux.io/project-manifest-path: typeflux.project.yaml");
    // The operator's absolute temp path never leaks into committed artifacts.
    expect(yaml).not.toContain(dir);

    // Explicit override recorded verbatim (the consumer's repo-relative value).
    const explicit = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--project-manifest-path", "config/typeflux.project.yaml", "--json"], explicit.io)).toBe(0);
    expect(JSON.parse(explicit.out.join("\n")).project_manifest_path).toBe("config/typeflux.project.yaml");

    // --absolute-manifest-path restores the operator's resolved absolute path.
    const abs = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", IMAGE, "--absolute-manifest-path", "--json"], abs.io)).toBe(0);
    expect(JSON.parse(abs.out.join("\n")).project_manifest_path).toBe(manifest);
  });

  it("rejects the all-zeros placeholder image at build, plan-write, and promote unless --allow-placeholder-image (#757 item 5)", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");

    // Build/render path refuses it, naming the digest and the flag.
    const build = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", PLACEHOLDER_IMAGE, "--json"], build.io)).toBe(1);
    expect(build.err.join("\n")).toMatch(/all-zeros placeholder digest/);
    expect(build.err.join("\n")).toMatch(/--allow-placeholder-image/);

    // Plan-write refuses it too.
    const write = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", PLACEHOLDER_IMAGE, "--plan-out", "deployments"], write.io)).toBe(1);
    expect(write.err.join("\n")).toMatch(/all-zeros placeholder digest/);

    // With the flag, a placeholder plan writes…
    const writeOk = capture();
    expect(await main(["deploy", manifest, "--environment", "prod", "--workflow", "review", "--image", PLACEHOLDER_IMAGE, "--allow-placeholder-image", "--plan-out", "deployments", "--json"], writeOk.io)).toBe(0);
    const written = readdirSync(join(dir, "deployments")).filter((name) => name.endsWith(".yaml"));
    expect(written).toHaveLength(1);

    // …but promoting it WITHOUT the flag fails closed at verify time (would only die at pod scheduling).
    const promote = capture();
    expect(await main(["deploy", manifest, "--apply", `deployments/${written[0]}`], promote.io)).toBe(3); // drift verdict (#818)
    const promoteErr = promote.err.join("\n");
    // The synthetic check code renders as an explanation (image literal + prose), not a value diff.
    expect(promoteErr).toMatch(/deployment\.image_placeholder: registry\.example\/worker@sha256:0{64}: the all-zeros placeholder digest/);
    expect(promoteErr).not.toMatch(/deployment\.image_placeholder: plan=/);

    // Promote WITH the flag passes.
    const promoteOk = capture();
    expect(await main(["deploy", manifest, "--apply", `deployments/${written[0]}`, "--allow-placeholder-image", "--json"], promoteOk.io)).toBe(0);
    expect(promoteOk.err).toEqual([]);
  });

  it("fires through an npm .bin-style symlink instead of silently no-opping (#757 item 4)", async () => {
    // The built bin (dist/cli.js), invoked via a SYMLINK — the exact node_modules/.bin shape that
    // used to make argv[1] (the link) mismatch import.meta.url (the realpath) and no-op with exit 0.
    const cliReal = fileURLToPath(new URL("../dist/cli.js", import.meta.url));
    const linkDir = mkdtempSync(join(tmpdir(), "tf-bin-"));
    createdDirs.push(linkDir);
    const link = join(linkDir, "typeflux-project");
    symlinkSync(cliReal, link);

    let status = 0;
    let stderr = "";
    try {
      execFileSync(process.execPath, [link], { stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      status = (error as { status?: number }).status ?? 0;
      stderr = String((error as { stderr?: Buffer }).stderr ?? "");
    }
    // The guard now fires: no command → usage + exit 2 (NOT a silent exit-0 no-op).
    expect(status).toBe(2);
    expect(stderr).toMatch(/usage: typeflux-project/);
  });
});

describe("typeflux-project erase (#715 slice 5)", () => {
  const ERASE_BASE = (manifest: string) => [
    "erase",
    manifest,
    "--workflow",
    "review",
    "--environment",
    "prod",
    "--subject",
    "subject-0001",
  ];

  it("requires --acknowledge-irreversible to execute the temporal surface", async () => {
    const cap = capture();
    // The gate fires before any project/manifest IO — a nonexistent path is fine.
    const code = await main([...ERASE_BASE("m.yaml"), "--execute"], cap.io);
    expect(code).toBe(2);
    expect(cap.err.join("\n")).toContain("IRREVERSIBLE");
    expect(cap.err.join("\n")).toContain("--acknowledge-irreversible");
  });

  it("rejects unknown surfaces, bad timestamps, and missing args as usage errors", async () => {
    const surfaces = capture();
    expect(await main([...ERASE_BASE("m.yaml"), "--surface", "provider_logs"], surfaces.io)).toBe(2);
    expect(surfaces.err.join("\n")).toContain("unknown surface");

    const since = capture();
    expect(await main([...ERASE_BASE("m.yaml"), "--since", "not-a-date"], since.io)).toBe(2);
    expect(since.err.join("\n")).toContain("ISO-8601");

    const noSubject = capture();
    expect(
      await main(["erase", "m.yaml", "--workflow", "review", "--environment", "prod"], noSubject.io),
    ).toBe(2);
    expect(noSubject.err.join("\n")).toContain("--subject");

    const both = capture();
    expect(await main([...ERASE_BASE("m.yaml"), "--dry-run", "--execute"], both.io)).toBe(2);
    expect(both.err.join("\n")).toContain("mutually exclusive");
  });

  it("runs a cache-only dry run through a real project with a bindings module", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const bindings = join(dir, "erase-bindings.mjs");
    writeFileSync(
      bindings,
      `export const cacheStore = {
        get: () => undefined,
        set: () => undefined,
        eraseSubject: (subjectId, { dryRun }) => ({
          subjectId, dryRun, storeClass: "FixtureStore", supported: true,
          keysFound: 2, keysDeleted: 0, keyDigests: ["d1", "d2"], failures: [], warnings: [],
        }),
      };\n`,
    );
    const cap = capture();
    const code = await main(
      [...ERASE_BASE(manifest), "--surface", "cache", "--bindings", bindings, "--actor", "compliance-ops", "--json"],
      cap.io,
    );
    expect(cap.err).toEqual([]);
    expect(code).toBe(0);
    const receipt = JSON.parse(cap.out.join("\n")) as {
      dryRun: boolean;
      actor: string;
      subjectIds: string[];
      surfaces: Record<string, { status: string; skipReason?: string; reports?: Array<{ keysFound: number }> }>;
      unreachable: Array<{ surface: string }>;
    };
    expect(receipt.dryRun).toBe(true);
    expect(receipt.actor).toBe("compliance-ops");
    expect(receipt.subjectIds).toEqual(["subject-0001"]);
    // Deselected surfaces are loudly skipped, never omitted.
    expect(receipt.surfaces["temporal"]!.status).toBe("skipped");
    expect(receipt.surfaces["langfuse"]!.status).toBe("skipped");
    expect(receipt.surfaces["cache"]!.status).toBe("ok");
    expect(receipt.surfaces["cache"]!.reports![0]!.keysFound).toBe(2);
    expect(receipt.unreachable.map((note) => note.surface)).toEqual([
      "provider_logs",
      "exported_artifacts",
      "mixed_workflow_payloads",
    ]);
  });

  it("cache-only execute needs no acknowledgment and reports the performed outcome", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const bindings = join(dir, "erase-exec-bindings.mjs");
    writeFileSync(
      bindings,
      `export const cacheStore = {
        get: () => undefined,
        set: () => undefined,
        eraseSubject: (subjectId, { dryRun }) => ({
          subjectId, dryRun, storeClass: "FixtureStore", supported: true,
          keysFound: 2, keysDeleted: dryRun ? 0 : 2, keyDigests: ["d1", "d2"], failures: [], warnings: [],
        }),
      };\n`,
    );
    const cap = capture();
    const code = await main(
      [...ERASE_BASE(manifest), "--surface", "cache", "--bindings", bindings, "--execute", "--json"],
      cap.io,
    );
    expect(cap.err).toEqual([]);
    expect(code).toBe(0);
    const receipt = JSON.parse(cap.out.join("\n")) as {
      dryRun: boolean;
      surfaces: { cache: { reports: Array<{ keysDeleted: number }> } };
    };
    expect(receipt.dryRun).toBe(false);
    expect(receipt.surfaces.cache.reports[0]!.keysDeleted).toBe(2);
  });

  it("honors a default-export bindings module and applies+restores the env overlay", async () => {
    // #715 fix round items 2+3: worker-entry accepts `export default { ... }` bindings,
    // so erase must too (a shape divergence would report a surface "skipped" while the
    // deployed worker uses that backend — an under-erasure); and the environment's
    // variable overlay must be visible DURING the run and restored AFTER it (no
    // cross-invocation contamination for in-process callers).
    delete process.env["ERASE_TS_MARKER"];
    const dir = writeProject();
    writeFileSync(join(dir, "prod.env.yaml"), "name: prod\nvariables:\n  ERASE_TS_MARKER: from-overlay\n");
    const manifest = join(dir, "typeflux.project.yaml");
    const bindings = join(dir, "default-bindings.mjs");
    writeFileSync(
      bindings,
      `export default {
        cacheStore: {
          get: () => undefined,
          set: () => undefined,
          eraseSubject: (subjectId, { dryRun }) => ({
            subjectId, dryRun, storeClass: "OverlayProbeStore", supported: true,
            keysFound: 0, keysDeleted: 0, keyDigests: [], failures: [],
            warnings: ["marker=" + (process.env.ERASE_TS_MARKER ?? "unset")],
          }),
        },
      };\n`,
    );
    const cap = capture();
    const code = await main(
      [...ERASE_BASE(manifest), "--surface", "cache", "--bindings", bindings, "--json"],
      cap.io,
    );
    expect(cap.err).toEqual([]);
    expect(code).toBe(0);
    const receipt = JSON.parse(cap.out.join("\n")) as {
      surfaces: { cache: { status: string; reports: Array<{ storeClass: string; warnings: string[] }> } };
    };
    // Default export honored — the surface ran, it was not "skipped".
    expect(receipt.surfaces.cache.status).toBe("ok");
    expect(receipt.surfaces.cache.reports[0]!.storeClass).toBe("OverlayProbeStore");
    // The overlay was live while the backend ran...
    expect(receipt.surfaces.cache.reports[0]!.warnings).toContain("marker=from-overlay");
    // ...and is restored afterwards (no leak into the ambient process env).
    expect(process.env["ERASE_TS_MARKER"]).toBeUndefined();
  });

  it("refuses a bindings module whose subjectKeystore is not a SubjectKeystore", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const bindings = join(dir, "bad-bindings.mjs");
    writeFileSync(bindings, "export const subjectKeystore = { notAKeystore: true };\n");
    const cap = capture();
    const code = await main(
      [...ERASE_BASE(manifest), "--surface", "cache", "--bindings", bindings],
      cap.io,
    );
    expect(code).toBe(1);
    expect(cap.err.join("\n")).toContain("SubjectKeystore");
  });

  it("whitespace-only Langfuse credentials report the surface skipped (#715 Bugbot)", async () => {
    const priorPublic = process.env["LANGFUSE_PUBLIC_KEY"];
    const priorSecret = process.env["LANGFUSE_SECRET_KEY"];
    process.env["LANGFUSE_PUBLIC_KEY"] = "   ";
    process.env["LANGFUSE_SECRET_KEY"] = "\t";
    try {
      const dir = writeProject(WORKFLOW().replace(
        "  provider: { type: openai, model: gpt-4o-mini }",
        "  provider: { type: openai, model: gpt-4o-mini }\n  observability: { type: langfuse }",
      ));
      const manifest = join(dir, "typeflux.project.yaml");
      const cap = capture();
      const code = await main([...ERASE_BASE(manifest), "--surface", "langfuse", "--json"], cap.io);
      expect(cap.err).toEqual([]);
      expect(code).toBe(0);
      const receipt = JSON.parse(cap.out.join("\n")) as {
        surfaces: { langfuse: { status: string; skipReason?: string } };
      };
      // Whitespace keys are NOT configured: honestly skipped-with-reason, never a
      // client that fails at runtime.
      expect(receipt.surfaces.langfuse.status).toBe("skipped");
      expect(receipt.surfaces.langfuse.skipReason).toContain("LANGFUSE_PUBLIC_KEY");
    } finally {
      if (priorPublic === undefined) delete process.env["LANGFUSE_PUBLIC_KEY"];
      else process.env["LANGFUSE_PUBLIC_KEY"] = priorPublic;
      if (priorSecret === undefined) delete process.env["LANGFUSE_SECRET_KEY"];
      else process.env["LANGFUSE_SECRET_KEY"] = priorSecret;
    }
  });

  it("rejects a non-positive --limit as a usage error", async () => {
    const zero = capture();
    expect(await main([...ERASE_BASE("m.yaml"), "--limit", "0"], zero.io)).toBe(2);
    expect(zero.err.join("\n")).toContain("--limit must be a positive integer");
    const negative = capture();
    expect(await main([...ERASE_BASE("m.yaml"), "--limit", "-1"], negative.io)).toBe(2);
    expect(negative.err.join("\n")).toContain("--limit must be a positive integer");
  });

  it("human output names the receipt as the proof and lists unreachable surfaces", async () => {
    const dir = writeProject();
    const manifest = join(dir, "typeflux.project.yaml");
    const cap = capture();
    const code = await main([...ERASE_BASE(manifest), "--surface", "cache"], cap.io);
    // No cache store injected: honestly skipped, still exit 0 (skips are not failures).
    expect(code).toBe(0);
    const output = cap.out.join("\n");
    expect(output).toContain("DRY RUN");
    expect(output).toContain("proof of erasure");
    expect(output).toContain("Unreachable surfaces");
    expect(output).toContain("no cache store");
  });
});
