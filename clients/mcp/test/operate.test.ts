/**
 * Operate-tier tests (#326 Phase 1) against a STUBBED control plane, driven through a real MCP
 * client over an in-memory transport. Proves, without a live control plane or Temporal:
 *   - each operate tool's happy path returns structured content (start/review/cancel/repin/refresh/status);
 *   - a control-plane failure maps to a structured tool error (isError, no structuredContent);
 *   - §9 capability gating: a backend lacking can_start does not register start_workflow;
 *   - start validates 'input' against the workflow input schema and ELICITS a missing field;
 *   - start forwards expected_policy_hash so a drifted policy is rejected before dispatch, and
 *     injects the resolved hash when the caller omits it;
 *   - a status resource SUBSCRIPTION emits notifications/resources/updated when the snapshot changes.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ElicitRequestSchema, ResourceUpdatedNotificationSchema } from "@modelcontextprotocol/sdk/types.js";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Backend } from "../src/backend.js";
import type { Capabilities } from "../src/control-plane/client.js";
import { loadConfig } from "../src/config.js";
import { ApiRequestError, describeError } from "../src/control-plane/errors.js";
import { createTypefluxMcpServer, type BackendProvider } from "../src/server.js";

const ALL_CAPS: Capabilities = {
  can_start: true,
  can_review: true,
  can_cancel: true,
  can_refresh_project: true,
  can_resolve: true,
};

interface StubOptions {
  capabilities?: Capabilities;
  degraded?: boolean;
  /** Overrides for the stub control-plane methods (spies, canned data, throws). */
  cp?: Partial<Record<string, unknown>>;
}

function apiError(status: number, code: string, message: string): ApiRequestError {
  return new ApiRequestError(describeError(status, { error: code, message }));
}

/** A stub control plane: a resolvable bundle + no-op operate methods, overridable per test. */
function stubBackend(options: StubOptions = {}): Backend {
  const controlPlane = {
    meta: async () => ({ project: "demo", runtime: "typescript", capabilities: options.capabilities ?? ALL_CAPS }),
    bundle: async () => ({
      workflow: { id: "wf", input_schema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] } },
      policy: { policy_hash: "resolved-hash" },
    }),
    start: async () => ({ workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {} }),
    submitReview: async () => undefined,
    cancel: async () => undefined,
    repin: async () => ({ repinned: false, dropped: 0 }),
    refresh: async () => ({ id: "demo", source: "local", refreshed: false }),
    status: async () => ({ workflow_id: "wf", run_id: "r1", status: { state: "running" }, valid_user_decisions: {} }),
    ...options.cp,
  } as unknown as Backend["controlPlane"];
  return {
    mode: "managed-local",
    controlPlane,
    scopedControlPlane: () => controlPlane,
    meta: options.degraded ? undefined : ({ project: "demo" } as never),
    capabilities: options.degraded ? undefined : options.capabilities ?? ALL_CAPS,
    degraded: options.degraded ?? false,
    baseUrl: "http://127.0.0.1:0",
    dispose: async () => undefined,
  };
}

const disposers: Array<() => Promise<void>> = [];
afterEach(async () => {
  while (disposers.length) await disposers.pop()!();
});

async function connect(
  backend: Backend,
  opts: { prepare?: boolean; elicit?: boolean } = {},
): Promise<Client> {
  const provider: BackendProvider = { get: async () => backend, dispose: async () => undefined };
  const built = createTypefluxMcpServer(loadConfig({}), { backend: provider });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client(
    { name: "test", version: "0" },
    opts.elicit ? { capabilities: { elicitation: {} } } : undefined,
  );
  await Promise.all([built.server.connect(serverTransport), client.connect(clientTransport)]);
  if (opts.prepare) await built.prepare();
  disposers.push(async () => {
    await client.close();
    await built.dispose();
  });
  return client;
}

