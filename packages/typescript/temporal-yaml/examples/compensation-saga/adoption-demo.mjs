// Existing Temporal compensation contracts; no Local rollback state or external effects.
import assert from "node:assert/strict";
import {randomUUID} from "node:crypto";
import {readFileSync} from "node:fs";
import {setTimeout as delay} from "node:timers/promises";
import {Client, Connection, WorkflowFailedError, WorkflowNotFoundError} from "@temporalio/client";
import proto from "@temporalio/proto";
import {NativeConnection, Runtime} from "@temporalio/worker";
import {buildRuntime, loadYamlSpec} from "@typeflux/temporal-yaml";
import {typefluxYamlSpec} from "@typeflux/yaml";
import {AdoptionProvider, input, schemas} from "./bindings.mjs";

const RETRY_SECONDS = 5;
const FORWARD_FAILURE = "demonstration forward failure; fake effects only";
Runtime.install({telemetryOptions: {logging: {forward: {}}}});
const bounded = (promise, milliseconds) => {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("compensation demo deadline exceeded")), milliseconds);
  })]).finally(() => clearTimeout(timer));
};
const messages = error => {
  const seen = new Set(), output = [];
  while (error && !seen.has(error)) {seen.add(error); output.push(String(error)); error = error.cause;}
  return output.join(" | ");
};

async function recordedRetry(handle) {
  const deadline = performance.now() + 4000;
  while (true) {
    const remaining = deadline - performance.now();
    assert(remaining > 0, "compensation failure was not acknowledged before worker shutdown");
    const description = await bounded(handle.describe(), Math.min(3000, remaining));
    assert.equal(description.status.name, "RUNNING");
    const pending = description.raw.pendingActivities ?? [];
    if (pending.length === 1 && pending[0].lastFailure) return;
    await delay(50);
  }
}

