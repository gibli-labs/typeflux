/** Trusted SDK review caller; operator permissions use the control-plane journey. */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { Client, Connection, WorkflowNotFoundError } from "@temporalio/client";
import proto from "@temporalio/proto";
import { NativeConnection } from "@temporalio/worker";
import { buildRuntime, loadYamlSpec, type WorkflowLifecycleStatus } from "../../src/index.js";
import { LifecycleDemoProvider } from "./fakes.js";
import { hooks } from "./hooks.js";
import { sampleCase } from "./sample-case.js";
import { FinalDecision, schemas } from "./schemas.js";

const DECISION = process.env["LIFECYCLE_REVIEW_DECISION"] ?? "send_email";
const routes = {prepare_submission: "prepare_submission", route_department: "route_to_department", send_email: "send_email"};
const steps = ["assess_case", "package_for_review", "prepare_submission", "route_to_department", "send_email"];
const bounded = <T>(promise: Promise<T>, milliseconds: number): Promise<T> => {
  let timer: ReturnType<typeof setTimeout>;
  return Promise.race([promise, new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("lifecycle review demo deadline exceeded")), milliseconds);
  })]).finally(() => clearTimeout(timer));
};

async function main(): Promise<void> {
  assert(Object.hasOwn(routes, DECISION), "choose a configured review decision");
  const route = routes[DECISION as keyof typeof routes];
  const spec = loadYamlSpec(readFileSync(new URL("./typeflux.yaml", import.meta.url), "utf-8"));
  spec.task_queue = `lifecycle-review-demo-${randomUUID()}`;
  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const namespace = process.env["TEMPORAL_NAMESPACE"] ?? "default";
  let workerConnection: NativeConnection | undefined;
  let clientConnection: Connection | undefined;
  let runtime: Awaited<ReturnType<typeof buildRuntime>> | undefined;
  let handle: ReturnType<Client["workflow"]["getHandle"]> | undefined;
  let primaryFailed = false;
  try {
    workerConnection = await NativeConnection.connect({address});
    clientConnection = await Connection.connect({address});
    const built = await buildRuntime(spec, {
      provider: new LifecycleDemoProvider(), schemas, hooks,
      worker: {connection: workerConnection, namespace},
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    runtime = built;
    let startedRunId: string | undefined;
    const client = new Client({
      connection: clientConnection, namespace,
      ...(built.dataConverter === undefined ? {} : {dataConverter: built.dataConverter}),
      interceptors: {workflow: [{async start(request, next) {
        startedRunId = await clientConnection!.withDeadline(Date.now() + 5000, () => next({
          ...request, options: {...request.options, workflowRunTimeout: proto.google.protobuf.Duration.fromObject({seconds: 45})},
        }));
        return startedRunId;
      }}]},
    });
    const workflowId = `lifecycle-review-${randomUUID()}`;
    await built.worker.runUntil(async () => {
      // Retain normal Typeflux memo/codecs/subjects/version start preparation.
      const resultPromise = bounded(built.runWorkflow(client, sampleCase(), {workflowId}), 30000);
      // Handle rejection immediately and preserve its cause while observing the gate.
      let executionFailure: {error: unknown} | undefined;
      void resultPromise.catch(error => { executionFailure = {error}; });
      handle = client.workflow.getHandle(workflowId);
      const deadline = Date.now() + 12000;
      let waiting: WorkflowLifecycleStatus | undefined;
      while (Date.now() < deadline) {
        if (executionFailure !== undefined) throw executionFailure.error;
        // Before the start ACK there is no bound run to query. Do not retry a
        // start or swallow query/backend failures once that ACK exists.
        if (startedRunId !== undefined) {
          handle = client.workflow.getHandle(workflowId, startedRunId);
          waiting = await bounded(handle.query<WorkflowLifecycleStatus>("typeflux_lifecycle_status"), 3000);
          if (waiting.state === "waiting_for_review") break;
        }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      assert.equal(waiting?.state, "waiting_for_review", "gate must be observed before any decision");
      assert.equal(waiting.waiting_checkpoint, "package_for_review");
      assert.equal(waiting.completed_units, 2);
      await clientConnection!.withDeadline(Date.now() + 5000, () => handle!.signal("typeflux_submit_review", {user_decision: DECISION}));
      const result = FinalDecision.parse(await resultPromise);
      assert.equal(result.approved, true);
      assert.equal(result.decision, "approved");
      const completed = await bounded(handle.query<WorkflowLifecycleStatus>("typeflux_lifecycle_status"), 3000);
      assert.equal(completed.state, "completed");
      assert.equal(completed.review_route_target, route);
      const expected = [...steps.slice(0, 2), ...steps.slice(steps.indexOf(route))];
      const ran = completed.events.filter(event => event.event === "step_started").map(event => event.step_id);
      assert.deepEqual(ran, expected);
      assert.equal(completed.completed_units, expected.length);
      assert.equal(completed.total_units, expected.length);
      const after = await bounded(handle.describe(), 3000);
      assert.equal(after.runId, startedRunId);
      assert.equal(after.status.name, "COMPLETED");
      console.log(JSON.stringify({scenario: "review-adoption", sameRun: true,
        waitingCheckpoint: waiting.waiting_checkpoint, reviewRoute: completed.review_route_target,
        state: completed.state, stepsStarted: ran, result}));
    });
  } catch (error) {
    primaryFailed = true;
    throw error;
  } finally {
    const cleanupErrors: unknown[] = [];
    try {
      if (handle && (await bounded(handle.describe(), 3000)).status.name === "RUNNING") await bounded(handle.terminate("owned lifecycle review demo cleanup"), 3000);
    } catch (error) {
      if (!(error instanceof WorkflowNotFoundError)) cleanupErrors.push(error);
    } finally {
      const cleanup = await Promise.allSettled([runtime?.drainObservability(), clientConnection?.close(), workerConnection?.close()]);
      cleanupErrors.push(...cleanup.filter(result => result.status === "rejected").map(result => result.reason));
    }
    if (cleanupErrors.length) {
      if (!primaryFailed) throw new AggregateError(cleanupErrors, "lifecycle review demo cleanup failed");
      console.error("Additional lifecycle review demo cleanup failed");
    }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