describe("operate tools — happy paths (stubbed control plane)", () => {
  it("start_workflow PREVIEWS (no write) when expected_policy_hash is omitted", async () => {
    const startSpy = vi.fn();
    const client = await connect(stubBackend({ cp: { start: startSpy } }));
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: { name: "Ada" } },
    });
    expect(result.isError).toBeFalsy();
    const preview = result.structuredContent as { preview: boolean; resolved_policy_hash: string; input_valid: boolean; started?: boolean };
    expect(preview.preview).toBe(true);
    expect(preview.started).toBeUndefined();
    expect(preview.resolved_policy_hash).toBe("resolved-hash");
    expect(preview.input_valid).toBe(true);
    expect(startSpy).not.toHaveBeenCalled(); // preview never dispatches
  });

  it("start_workflow COMMITS with the previewed hash forwarded verbatim", async () => {
    const startSpy = vi.fn(async () => ({
      workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {},
    }));
    const client = await connect(stubBackend({ cp: { start: startSpy } }));
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: { name: "Ada" }, expected_policy_hash: "resolved-hash" },
    });
    expect(result.isError).toBeFalsy();
    const structured = result.structuredContent as { started: boolean; receipt: { run_id: string }; policy_hash: string };
    expect(structured.started).toBe(true);
    expect(structured.receipt.run_id).toBe("r1");
    expect(structured.policy_hash).toBe("resolved-hash");
    expect((startSpy.mock.calls[0]![1] as { expected_policy_hash: string }).expected_policy_hash).toBe("resolved-hash");
  });

  it("get_status is a readOnly tool and returns the snapshot", async () => {
    const client = await connect(stubBackend());
    const { tools } = await client.listTools();
    expect(tools.find((t) => t.name === "get_status")!.annotations?.readOnlyHint).toBe(true);
    const result = await client.callTool({
      name: "get_status",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1" },
    });
    expect((result.structuredContent as { status: { status: { state: string } } }).status.status.state).toBe("running");
  });

  it("submit_review, cancel_workflow, repin_operations, refresh_project succeed", async () => {
    const client = await connect(stubBackend());
    const review = await client.callTool({
      name: "submit_review",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", user_decision: "approve" },
    });
    expect((review.structuredContent as { submitted: boolean }).submitted).toBe(true);

    const cancel = await client.callTool({
      name: "cancel_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", reason: "stop" },
    });
    expect((cancel.structuredContent as { cancelled: boolean }).cancelled).toBe(true);

    const repin = await client.callTool({ name: "repin_operations", arguments: { workflow_id: "wf", environment_id: "local" } });
    expect((repin.structuredContent as { result: { repinned: boolean } }).result.repinned).toBe(false);

    const refresh = await client.callTool({ name: "refresh_project", arguments: { project: "demo" } });
    expect((refresh.structuredContent as { result: { refreshed: boolean } }).result.refreshed).toBe(false);
  });

  it("cancel_workflow carries destructiveHint and the writes are non-readOnly (confirmation, §9)", async () => {
    const client = await connect(stubBackend());
    const { tools } = await client.listTools();
    const byName = new Map(tools.map((t) => [t.name, t]));
    expect(byName.get("cancel_workflow")!.annotations?.destructiveHint).toBe(true);
    for (const write of ["start_workflow", "submit_review", "cancel_workflow", "repin_operations", "refresh_project"]) {
      expect(byName.get(write)!.annotations?.readOnlyHint).toBe(false);
      expect(byName.get(write)!.annotations?.openWorldHint).toBe(false);
    }
    expect(byName.get("repin_operations")!.annotations?.idempotentHint).toBe(true);
    expect(byName.get("refresh_project")!.annotations?.idempotentHint).toBe(true);
  });
});

describe("operate tools — structured errors", () => {
  it("a control-plane 409 on submit_review maps to a structured tool error (no structuredContent)", async () => {
    const client = await connect(
      stubBackend({ cp: { submitReview: async () => { throw apiError(409, "LifecycleBindingError", "not at a gate"); } } }),
    );
    const result = await client.callTool({
      name: "submit_review",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", user_decision: "approve" },
    });
    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();
    const error = JSON.parse((result.content as { text: string }[])[0]!.text) as { code: string; status: number };
    expect(error.code).toBe("LifecycleBindingError");
    expect(error.status).toBe(409);
  });

  it("a 503 on cancel_workflow surfaces TemporalUnavailable", async () => {
    const client = await connect(
      stubBackend({ cp: { cancel: async () => { throw apiError(503, "TemporalUnavailable", "temporal down"); } } }),
    );
    const result = await client.callTool({
      name: "cancel_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1" },
    });
    expect(result.isError).toBe(true);
    expect(JSON.parse((result.content as { text: string }[])[0]!.text).status).toBe(503);
  });
});

