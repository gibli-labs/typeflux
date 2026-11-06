/**
 * GATED live operate-tier test (#563): the TS control-plane server's HTTP SURFACE operates a REAL
 * TS worker execution end-to-end. Skipped unless TYPEFLUX_LIVE_TEMPORAL=1 (there is no Temporal
 * server in CI) — the orchestrator runs it against a local dev server on localhost:7233 after
 * `pnpm -r build`.
 *
 * What it proves over the wire (no in-process operations shortcut — every op is an HTTP request to
 * the server's own `serve()` on an ephemeral port):
 *   1. start → 200 receipt with the plan-as-argument identity (workflow_type typefluxYamlWorkflow).
 *   2. poll status until state=waiting_for_review (the review gate opened).
 *   3. review-approve → 204, then status reaches state=completed.
 *   4. a second execution: cancel with a reason → 204, then cancellation_requested=true.
 *   5. a FOREIGN execution (a raw client.start of ANOTHER workflow type) → status 409
 *      LifecycleBindingError (memo/type verification fails closed).
 *
 * The worker runs in a spawned subprocess (test/live-operate-worker.mjs) so the CP package needs no
 * @temporalio/worker dependency; the CP server + all operate requests run in THIS process.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { appendFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { createServer as createPortLease } from "node:net";
import { parse, stringify } from "yaml";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { loadProjectRegistry, serve, type ControlPlaneServer } from "../src/index.js";
import { CONFORMANCE_SCHEMAS } from "../src/http/conformance-schemas.js";

const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";
const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = resolve(HERE, "../../../../contracts/controlplane/conformance/project/typescript");
const REGISTRY = resolve(PROJECT_DIR, "typeflux.projects.yaml");
const WORKFLOW_YAML = resolve(PROJECT_DIR, "workflow.yaml");
// Lives in temporal-yaml (like the #618 harness): Node ESM resolves imports from the FILE's
// package, and @temporalio/worker is temporal-yaml's dependency, not this package's.
const WORKER_HARNESS = resolve(HERE, "../../temporal-yaml/scripts/live-operate-worker.mjs");
const ADDRESS = "localhost:7233";

async function stopOwned(child: ChildProcess | undefined, signal: "SIGTERM" | "SIGKILL" = "SIGTERM"): Promise<void> {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise<void>((done) => child.once("close", () => done()));
  child.kill(signal);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([exited, new Promise<never>((_, reject) => {
      timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("owned no-poller process did not stop within 10s")); }, 10_000);
    })]);
  } finally { if (timer) clearTimeout(timer); }
}

async function startOwnedServer(port: number, directory: string): Promise<ChildProcess> {
  const cli = resolve(HERE, "../../../../.project-graph/bin/temporal");
  const child = spawn(cli, ["server", "start-dev", "--ip", "127.0.0.1", "--port", String(port),
    "--headless", "--db-filename", resolve(directory, "history.db")], {stdio: ["ignore", "pipe", "pipe"]});
  const log = resolve(directory, "server.log");
  let stdout = "";
  child.stdout!.on("data", (chunk: Buffer) => { stdout += chunk.toString(); appendFileSync(log, chunk); });
  child.stderr!.on("data", (chunk: Buffer) => appendFileSync(log, chunk));
  try {
    await new Promise<void>((done, reject) => {
      const timer = setTimeout(() => reject(new Error("owned no-poller server startup exceeded 45s")), 45_000);
      const ready = () => {
        if (new RegExp(`Temporal Server:\\s+[^\\s]+:${port}(?:\\s|$)`).test(stdout)) {
          clearTimeout(timer); done();
        }
      };
      child.stdout!.on("data", ready);
      child.once("error", (error) => {clearTimeout(timer); reject(error);});
      child.once("exit", () => {clearTimeout(timer); reject(new Error("owned no-poller server exited"));});
      ready();
    });
    return child;
  } catch (error) { await stopOwned(child); throw error; }
}

describe.skipIf(!LIVE)("operate tier — live end-to-end over the CP HTTP surface (#563)", () => {
  let worker: ChildProcess | undefined;
  let server: ControlPlaneServer | undefined;
  let base = "";

  const api = async (
    method: string,
    path: string,
    body?: unknown,
  ): Promise<{ status: number; body: unknown }> => {
    const response = await fetch(`${base}${path}`, {
      method,
      ...(body !== undefined ? { body: JSON.stringify(body), headers: { "content-type": "application/json" } } : {}),
    });
    const text = await response.text();
    return { status: response.status, body: text.length > 0 ? JSON.parse(text) : undefined };
  };

  const pollStatus = async (executionId: string, until: (state: string) => boolean, timeoutMs = 30_000) => {
    const deadline = Date.now() + timeoutMs;
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { status, body } = await api(
        "GET",
        `/api/v1/workflows/workflow/status?environment_id=local&execution_id=${encodeURIComponent(executionId)}`,
      );
      expect(status).toBe(200);
      const state = (body as { status: { state: string } }).status.state;
      if (until(state)) return body as { status: { state: string; cancellation_requested: boolean } };
      if (Date.now() > deadline) throw new Error(`status did not reach the target from '${state}' within ${timeoutMs}ms`);
      await new Promise((r) => setTimeout(r, 500));
    }
  };

  beforeAll(async () => {
    // Spawn the worker and wait for its readiness line.
    worker = spawn("node", [WORKER_HARNESS, WORKFLOW_YAML], { stdio: ["ignore", "pipe", "inherit"] });
    await new Promise<void>((resolvePromise, reject) => {
      const timer = setTimeout(() => reject(new Error("worker did not become ready within 60s")), 60_000);
      worker!.stdout!.on("data", (chunk: Buffer) => {
        for (const line of chunk.toString().split("\n")) {
          if (line.trim().length === 0) continue;
          try {
            if ((JSON.parse(line) as { event?: string }).event === "ready") {
              clearTimeout(timer);
              resolvePromise();
            }
          } catch {
            /* non-JSON log line — ignore */
          }
        }
      });
      worker!.on("exit", (code) => reject(new Error(`worker exited early (code ${code})`)));
    });

    server = await serve({
      registry: loadProjectRegistry(REGISTRY),
      // The embedding host injects schemas: start VALIDATES the caller's input against the
      // workflow's input schema before dispatch (Python `_coerce_input` parity), so the operate
      // tier needs the fixture schemas just like bundle/catalog do.
      schemasFor: () => CONFORMANCE_SCHEMAS,
      port: 0,
      host: "127.0.0.1",
    });
    base = `http://127.0.0.1:${server.port}`;
  }, 90_000);

  afterAll(async () => {
    if (server !== undefined) await server.close();
    if (worker !== undefined) {
      worker.kill("SIGTERM");
      await new Promise((r) => setTimeout(r, 500));
    }
  });

  it("reports a real no-poller lifecycle query as unavailable without signalling", async () => {
    const {Client, Connection} = await import("@temporalio/client");
    const directory = mkdtempSync(resolve(tmpdir(), "typeflux-no-poller-"));
    const project = resolve(directory, "project");
    cpSync(PROJECT_DIR, project, {recursive: true});
    const lease = createPortLease();
    await new Promise<void>((done, reject) => {lease.once("error", reject); lease.listen(0, "127.0.0.1", done);});
    const port = (lease.address() as {port: number}).port;
    await new Promise<void>((done, reject) => lease.close(error => error ? reject(error) : done()));
    const address = `127.0.0.1:${port}`;
    const spec = parse(readFileSync(resolve(project, "workflow.yaml"), "utf8"));
    spec.runtime.temporal.address = address;
    spec.task_queue = `no-poller-${port}`;
    writeFileSync(resolve(project, "workflow.yaml"), stringify(spec));
    const environment = parse(readFileSync(resolve(project, "environments/local.yaml"), "utf8"));
    environment.overrides.runtime.temporal.address = address;
    writeFileSync(resolve(project, "environments/local.yaml"), stringify(environment));
    let ownedServer: ChildProcess | undefined;
    let ownedWorker: ChildProcess | undefined;
    let ownApi: ControlPlaneServer | undefined;
    let connection: Awaited<ReturnType<typeof Connection.connect>> | undefined;
    let handle: ReturnType<InstanceType<typeof Client>["workflow"]["getHandle"]> | undefined;
    try {
      ownedServer = await startOwnedServer(port, directory);
      connection = await Connection.connect({address, connectTimeout: 3000});
      const client = new Client({connection, namespace: "default"});
      ownApi = await serve({registry: loadProjectRegistry(resolve(project, "typeflux.projects.yaml")),
        schemasFor: () => CONFORMANCE_SCHEMAS, port: 0, host: "127.0.0.1"});
      const apiUrl = `http://127.0.0.1:${ownApi.port}`;
      ownedWorker = spawn("node", [WORKER_HARNESS, resolve(project, "workflow.yaml")], {stdio: ["ignore", "pipe", "inherit"]});
      await new Promise<void>((done, reject) => {
        const timer = setTimeout(() => reject(new Error("owned no-poller worker startup exceeded 45s")), 45_000);
        let text = "";
        ownedWorker!.stdout!.on("data", (chunk: Buffer) => {
          text = (text + chunk.toString()).slice(-8192);
          if (text.includes('"event":"ready"')) { clearTimeout(timer); done(); }
        });
        ownedWorker!.once("error", error => {clearTimeout(timer); reject(error);});
        ownedWorker!.once("exit", () => {clearTimeout(timer); reject(new Error("owned no-poller worker exited"));});
      });
      // The gate below establishes execution; the readiness line alone is insufficient.
      const executionId = `live-no-poller-${port}`;
      const receipt = await fetch(apiUrl + "/api/v1/workflows/workflow/start", {method: "POST",
        headers: {"content-type": "application/json"}, body: JSON.stringify({environment_id: "local",
          execution_id: executionId, input: {claims: [{value: "fixed"}]}})});
      expect(receipt.status).toBe(200);
      const runId = ((await receipt.json()) as {run_id: string}).run_id;
      expect(typeof runId).toBe("string");
      handle = client.workflow.getHandle(executionId, runId);
      const statusUrl = apiUrl + `/api/v1/workflows/workflow/status?environment_id=local&execution_id=${executionId}&run_id=${runId}`;
      const deadline = Date.now() + 30_000;
      while (true) {
        const response = await fetch(statusUrl);
        expect(response.status).toBe(200);
        if (((await response.json()) as {status: {state: string}}).status.state === "waiting_for_review") break;
        if (Date.now() >= deadline) throw new Error("owned workflow did not reach its gate");
        await new Promise(done => setTimeout(done, 100));
      }
      const before = await handle.fetchHistory();
      await stopOwned(ownedWorker);
      await connection.close(); connection = undefined;
      await stopOwned(ownedServer, "SIGKILL");
      ownedServer = await startOwnedServer(port, directory);
      connection = await Connection.connect({address, connectTimeout: 3000});
      const recovered = new Client({connection, namespace: "default"});
      handle = recovered.workflow.getHandle(executionId, runId);
      expect((await handle.describe()).runId).toBe(runId);
      expect((await handle.fetchHistory()).events?.length).toBeGreaterThanOrEqual(before.events?.length ?? 0);
      let failure: unknown;
      try { await connection.withDeadline(Date.now() + 3000, () => handle!.query("typeflux_lifecycle_status")); }
      catch (error) { failure = error; }
      const causes: Array<{code: unknown; details: unknown}> = [];
      for (let depth = 0; depth < 5 && failure instanceof Error; depth++) {
        causes.push({code: (failure as {code?: unknown}).code, details: (failure as {details?: unknown}).details});
        failure = failure.cause;
      }
      expect(causes).toContainEqual({code: 9, details: "no poller seen for task queue recently, worker may be down"});
      const response = await fetch(statusUrl);
      expect(response.status).toBe(503);
      const body = await response.json() as {error: string; message: string};
      expect(body.error).toBe("TemporalUnavailable");
      expect(body.message).toContain("compatible worker");
      const history = await handle.fetchHistory();
      expect(history.events?.filter(event => event.workflowExecutionSignaledEventAttributes)).toHaveLength(0);
      expect(history.events?.filter(event => event.activityTaskStartedEventAttributes)).toHaveLength(1);
    } finally {
      try { if (handle && connection) await handle.terminate("owned no-poller cleanup"); }
      finally {
        const outcomes = await Promise.allSettled([connection?.close(), ownApi?.close(),
          stopOwned(ownedWorker), stopOwned(ownedServer)]);
        const evidence = resolve(HERE, "../../../../.project-graph/evidence/temporal");
        mkdirSync(evidence, {recursive: true});
        if (existsSync(resolve(directory, "server.log")))
          cpSync(resolve(directory, "server.log"), resolve(evidence, "no-poller-server.log"));
        const failed = outcomes.filter((outcome): outcome is PromiseRejectedResult => outcome.status === "rejected");
        if (failed.length) throw new AggregateError(failed.map(outcome => outcome.reason), "owned no-poller cleanup incomplete; temporary data retained");
        rmSync(directory, {recursive: true, force: true});
      }
    }
  }, 180_000);


  it("starts, reaches the review gate, approves to completion", async () => {
    const executionId = `live-review-${Date.now()}`;
    const start = await api("POST", "/api/v1/workflows/workflow/start", {
      environment_id: "local",
      execution_id: executionId,
      input: { claims: [{ value: "c1" }] },
    });
    expect(start.status).toBe(200);
    expect((start.body as { workflow_type: string }).workflow_type).toBe("typefluxYamlWorkflow");

    await pollStatus(executionId, (state) => state === "waiting_for_review");

    const review = await api("POST", "/api/v1/workflows/workflow/review", {
      environment_id: "local",
      execution_id: executionId,
      command: { user_decision: "approve" },
    });
    expect(review.status).toBe(204);

    const done = await pollStatus(executionId, (state) => state === "completed" || state === "failed");
    expect(done.status.state).toBe("completed");
  }, 60_000);

  it("a policied start (#663): the correct expected hash is honored, a wrong one is a 422 before dispatch", async () => {
    // The conformance TS project binds `base` to workflow/local (its `local` validation target),
    // so the operate gate composes it even without explicit ids. Read the composed closure hash
    // off the bundle projection — the same hash the gate verifies.
    const bundle = await api("GET", "/api/v1/workflows/workflow/bundle?environment_id=local");
    expect(bundle.status).toBe(200);
    const policyHash = (bundle.body as { policy: { policy_hash: string } }).policy.policy_hash;
    expect(typeof policyHash).toBe("string");

    // Wrong hash → 422 ProjectPolicyEnforcementError, and it never starts an execution.
    const wrongId = `live-policy-wrong-${Date.now()}`;
    const wrong = await api("POST", "/api/v1/workflows/workflow/start", {
      environment_id: "local",
      execution_id: wrongId,
      input: { claims: [{ value: "c1" }] },
      policy_ids: ["base"],
      expected_policy_hash: "deadbeef",
    });
    expect(wrong.status).toBe(422);
    expect((wrong.body as { error: string }).error).toBe("ProjectPolicyEnforcementError");

    // Correct hash → the policy is composed + admitted, and the real execution runs to completion.
    const okId = `live-policy-ok-${Date.now()}`;
    const ok = await api("POST", "/api/v1/workflows/workflow/start", {
      environment_id: "local",
      execution_id: okId,
      input: { claims: [{ value: "c1" }] },
      policy_ids: ["base"],
      expected_policy_hash: policyHash,
    });
    expect(ok.status).toBe(200);
    await pollStatus(okId, (state) => state === "waiting_for_review");
    const review = await api("POST", "/api/v1/workflows/workflow/review", {
      environment_id: "local",
      execution_id: okId,
      command: { user_decision: "approve" },
    });
    expect(review.status).toBe(204);
    const done = await pollStatus(okId, (state) => state === "completed" || state === "failed");
    expect(done.status.state).toBe("completed");
  }, 60_000);

  it("cancels a second execution with a reason", async () => {
    const executionId = `live-cancel-${Date.now()}`;
    const start = await api("POST", "/api/v1/workflows/workflow/start", {
      environment_id: "local",
      execution_id: executionId,
      input: { claims: [{ value: "c1" }, { value: "c2" }] },
    });
    expect(start.status).toBe(200);

    // Wait until it is at least running before cancelling.
    await pollStatus(executionId, (state) => state !== "pending");
    const cancel = await api("POST", "/api/v1/workflows/workflow/cancel", {
      environment_id: "local",
      execution_id: executionId,
      reason: "live-test cancel",
    });
    expect(cancel.status).toBe(204);

    const after = await pollStatus(executionId, (s) => s === "cancelling" || s === "cancelled" || s === "waiting_for_review");
    // The cancel signal is recorded — cancellation_requested flips true regardless of the exact state.
    const status = await api(
      "GET",
      `/api/v1/workflows/workflow/status?environment_id=local&execution_id=${executionId}`,
    );
    expect((status.body as { status: { cancellation_requested: boolean } }).status.cancellation_requested).toBe(true);
    expect(after.status.state).toBeDefined();
  }, 60_000);

  it("a foreign execution (another workflow type) fails status with 409 LifecycleBindingError", async () => {
    // Start a raw workflow of a DIFFERENT type directly, bypassing the CP — the CP's memo/type
    // verification must refuse to operate it.
    const { Client, Connection } = await import("@temporalio/client");
    const connection = await Connection.connect({ address: ADDRESS });
    const client = new Client({ connection, namespace: "default" });
    const foreignId = `live-foreign-${Date.now()}`;
    try {
      // A workflow type the worker does not even serve; it will not progress, but describe() returns
      // its (foreign) type, which is all the CP's binding verification needs to fail closed.
      await client.workflow.start("someForeignWorkflow", {
        taskQueue: "conformance-demo-queue",
        workflowId: foreignId,
        args: [],
      });
      const status = await api(
        "GET",
        `/api/v1/workflows/workflow/status?environment_id=local&execution_id=${foreignId}`,
      );
      expect(status.status).toBe(409);
      expect((status.body as { error: string }).error).toBe("LifecycleBindingError");
    } finally {
      await connection.close().catch(() => undefined);
    }
  }, 60_000);
});
