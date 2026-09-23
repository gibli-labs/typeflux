/**
 * GATED live operate-tier end-to-end (#326 Phase 1). Drives the REAL managed-local MCP server —
 * which spins the in-process TS control plane — against a REAL Temporal dev server and a REAL TS
 * worker, exercising the operate tools over an in-memory MCP transport exactly as an editor agent
 * would. Skipped unless TYPEFLUX_LIVE_TEMPORAL=1 (there is no Temporal server in CI); requires a
 * local dev server on localhost:7233 and `pnpm -r --filter ./packages/typescript/** build`.
 *
 * What it proves (every op is a tool call through the MCP client → managed-local CP → Temporal):
 *   1. start_workflow → a real WorkflowStartReceipt (workflow_type typefluxYamlWorkflow).
 *   2. get_status polled until state=waiting_for_review (the review gate opened).
 *   3. submit_review(approve) → the run reaches state=completed.
 *   4. a second execution: cancel_workflow(reason) → status shows cancellation_requested=true.
 *   5. a status resource subscription emits notifications/resources/updated as a run progresses.
 *
 * The worker (fake deterministic provider, no LLM key) runs in a spawned subprocess; the MCP server
 * + CP run in THIS process. The conformance schemas are injected so start validates input for real.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ResourceUpdatedNotificationSchema } from "@modelcontextprotocol/sdk/types.js";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { CONFORMANCE_SCHEMAS } from "@tf/conformance-schemas";

import { LazyBackend } from "../src/backend.js";
import { loadConfig } from "../src/config.js";
import { createTypefluxMcpServer } from "../src/server.js";

const MANIFEST = fileURLToPath(
  new URL("../../../contracts/controlplane/conformance/project/typescript/typeflux.project.yaml", import.meta.url),
);
const WORKFLOW_YAML = fileURLToPath(
  new URL("../../../contracts/controlplane/conformance/project/typescript/workflow.yaml", import.meta.url),
);
const CP_DIST = fileURLToPath(new URL("../../../packages/typescript/temporal-controlplane/dist/index.js", import.meta.url));
const WORKER_HARNESS = fileURLToPath(
  new URL("../../../packages/typescript/temporal-yaml/scripts/live-operate-worker.mjs", import.meta.url),
);

const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1" && existsSync(CP_DIST);

interface StatusSnapshot {
  status: { state: string; cancellation_requested?: boolean };
  valid_user_decisions: Record<string, string>;
}

describe.skipIf(!LIVE)("operate tier — live end-to-end through the MCP tools (#326 Phase 1)", () => {
  let worker: ChildProcess | undefined;
  let client: Client;
  let dispose: () => Promise<void>;

  // The design's PREVIEW-THEN-COMMIT flow: preview (no hash) to surface the resolved policy hash,
  // then commit with that hash (an agent would show it to the human between the two calls).
  const callStart = async (executionId: string, claims: { value: string }[]) => {
    const preview = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId, input: { claims } },
    });
    expect(preview.isError).toBeFalsy();
    const p = preview.structuredContent as { preview: boolean; resolved_policy_hash: string | null; input_valid: boolean };
    expect(p.preview).toBe(true);
    expect(p.input_valid).toBe(true);
    return client.callTool({
      name: "start_workflow",
      arguments: {
        workflow_id: "workflow",
        environment_id: "local",
        execution_id: executionId,
        input: { claims },
        ...(p.resolved_policy_hash ? { expected_policy_hash: p.resolved_policy_hash } : {}),
      },
    });
  };

  const getStatus = async (executionId: string): Promise<StatusSnapshot> => {
    const result = await client.callTool({
      name: "get_status",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId },
    });
    expect(result.isError).toBeFalsy();
    return (result.structuredContent as { status: StatusSnapshot }).status;
  };

  const pollStatus = async (executionId: string, until: (state: string) => boolean, timeoutMs = 30_000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const snapshot = await getStatus(executionId);
      if (until(snapshot.status.state)) return snapshot;
      if (Date.now() > deadline) throw new Error(`status stuck at '${snapshot.status.state}' after ${timeoutMs}ms`);
      await new Promise((r) => setTimeout(r, 500));
    }
  };

  beforeAll(async () => {
    // 1. Spawn the worker and wait for its readiness line.
    worker = spawn("node", [WORKER_HARNESS, WORKFLOW_YAML], { stdio: ["ignore", "pipe", "inherit"] });
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("worker did not become ready within 60s")), 60_000);
      worker!.stdout!.on("data", (chunk: Buffer) => {
        for (const line of chunk.toString().split("\n")) {
          if (line.trim().length === 0) continue;
          try {
            if ((JSON.parse(line) as { event?: string }).event === "ready") {
              clearTimeout(timer);
              resolve();
            }
          } catch {
            /* non-JSON log line */
          }
        }
      });
      worker!.on("exit", (code) => reject(new Error(`worker exited early (code ${code})`)));
    });

    // 2. Start the managed-local MCP server (spins the in-process CP) and connect a client.
    const config = loadConfig({ TYPEFLUX_MANIFEST: MANIFEST });
    const backend = new LazyBackend(config, { managedSchemas: CONFORMANCE_SCHEMAS });
    const built = createTypefluxMcpServer(config, { backend });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    client = new Client({ name: "e2e-operate", version: "0" });
    await Promise.all([built.server.connect(serverTransport), client.connect(clientTransport)]);
    await built.prepare();
    dispose = async () => {
      await client.close();
      await built.dispose();
    };
  }, 90_000);

  afterAll(async () => {
    await dispose?.();
    if (worker !== undefined) {
      worker.kill("SIGTERM");
      await new Promise((r) => setTimeout(r, 500));
    }
  });

  it("registers the operate tools with confirmation hints when the caller can operate", async () => {
    const { tools } = await client.listTools();
    const byName = new Map(tools.map((t) => [t.name, t]));
    for (const op of ["start_workflow", "get_status", "submit_review", "cancel_workflow", "repin_operations", "refresh_project"]) {
      expect(byName.has(op)).toBe(true);
    }
    expect(byName.get("start_workflow")!.annotations?.readOnlyHint).toBe(false);
    expect(byName.get("cancel_workflow")!.annotations?.destructiveHint).toBe(true);
  });

  it("starts, reaches the review gate, approves to completion", async () => {
    const executionId = `mcp-review-${Date.now()}`;
    const start = await callStart(executionId, [{ value: "c1" }]);
    expect(start.isError).toBeFalsy();
    const receipt = (start.structuredContent as { receipt: { workflow_type: string; run_id: string } }).receipt;
    expect(receipt.workflow_type).toBe("typefluxYamlWorkflow");
    expect(typeof receipt.run_id).toBe("string");

    const gated = await pollStatus(executionId, (s) => s === "waiting_for_review");
    expect(Object.keys(gated.valid_user_decisions).length).toBeGreaterThan(0);

    const review = await client.callTool({
      name: "submit_review",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId, user_decision: "approve", reviewer: "e2e" },
    });
    expect(review.isError).toBeFalsy();

    const done = await pollStatus(executionId, (s) => s === "completed" || s === "failed");
    expect(done.status.state).toBe("completed");
  }, 60_000);

  it("cancels a second execution with a reason", async () => {
    const executionId = `mcp-cancel-${Date.now()}`;
    const start = await callStart(executionId, [{ value: "c1" }, { value: "c2" }]);
    expect(start.isError).toBeFalsy();

    await pollStatus(executionId, (s) => s !== "pending");
    const cancel = await client.callTool({
      name: "cancel_workflow",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId, reason: "e2e cancel" },
    });
    expect(cancel.isError).toBeFalsy();

    await pollStatus(executionId, (s) => s === "cancelling" || s === "cancelled" || s === "waiting_for_review");
    const after = await getStatus(executionId);
    expect(after.status.cancellation_requested).toBe(true);
  }, 60_000);

  it("/typeflux:review-gate surfaces the open gate, then the decision submits (§7 end-to-end)", async () => {
    const executionId = `mcp-prompt-review-${Date.now()}`;
    await callStart(executionId, [{ value: "c1" }]);
    await pollStatus(executionId, (s) => s === "waiting_for_review");

    // The recipe pulls the live status and surfaces the OPEN gate + the version-valid decisions.
    const rendered = await client.getPrompt({
      name: "typeflux:review-gate",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId },
    });
    const text = (rendered.messages[0]!.content as { text: string }).text;
    expect(text).toContain("waiting_for_review");
    expect(text).toContain("valid_user_decisions");
    expect(text).toContain("submit_review");

    // The recipe routes to submit_review — driving it clears the gate to completion, as the agent would.
    const review = await client.callTool({
      name: "submit_review",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId, user_decision: "approve", reviewer: "e2e-prompt" },
    });
    expect(review.isError).toBeFalsy();
    const done = await pollStatus(executionId, (s) => s === "completed" || s === "failed");
    expect(done.status.state).toBe("completed");
  }, 60_000);

  it("autocompletes workflow_id and environment_id against the live project (§4)", async () => {
    const wf = await client.complete({
      ref: { type: "ref/prompt", name: "typeflux:diagnose-run" },
      argument: { name: "workflow_id", value: "" },
    });
    expect(wf.completion.values).toContain("workflow");
    const env = await client.complete({
      ref: { type: "ref/prompt", name: "typeflux:diagnose-run" },
      argument: { name: "environment_id", value: "" },
    });
    expect(env.completion.values).toContain("local");
  }, 30_000);

  it("a status subscription emits an update as a run transitions", async () => {
    const executionId = `mcp-sub-${Date.now()}`;
    const uri = `typeflux://default/workflows/workflow/status?environment_id=local&execution_id=${executionId}`;
    const updated = new Promise<void>((resolve) => {
      client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => {
        if (n.params.uri === uri) resolve();
      });
    });
    // Start first, THEN subscribe to the live run (the natural "watch a running run" flow), so the
    // subscription's baseline poll reads a real snapshot. Drive a transition (approve → completed)
    // so the poller observes a change and emits resources/updated.
    await callStart(executionId, [{ value: "c1" }]);
    await pollStatus(executionId, (s) => s === "waiting_for_review");
    await client.subscribeResource({ uri });
    await client.callTool({
      name: "submit_review",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: executionId, user_decision: "approve" },
    });
    await Promise.race([
      updated,
      new Promise((_r, reject) => setTimeout(() => reject(new Error("no resource update within 30s")), 30_000)),
    ]);
    await client.unsubscribeResource({ uri });
  }, 60_000);
});