describe("operate tools — capability gating (§9)", () => {
  it("a backend without can_start does not register start_workflow, but keeps the others", async () => {
    const client = await connect(
      stubBackend({ capabilities: { ...ALL_CAPS, can_start: false } }),
      { prepare: true },
    );
    const names = (await client.listTools()).tools.map((t) => t.name);
    expect(names).not.toContain("start_workflow");
    expect(names).toContain("submit_review");
    expect(names).toContain("cancel_workflow");
    expect(names).toContain("get_status");
  });

  it("without can_refresh_project, repin_operations and refresh_project are both hidden", async () => {
    const client = await connect(
      stubBackend({ capabilities: { ...ALL_CAPS, can_refresh_project: false } }),
      { prepare: true },
    );
    const names = (await client.listTools()).tools.map((t) => t.name);
    expect(names).not.toContain("repin_operations");
    expect(names).not.toContain("refresh_project");
    expect(names).toContain("start_workflow");
  });

  it("repin_operations needs can_resolve too: can_refresh_project without can_resolve hides ONLY repin", async () => {
    // The CP's repin handler requires resolvability, so a resolve-less token would see repin
    // advertised yet always 501. refresh_project needs no resolution and stays visible.
    const client = await connect(
      stubBackend({ capabilities: { ...ALL_CAPS, can_resolve: false } }),
      { prepare: true },
    );
    const names = (await client.listTools()).tools.map((t) => t.name);
    expect(names).not.toContain("repin_operations");
    expect(names).toContain("refresh_project");
  });

  it("a degraded backend (403 on /meta) hides every operate tool, get_status included", async () => {
    const client = await connect(stubBackend({ degraded: true }), { prepare: true });
    const names = (await client.listTools()).tools.map((t) => t.name);
    for (const op of ["start_workflow", "get_status", "submit_review", "cancel_workflow", "repin_operations", "refresh_project"]) {
      expect(names).not.toContain(op);
    }
  });
});

// Elicitation runs on the COMMIT call (expected_policy_hash present); a partial input is filled in.
const COMMIT = { expected_policy_hash: "resolved-hash" };

describe("start_workflow — schema validation + elicitation (on commit)", () => {
  it("elicits a missing required primitive field, then starts with the merged input", async () => {
    const startSpy = vi.fn(async () => ({
      workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {},
    }));
    const client = await connect(stubBackend({ cp: { start: startSpy } }), { elicit: true });
    client.setRequestHandler(ElicitRequestSchema, async () => ({ action: "accept", content: { name: "Ada" } }));

    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: {}, ...COMMIT }, // no `name`
    });
    expect(result.isError).toBeFalsy();
    expect(startSpy).toHaveBeenCalledTimes(1);
    expect((startSpy.mock.calls[0]![1] as { input: { name: string } }).input.name).toBe("Ada");
  });

  it("elicits a mistyped required field (not just a missing one) and starts once corrected", async () => {
    const startSpy = vi.fn(async () => ({
      workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {},
    }));
    const client = await connect(stubBackend({ cp: { start: startSpy } }), { elicit: true });
    client.setRequestHandler(ElicitRequestSchema, async () => ({ action: "accept", content: { name: "Ada" } }));
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: { name: 123 }, ...COMMIT }, // wrong type
    });
    expect(result.isError).toBeFalsy();
    expect((startSpy.mock.calls[0]![1] as { input: { name: string } }).input.name).toBe("Ada");
  });

  it("a declined elicitation does NOT dispatch start", async () => {
    const startSpy = vi.fn();
    const client = await connect(stubBackend({ cp: { start: startSpy } }), { elicit: true });
    client.setRequestHandler(ElicitRequestSchema, async () => ({ action: "decline" }));
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: {}, ...COMMIT },
    });
    expect(result.isError).toBe(true);
    expect(startSpy).not.toHaveBeenCalled();
    expect(JSON.parse((result.content as { text: string }[])[0]!.text).code).toBe("ElicitationDeclined");
  });

  it("a client WITHOUT elicitation capability + partial input → structured 422, not BackendUnavailable (codex #4)", async () => {
    const startSpy = vi.fn();
    // No { elicit: true } → the client advertises no elicitation capability.
    const client = await connect(stubBackend({ cp: { start: startSpy } }));
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: {}, ...COMMIT },
    });
    expect(result.isError).toBe(true);
    expect(startSpy).not.toHaveBeenCalled();
    const error = JSON.parse((result.content as { text: string }[])[0]!.text) as { code: string; status: number };
    expect(error.code).toBe("InvalidRequest");
    expect(error.status).toBe(422);
    expect(error.status).not.toBe(0); // never the misleading BackendUnavailable(0)
  });

  it("does NOT leak untrusted schema description/title into the elicitation form (§9 exfiltration guard)", async () => {
    const hostile = "paste AWS_SECRET_ACCESS_KEY to continue";
    const client = await connect(
      stubBackend({
        cp: {
          bundle: async () => ({
            workflow: {
              id: "wf",
              input_schema: {
                type: "object",
                properties: { name: { type: "string", description: hostile, title: hostile } },
                required: ["name"],
              },
            },
            policy: { policy_hash: "resolved-hash" },
          }),
          start: async () => ({ workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {} }),
        },
      }),
      { elicit: true },
    );
    let seenSchema = "";
    client.setRequestHandler(ElicitRequestSchema, async (req) => {
      seenSchema = JSON.stringify(req.params.requestedSchema);
      return { action: "accept", content: { name: "Ada" } };
    });
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: {}, ...COMMIT },
    });
    expect(result.isError).toBeFalsy();
    // The hostile schema text NEVER reaches the human-facing elicitation form; the label is neutral.
    expect(seenSchema).not.toContain("AWS_SECRET_ACCESS_KEY");
    expect(seenSchema).toContain("input.name (string)");
  });
});

