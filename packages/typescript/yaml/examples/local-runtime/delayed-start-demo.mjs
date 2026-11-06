// Native Temporal delayed start; no scheduling state is added to Local.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { Client, Connection, WorkflowNotFoundError } from "@temporalio/client";
import { NativeConnection, Runtime } from "@temporalio/worker";
import { buildRuntime, YAML_WORKFLOW_TYPE } from "@typeflux/temporal-yaml";
import { resolveRuntimeTarget } from "@typeflux/yaml/runtime-target";
import { schemas, provider } from "./bindings.mjs";

const DELAY_SECONDS = 2;
Runtime.install({telemetryOptions: {logging: {forward: {}}}});
const bounded = (promise, milliseconds) => {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("delayed-start demo deadline exceeded")), milliseconds); }),
  ]).finally(() => clearTimeout(timer));
};
const nanos = timestamp => BigInt(timestamp?.seconds?.toString() ?? "0") * 1_000_000_000n + BigInt(timestamp?.nanos ?? 0);

async function demonstrate() {
  const {spec, ...options} = resolveRuntimeTarget({
    manifest: fileURLToPath(new URL("typeflux.project.yaml", import.meta.url)),
    workflow: "echo", environment: "temporal", policies: [],
  });
  spec.task_queue = `delayed-adoption-${randomUUID()}`;
  const workflowId = `delayed-adoption-${randomUUID()}`;
  const input = JSON.parse(readFileSync(new URL("input.json", import.meta.url), "utf8"));
  let workerConnection, clientConnection, runtime, handle, startedRunId, primaryFailed = false;
  try {
    workerConnection = await NativeConnection.connect({address: spec.runtime.temporal.address});
    clientConnection = await Connection.connect({address: spec.runtime.temporal.address});
    runtime = await buildRuntime(spec, {...options, schemas, provider, worker: {connection: workerConnection, namespace: spec.runtime.temporal.namespace}});
    const client = new Client({
      connection: clientConnection, namespace: spec.runtime.temporal.namespace,
      dataConverter: runtime.dataConverter,
      interceptors: {workflow: [{
        async start(request, next) {
          assert.equal(runtime.worker.getState(), "RUNNING");
          assert.equal(request.workflowType, YAML_WORKFLOW_TYPE);
          assert.equal(request.options.workflowId, workflowId);
          assert.equal(request.options.taskQueue, runtime.taskQueue);
          // This public native-SDK hook preserves Typeflux's complete start
          // preparation, including memo/codecs/subjects/version guards.
          startedRunId = await clientConnection.withDeadline(Date.now() + 5000, () => next({
            ...request,
            options: {...request.options, startDelay: {seconds: DELAY_SECONDS}, workflowRunTimeout: {seconds: 45}},
          }));
          return startedRunId;
        },
      }]},
    });
    handle = client.workflow.getHandle(workflowId);
    // Keep the supported worker lifetime; an available worker still cannot
    // start the first task before the service's recorded backoff expires.
    const result = await runtime.worker.runUntil(async () => bounded(runtime.runWorkflow(client, input, {workflowId}), 20000));
    assert.deepEqual(result, {text: "local result"});
    const after = await bounded(handle.describe(), 3000);
    assert.equal(after.runId, startedRunId);
    assert.equal(after.status.name, "COMPLETED");
    const history = await bounded(client.workflow.getHandle(workflowId, startedRunId).fetchHistory(), 3000);
    const started = history.events[0];
    const backoff = started.workflowExecutionStartedEventAttributes.firstWorkflowTaskBackoff;
    assert.equal(nanos(backoff), BigInt(DELAY_SECONDS) * 1_000_000_000n);
    const scheduled = history.events.find(event => event.workflowTaskScheduledEventAttributes);
    const firstTask = history.events.find(event => event.workflowTaskStartedEventAttributes);
    assert(scheduled && firstTask, "successful execution must have scheduled and started workflow tasks");
    assert(nanos(scheduled.eventTime) - nanos(started.eventTime) >= nanos(backoff));
    assert(nanos(firstTask.eventTime) - nanos(started.eventTime) >= nanos(backoff));
    assert.equal(history.events.filter(event => event.activityTaskScheduledEventAttributes).length, 1);
    return {scenario: "delayed-start", backoffSeconds: DELAY_SECONDS, workerAvailable: true, sameRun: true, scheduledAfterDelay: true, firstTaskStartedAfterDelay: true, status: after.status.name, result};
  } catch (error) {
    primaryFailed = true;
    throw error;
  } finally {
    const cleanupErrors = [];
    try {
      if (handle && (await bounded(handle.describe(), 3000)).status.name === "RUNNING") await bounded(handle.terminate("owned delayed-start demo cleanup"), 3000);
    } catch (error) {
      if (!(error instanceof WorkflowNotFoundError)) cleanupErrors.push(error);
    } finally {
      const cleanup = await Promise.allSettled([runtime?.drainObservability(), clientConnection?.close(), workerConnection?.close()]);
      cleanupErrors.push(...cleanup.filter(result => result.status === "rejected").map(result => result.reason));
    }
    if (cleanupErrors.length) {
      if (!primaryFailed) throw new AggregateError(cleanupErrors, "delayed-start demo cleanup failed");
      console.error("Additional delayed-start demo cleanup failures:", cleanupErrors.map(error => error?.name ?? "unknown error"));
    }
  }
}
console.log(JSON.stringify(await demonstrate()));