async function demonstrate(scenario) {
  let spec = loadYamlSpec(readFileSync(new URL("typeflux.yaml", import.meta.url), "utf8"));
  spec.task_queue = `compensation-adoption-${randomUUID()}`;
  spec.runtime.provider_retry = {max_attempts: 1};
  spec.runtime.activity_retry = {maximum_attempts: 1};
  for (const definition of spec.activities.definitions) definition.start_to_close_timeout_seconds = 10;
  spec.workflow.steps[1].compensate.retry = {
    maximum_attempts: scenario === "replacement" ? 2 : 1,
    initial_interval_seconds: RETRY_SECONDS, maximum_interval_seconds: RETRY_SECONDS, backoff_coefficient: 1,
  };
  // Reapply the existing schema defaults after operational policy overrides.
  spec = typefluxYamlSpec.parse(spec);
  const workflowId = `compensation-adoption-${randomUUID()}`;
  const provider = new AdoptionProvider(scenario);
  let workerConnection, clientConnection, runtime, replacement, handle, recoveredProvider, primaryFailed = false;
  try {
    workerConnection = await NativeConnection.connect({address: spec.runtime.temporal.address});
    clientConnection = await Connection.connect({address: spec.runtime.temporal.address});
    const worker = {connection: workerConnection, namespace: spec.runtime.temporal.namespace};
    runtime = await buildRuntime(spec, {schemas, provider, worker});
    let startedRunId, executionFailure;
    const client = new Client({connection: clientConnection, namespace: spec.runtime.temporal.namespace,
      ...(runtime.dataConverter ? {dataConverter: runtime.dataConverter} : {}),
      interceptors: {workflow: [{async start(request, next) {
        startedRunId = await clientConnection.withDeadline(Date.now() + 5000, () => next({
          ...request, options: {...request.options, workflowRunTimeout: proto.google.protobuf.Duration.fromObject({seconds: 45})},
        }));
        return startedRunId;
      }}]},
    });
    handle = client.workflow.getHandle(workflowId);
    let resultPromise, before, status;
    const observeFailure = async () => {
      await assert.rejects(resultPromise, error => error instanceof WorkflowFailedError && messages(error).includes(FORWARD_FAILURE));
      // Query while a compatible worker still polls, including closed-run queries.
      return bounded(handle.query("typeflux_lifecycle_status"), 5000);
    };
    await runtime.worker.runUntil(async () => {
      resultPromise = bounded(runtime.runWorkflow(client, input, {workflowId}), 30000);
      void resultPromise.catch(error => {executionFailure = {error};});
      const deadline = performance.now() + 6000;
      while (startedRunId === undefined) {
        if (executionFailure !== undefined) throw executionFailure.error;
        assert(performance.now() < deadline, "start acknowledgement deadline exceeded");
        await delay(50);
      }
      handle = client.workflow.getHandle(workflowId, startedRunId);
      before = await bounded(handle.describe(), 3000);
      if (scenario === "replacement") await recordedRetry(handle);
      else status = await observeFailure();
    });
    if (scenario === "replacement") {
      assert.deepEqual(provider.calls, [["book_room", 1], ["charge_card", 1], ["fulfill_order", 1], ["refund_card", 1]]);
      await delay((RETRY_SECONDS + 0.2) * 1000);
      const pending = await bounded(handle.describe(), 3000);
      assert.equal(pending.runId, before.runId);
      assert.equal(pending.status.name, "RUNNING");
      recoveredProvider = new AdoptionProvider(scenario);
      replacement = await buildRuntime(spec, {schemas, provider: recoveredProvider, worker});
      status = await replacement.worker.runUntil(observeFailure);
    }
    const calls = [...provider.calls, ...(recoveredProvider?.calls ?? [])];
    assert.deepEqual(calls, [["book_room", 1], ["charge_card", 1], ["fulfill_order", 1], ["refund_card", 1],
      ...(scenario === "replacement" ? [["refund_card", 2]] : []), ["cancel_room", 1]]);
    const outcome = scenario === "partial" ? "partial" : "complete";
    assert.equal(status.state, "failed");
    assert.equal(status.compensation_status, outcome);
    const events = status.events.filter(event => event.event.startsWith("compensation_")).map(event => [event.event, event.step_id]);
    assert.deepEqual(events, [["compensation_started", "charge"],
      [scenario === "partial" ? "compensation_failed" : "compensation_completed", "charge"],
      ["compensation_started", "book"], ["compensation_completed", "book"]]);
    const after = await bounded(handle.describe(), 3000);
    assert.equal(after.runId, before.runId);
    assert.equal(after.status.name, "FAILED");
    const history = await bounded(handle.fetchHistory(), 5000);
    assert.equal(history.events.filter(event => event.activityTaskScheduledEventAttributes).length, 5);
    const writes = Object.fromEntries(["book_room", "charge_card"].map(name => [name, provider.forwardFakeWrites[name] + (recoveredProvider?.forwardFakeWrites[name] ?? 0)]));
    assert.deepEqual(writes, {book_room: 1, charge_card: 1});
    return {scenario, sameRun: true, originalFailure: true, compensationStatus: outcome,
      actions: calls, forwardFakeWrites: writes, retryRecordedBeforeStop: scenario === "replacement"};
  } catch (error) {
    primaryFailed = true;
    throw error;
  } finally {
    const cleanupErrors = [];
    try {
      if (handle && (await bounded(handle.describe(), 3000)).status.name === "RUNNING") await bounded(handle.terminate("owned compensation demo cleanup"), 3000);
    } catch (error) {
      if (!(error instanceof WorkflowNotFoundError)) cleanupErrors.push(error);
    } finally {
      const cleanup = await Promise.allSettled([runtime?.drainObservability(), replacement?.drainObservability(), clientConnection?.close(), workerConnection?.close()]);
      cleanupErrors.push(...cleanup.filter(result => result.status === "rejected").map(result => result.reason));
    }
    if (cleanupErrors.length) {
      if (!primaryFailed) throw new AggregateError(cleanupErrors, "compensation demo cleanup failed");
      console.error("Additional compensation demo cleanup failed");
    }
  }
}
const observations = [];
for (const scenario of ["complete", "partial", "replacement"]) observations.push(await demonstrate(scenario));
console.log(JSON.stringify(observations));