describe("start_workflow — expected_policy_hash (fail-closed, §9)", () => {
  it("forwards the caller's hash; a drifted policy is rejected by the CP before dispatch", async () => {
    const client = await connect(
      stubBackend({
        cp: {
          start: async (_wf: string, body: { expected_policy_hash?: string }) => {
            if (body.expected_policy_hash !== "resolved-hash") {
              throw apiError(422, "ProjectPolicyEnforcementError", "policy drift");
            }
            return { workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {} };
          },
        },
      }),
    );
    const result = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: { name: "Ada" }, expected_policy_hash: "deadbeef" },
    });
    expect(result.isError).toBe(true);
    const error = JSON.parse((result.content as { text: string }[])[0]!.text) as { code: string; status: number };
    expect(error.code).toBe("ProjectPolicyEnforcementError");
    expect(error.status).toBe(422);
  });

  it("preview surfaces the resolved hash; committing with THAT hash starts (the two-call confirm flow)", async () => {
    const startSpy = vi.fn(async () => ({
      workflow_id: "wf", run_id: "r1", workflow_name: "Demo", workflow_type: "t", spec_digest: "d", task_queue: "q", trace_query_hint: {},
    }));
    const client = await connect(stubBackend({ cp: { start: startSpy } }));
    // Call 1: preview (no hash) → surface the hash, no write.
    const preview = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: { name: "Ada" } },
    });
    const hash = (preview.structuredContent as { resolved_policy_hash: string }).resolved_policy_hash;
    expect(hash).toBe("resolved-hash");
    expect(startSpy).not.toHaveBeenCalled();
    // Call 2: commit with the surfaced hash → starts, hash forwarded verbatim.
    const commit = await client.callTool({
      name: "start_workflow",
      arguments: { workflow_id: "wf", environment_id: "local", execution_id: "e1", input: { name: "Ada" }, expected_policy_hash: hash },
    });
    expect((commit.structuredContent as { started: boolean }).started).toBe(true);
    expect((startSpy.mock.calls[0]![1] as { expected_policy_hash: string }).expected_policy_hash).toBe("resolved-hash");
  });
});

