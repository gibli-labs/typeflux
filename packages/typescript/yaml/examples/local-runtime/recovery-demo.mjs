// Optional Temporal demonstration: the local bindings remain Temporal-free.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { Client, Connection, WorkflowFailedError, WorkflowNotFoundError } from "@temporalio/client";
import { ActivityFailure, TimeoutFailure, TimeoutType } from "@temporalio/common";
import { NativeConnection, Runtime } from "@temporalio/worker";
import { ProviderTransientError } from "@typeflux/temporal";
import { currentActivityContext } from "@typeflux/temporal-worker";
import { buildRuntime } from "@typeflux/temporal-yaml";
import { resolveRuntimeTarget } from "@typeflux/yaml/runtime-target";
import { schemas } from "./bindings.mjs";

const RETRY_SECONDS = 5;
// Keep native Core warnings alongside SDK logs on stderr, preserving JSON stdout.
Runtime.install({telemetryOptions: {logging: {forward: {}}}});
const bounded = (promise, milliseconds) => {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("demo deadline exceeded")), milliseconds); }),
  ]).finally(() => clearTimeout(timer));
};

class RecoveryProvider {
  calls = [];
  constructor(mode) {
    this.mode = mode;
    this.firstFailure = new Promise(resolve => { this.failed = resolve; });
    this.started = new Promise(resolve => { this.called = resolve; });
  }
  async structuredCall() {
    const attempt = currentActivityContext().attempt;
    this.calls.push(attempt);
    this.called();
    if (this.mode === "retry" && attempt === 1) {
      this.failed();
      throw new ProviderTransientError("demonstration fault; no external side effects");
    }
    if (this.mode === "timeout") await delay(2000);
    return {text: "local result"};
  }
}

async function demonstrate(mode) {
  const { spec, ...options } = resolveRuntimeTarget({
    manifest: fileURLToPath(new URL("typeflux.project.yaml", import.meta.url)),
    workflow: "echo", environment: "temporal", policies: [],
  });
  spec.task_queue = `adoption-${randomUUID()}`;
  spec.runtime.provider_retry = {max_attempts: 1};
  spec.runtime.activity_retry = {
    maximum_attempts: mode === "retry" ? 2 : 1,
    initial_interval_seconds: RETRY_SECONDS,
    maximum_interval_seconds: RETRY_SECONDS,
    backoff_coefficient: 1,
  };
  const timeout = mode === "retry" ? 10 : 0.5;
  spec.activities.definitions[0].start_to_close_timeout_seconds = timeout;
  const workerConnection = await NativeConnection.connect({address: spec.runtime.temporal.address});
  const provider = new RecoveryProvider(mode);
  const workflowId = `adoption-${randomUUID()}`;
  let runtime, replacement, clientConnection, handle, primaryFailed = false;
  try {
    clientConnection = await Connection.connect({address: spec.runtime.temporal.address});
    const client = new Client({connection: clientConnection, namespace: spec.runtime.temporal.namespace});
    handle = client.workflow.getHandle(workflowId);
    const worker = {connection: workerConnection, namespace: spec.runtime.temporal.namespace};
    runtime = await buildRuntime(spec, {...options, schemas, provider, worker});
    const resultPromise = runtime.runWorkflow(client, JSON.parse(readFileSync(new URL("input.json", import.meta.url), "utf8")), {workflowId});
    // Attach a handler immediately: timeout failure may arrive before it is awaited.
    resultPromise.catch(() => {});
    // The starter is async; observe the run after its activity starts. A rejected
    // start must also settle this wait, without leaving a polling loop alive.
    const beforePromise = bounded(Promise.race([
      mode === "retry" ? provider.firstFailure : provider.started, resultPromise,
    ]).then(() => handle.describe()), 15000);
    let before, attempts, result;
    if (mode === "retry") {
      before = await runtime.worker.runUntil(beforePromise);
      assert.deepEqual(provider.calls, [1]);
      await delay((RETRY_SECONDS + 0.2) * 1000);
      const waiting = await handle.describe();
      assert.equal(waiting.runId, before.runId);
      assert.equal(waiting.status.name, "RUNNING");
      const recoveredProvider = new RecoveryProvider(mode);
      replacement = await buildRuntime(spec, {...options, schemas, provider: recoveredProvider, worker});
      result = await replacement.worker.runUntil(bounded(resultPromise, 20000));
      assert.deepEqual(result, {text: "local result"});
      attempts = [...provider.calls, ...recoveredProvider.calls];
      assert.deepEqual(attempts, [1, 2]);
    } else {
      await runtime.worker.runUntil(async () => {
        before = await beforePromise;
        await assert.rejects(bounded(resultPromise, 15000), error =>
          error instanceof WorkflowFailedError && error.cause instanceof ActivityFailure && error.cause.cause instanceof TimeoutFailure && error.cause.cause.timeoutType === TimeoutType.START_TO_CLOSE);
      });
      attempts = provider.calls;
      assert.deepEqual(attempts, [1]);
    }
    const after = await handle.describe();
    assert.equal(after.runId, before.runId);
    const history = await handle.fetchHistory();
    const scheduled = history.events.filter(event => event.activityTaskScheduledEventAttributes).map(event => event.activityTaskScheduledEventAttributes);
    assert.equal(scheduled.length, 1);
    const maximumAttempts = scheduled[0].retryPolicy.maximumAttempts;
    assert.equal(maximumAttempts, mode === "retry" ? 2 : 1);
    assert.equal(Number(scheduled[0].retryPolicy.initialInterval.seconds), RETRY_SECONDS);
    assert.equal(Number(scheduled[0].retryPolicy.maximumInterval.seconds), RETRY_SECONDS);
    assert.equal(scheduled[0].retryPolicy.backoffCoefficient, 1);
    const startToCloseSeconds = Number(scheduled[0].startToCloseTimeout.seconds) + scheduled[0].startToCloseTimeout.nanos / 1e9;
    assert.equal(startToCloseSeconds, timeout);
    return {scenario: mode, attempts, sameRun: true, status: after.status.name, maximumAttempts, startToCloseSeconds, result: result ?? null};
  } catch (error) {
    primaryFailed = true;
    throw error;
  } finally {
    const cleanupErrors = [];
    try {
      if (handle && (await handle.describe()).status.name === "RUNNING") await handle.terminate("adoption demo cleanup");
    } catch (error) {
      if (!(error instanceof WorkflowNotFoundError)) cleanupErrors.push(error);
    } finally {
      const cleanup = await Promise.allSettled([
        replacement?.drainObservability(), runtime?.drainObservability(),
        clientConnection?.close(), workerConnection.close(),
      ]);
      cleanupErrors.push(...cleanup.filter(result => result.status === "rejected").map(result => result.reason));
    }
    if (cleanupErrors.length) {
      if (!primaryFailed) throw new AggregateError(cleanupErrors, "adoption demo cleanup failed");
      console.error("Additional demo cleanup failures:", cleanupErrors.map(error => error?.name ?? "unknown error"));
    }
  }
}

console.log(JSON.stringify([await demonstrate("retry"), await demonstrate("timeout")]));
