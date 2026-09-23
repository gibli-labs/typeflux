/**
 * The subprocess resolver entry (#642): spawn the COMPILED dist entry and speak the contract's
 * newline-delimited JSON envelope at it, resolving the conformance TS project — the same wire
 * exchange the Python control plane's SubprocessResolver performs.
 *
 * Runs `pnpm -r build` output: dist/resolver-stdio.js must exist (CI builds before testing).
 */

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENTRY = resolve(HERE, "../dist/resolver-stdio.js");
const PROJECT_DIR = resolve(HERE, "../../../../contracts/controlplane/conformance/project/typescript");
const MANIFEST = resolve(PROJECT_DIR, "typeflux.project.yaml");

describe.skipIf(!existsSync(ENTRY))("resolver-stdio (#642)", () => {
  let child: ChildProcess;
  let buffered = "";
  const pending: Array<(line: string) => void> = [];

  const nextLine = (): Promise<string> =>
    new Promise((resolveLine) => {
      const newline = buffered.indexOf("\n");
      if (newline >= 0) {
        const line = buffered.slice(0, newline);
        buffered = buffered.slice(newline + 1);
        resolveLine(line);
        return;
      }
      pending.push(resolveLine);
    });

  const request = async (message: unknown): Promise<Record<string, unknown>> => {
    child.stdin!.write(JSON.stringify(message) + "\n");
    return JSON.parse(await nextLine()) as Record<string, unknown>;
  };

  beforeAll(async () => {
    child = spawn("node", [ENTRY, "--conformance-schemas"], { stdio: ["pipe", "pipe", "inherit"] });
    child.stdout!.setEncoding("utf-8");
    child.stdout!.on("data", (chunk: string) => {
      buffered += chunk;
      while (pending.length > 0) {
        const newline = buffered.indexOf("\n");
        if (newline < 0) break;
        const line = buffered.slice(0, newline);
        buffered = buffered.slice(newline + 1);
        pending.shift()!(line);
      }
    });
    const ready = JSON.parse(await nextLine()) as Record<string, unknown>;
    expect(ready).toEqual({ event: "ready", resolver_version: "1", runtime: "typescript" });
  }, 30_000);

  afterAll(() => {
    child.kill("SIGTERM");
  });

  it("validate_project resolves the conformance project over the wire (honest project-level issues)", async () => {
    const response = await request({
      operation: "validate_project",
      params: { manifest_path: MANIFEST, environment_id: "local", workflow_ids: ["workflow"] },
    });
    expect(response["ok"]).toBe(true);
    const result = response["result"] as {
      ok: boolean;
      project_name: string;
      issues: Array<{ code: string }>;
      resolved_workflows?: Array<{ workflow_id: string; ok: boolean }>;
    };
    // The conformance project DELIBERATELY declares `broken` (an undeclared profile
    // selection + a duplicate YAML name), so project-level ok is honestly false and
    // the duplicate-name issue BAILS resolved validation (Python parity, #566) —
    // the wire report carries exactly what the TS HTTP server would serve.
    expect(result.project_name).toBe("conformance-fixture");
    expect(result.ok).toBe(false);
    expect(result.issues.map((issue) => issue.code)).toContain("unknown_profile_reference");
    expect(result.issues.map((issue) => issue.code)).toContain("duplicate_workflow_name");
  });

  it("resolve_plan answers the raw plan + start identity", async () => {
    const response = await request({
      id: "req-1",
      operation: "resolve_plan",
      params: { manifest_path: MANIFEST, workflow_id: "workflow", environment_id: "local" },
    });
    expect(response["id"]).toBe("req-1"); // the id echoes for stream pairing
    expect(response["ok"]).toBe(true);
    const result = response["result"] as Record<string, unknown>;
    expect(result["task_queue"]).toBe("conformance-demo-queue");
    expect(typeof result["spec_digest"]).toBe("string");
    expect(result["workflow_name"]).toBe("ConformanceDemoWorkflow");
    // The plan is the raw WorkflowPlan the generic workflow interprets — opaque but structured.
    expect(typeof result["plan"]).toBe("object");
  });

  it("resolve_bundle works with the injected conformance schemas", async () => {
    const response = await request({
      operation: "resolve_bundle",
      params: { manifest_path: MANIFEST, workflow_id: "workflow", environment_id: "local" },
    });
    expect(response["ok"]).toBe(true);
    const result = response["result"] as { workflow: { workflow_type: string } };
    expect(result.workflow.workflow_type).toBe("typefluxYamlWorkflow");
  });

  it("an unknown workflow is the SAME ApiError + status the HTTP server would answer", async () => {
    const response = await request({
      operation: "resolve_catalog",
      params: { manifest_path: MANIFEST, workflow_id: "nope", environment_id: "local" },
    });
    expect(response["ok"]).toBe(false);
    expect(response["status"]).toBe(404);
    expect(response["error"]).toEqual({ error: "NotFound", message: "unknown project workflow: nope" });
  });

  it("an unknown operation and a garbled line answer structured 422s (the stream never dies)", async () => {
    const unknown = await request({ operation: "resolve_universe", params: { manifest_path: MANIFEST } });
    expect(unknown["ok"]).toBe(false);
    expect(unknown["status"]).toBe(422);

    child.stdin!.write("not json\n");
    const garbled = JSON.parse(await nextLine()) as Record<string, unknown>;
    expect(garbled["ok"]).toBe(false);
    expect((garbled["error"] as { error: string }).error).toBe("InvalidRequest");

    // The stream survives both: a normal request still answers.
    const after = await request({
      operation: "prompt_status",
      params: { manifest_path: MANIFEST, workflow_id: "workflow", environment_id: "local" },
    });
    expect(after["ok"]).toBe(true);
  });
});