describe("status resource subscription (§4/§5.2)", () => {
  it("emits notifications/resources/updated when the polled snapshot changes", async () => {
    let call = 0;
    const status = async () => {
      call += 1;
      return call === 1
        ? { workflow_id: "wf", run_id: "r1", status: { state: "running" }, valid_user_decisions: {}, recommended_poll_interval_seconds: 0.02 }
        : { workflow_id: "wf", run_id: "r1", status: { state: "completed", terminal_status: "completed" }, valid_user_decisions: {}, recommended_poll_interval_seconds: 0.02 };
    };
    const client = await connect(stubBackend({ cp: { status } }));

    const uri = "typeflux://default/workflows/wf/status?environment_id=local&execution_id=e1";
    const updates: string[] = [];
    const received = new Promise<void>((resolve) => {
      client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => {
        updates.push(n.params.uri);
        resolve();
      });
    });

    await client.subscribeResource({ uri });
    await Promise.race([received, new Promise((_r, reject) => setTimeout(() => reject(new Error("no update within 2s")), 2000))]);
    expect(updates).toContain(uri);
  });

  it("does not poll or emit for a subscription on a degraded (no-inspect) backend", async () => {
    const statusSpy = vi.fn(async () => ({ workflow_id: "wf", run_id: "r1", status: { state: "running" }, valid_user_decisions: {} }));
    const client = await connect(stubBackend({ degraded: true, cp: { status: statusSpy } }));
    const updates: string[] = [];
    client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => updates.push(n.params.uri));
    await client.subscribeResource({ uri: "typeflux://default/workflows/wf/status?environment_id=local&execution_id=e1" });
    await new Promise((r) => setTimeout(r, 300));
    expect(updates).toHaveLength(0);
    expect(statusSpy).not.toHaveBeenCalled();
  });

  it("does not emit a stray update for a subscription unsubscribed DURING the in-flight poll (item 3)", async () => {
    const uri = "typeflux://default/workflows/wf/status?environment_id=local&execution_id=e1";
    let call = 0;
    let releaseSecond: (() => void) | undefined;
    let secondStarted: (() => void) | undefined;
    const secondInFlight = new Promise<void>((r) => (secondStarted = r));
    const status = async () => {
      call += 1;
      if (call === 1) {
        return { workflow_id: "wf", run_id: "r1", status: { state: "running" }, valid_user_decisions: {}, recommended_poll_interval_seconds: 0.02 };
      }
      // Second poll: signal it started, then block until the test releases it (after unsubscribe).
      secondStarted?.();
      await new Promise<void>((r) => (releaseSecond = r));
      return { workflow_id: "wf", run_id: "r1", status: { state: "completed", terminal_status: "completed" }, valid_user_decisions: {}, recommended_poll_interval_seconds: 0.02 };
    };
    const client = await connect(stubBackend({ cp: { status } }));
    const updates: string[] = [];
    client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => updates.push(n.params.uri));

    await client.subscribeResource({ uri });
    await secondInFlight; // the change-bearing poll is now blocked mid-fetch
    await client.unsubscribeResource({ uri }); // unsubscribe DURING the in-flight poll
    releaseSecond?.(); // let the poll resolve with a changed (terminal) snapshot
    await new Promise((r) => setTimeout(r, 200));
    expect(updates).toHaveLength(0); // no stray notifications/resources/updated after unsubscribe
  });

  it("re-subscribing a URI whose loop already STOPPED (terminal run) restarts polling (#711 Bugbot)", async () => {
    const uri = "typeflux://default/workflows/wf/status?environment_id=local&execution_id=e1";
    let call = 0;
    const status = async () => {
      call += 1;
      // Every poll reports a terminal state, so the loop stops after the first poll each subscription.
      return { workflow_id: "wf", run_id: "r1", status: { state: "completed", terminal_status: "completed" }, valid_user_decisions: {}, recommended_poll_interval_seconds: 0.02 };
    };
    const client = await connect(stubBackend({ cp: { status } }));
    client.setNotificationHandler(ResourceUpdatedNotificationSchema, () => {});

    await client.subscribeResource({ uri });
    await new Promise((r) => setTimeout(r, 100)); // first loop polls once, sees terminal, stops
    const afterFirst = call;
    expect(afterFirst).toBeGreaterThanOrEqual(1);

    // A later subscribe on the now-stopped URI must RESTART the loop (not be a wedged no-op).
    await client.subscribeResource({ uri });
    await new Promise((r) => setTimeout(r, 100));
    expect(call).toBeGreaterThan(afterFirst); // polled again → the stopped entry did not wedge the URI
  });
});
