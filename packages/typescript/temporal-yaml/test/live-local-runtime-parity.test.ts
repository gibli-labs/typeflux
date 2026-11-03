import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { cpSync, mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { buildLocalRuntime, typefluxYamlSpec } from "@typeflux/yaml";
import { buildRuntime } from "../src/index.js";

const fixtures = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/execution-cases.json", import.meta.url), "utf8"));
const Item = z.object({ n: z.number().int() });
const schemas = {
  "models:Item": Item,
  "models:Batch": z.object({ items: z.array(Item) }),
  "models:Branches": z.object({ left: Item, right: Item.nullable() }),
};

class NumericProvider implements ModelProvider {
  calls: number[] = [];
  active = 0;
  peak = 0;
  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    const n = Number(params.messages.at(-1)!.content);
    this.calls.push(n);
    this.active++;
    this.peak = Math.max(this.peak, this.active);
    try {
      await new Promise(resolve => setTimeout(resolve, (10 - Math.min(n, 9)) * 5));
      return { n: n + 1 };
    } finally { this.active--; }
  }
}

const delayedFailureRunner = String.raw`
import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { Worker } from "@temporalio/worker";
let reporting = false, released = false, delays = 0, shutdowns = 0;
const create = Worker.create;
const shutdown = Worker.prototype.shutdown;
Worker.create = function(options) {
  const interceptors = options.interceptors ?? {};
  return create.call(this, {
    ...options,
    interceptors: {...interceptors, activity: [
      ...(interceptors.activity ?? []),
      () => ({inbound: {async execute(input, next) {
        try { return await next(input); }
        catch (error) {
          if (error?.message?.includes("demonstration fault; no external side effects")) {
            reporting = true;
            delays++;
            await delay(250);
            released = true;
          }
          throw error;
        }
      }}}),
    ]},
  });
};
Worker.prototype.shutdown = function() {
  shutdowns++;
  const premature = reporting && !released;
  const result = shutdown.call(this);
  assert(!premature, "worker shutdown preceded failure report");
  return result;
};
await import("./recovery-demo.mjs");
assert.equal(delays, 1);
assert(released && shutdowns >= 3);
`;

describe.skipIf(process.env["TYPEFLUX_LIVE_TEMPORAL"] !== "1")("local and Temporal computation parity", () => {
  it("matches shared local execution fixtures on real Temporal workers", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client, Connection } = await import("@temporalio/client");
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    const clientConnection = await Connection.connect({ address: "localhost:7233" });
    const client = new Client({ connection: clientConnection });
    try {
      for (const test of fixtures.cases) {
        const results = [];
        for (const engine of ["local", "temporal"] as const) {
          const queue = `local-parity-${randomUUID()}`;
          const spec = (id: string, workflow: object) => {
            const patch = test.spec_overrides?.[id] ?? {};
            return typefluxYamlSpec.parse({
            ...structuredClone(fixtures.base), ...patch, name: id,
            runtime: { ...structuredClone(fixtures.base.runtime), ...patch.runtime, engine },
            ...(engine === "temporal" ? { task_queue: queue } : {}),
            workflow: { name: id, ...workflow },
          });
          };
          const root = spec("root", test.workflow);
          const children = Object.fromEntries(Object.entries(test.children).map(([id, workflow]) => [id, spec(id, workflow as object)]));
          const provider = new NumericProvider();
          const options = { provider, schemas, subworkflows: { selfId: "root", specFor: (id: string) => children[id] } };
          const build = () => engine === "local" ? buildLocalRuntime(root, options) : buildRuntime(root, {
            ...options, worker: { connection },
            workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
          });
          if (test.error) {
            await expect(build()).rejects.toThrow(test.error);
            expect(provider.calls).toEqual([]);
            results.push("refused");
            continue;
          }
          let result;
          if (engine === "local") {
            result = await (await buildLocalRuntime(root, options)).executeWorkflow(test.input);
          } else {
            const runtime = await buildRuntime(root, {
              ...options, worker: { connection },
              workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
            });
            result = await runtime.worker.runUntil(runtime.runWorkflow(client, test.input, { workflowId: `local-parity-${randomUUID()}` }));
          }
          expect(result, `${engine}: ${test.name}`).toEqual(test.expected);
          results.push(result);
          const unordered = test.call_order === "unordered";
          expect(unordered ? [...provider.calls].sort((a, b) => a - b) : provider.calls, `${engine}: ${test.name}`)
            .toEqual(unordered ? [...test.calls].sort((a, b) => a - b) : test.calls);
          expect(provider.active).toBe(0);
          expect(provider.peak).toBeLessThanOrEqual(2);
        }
        expect(results[0], test.name).toEqual(results[1]);
      }
    } finally {
      await clientConnection.close();
      await connection.close();
    }
  }, 120_000);
  it("executes the documented offline example after switching its environment to Temporal", async () => {
    // Copy the application under the Temporal consumer package, whose dependencies
    // include the integration/client/worker required by the documented upgrade.
    const directory = mkdtempSync(fileURLToPath(new URL("./tmp-adoption-", import.meta.url)));
    try {
      cpSync(fileURLToPath(new URL("../../yaml/examples/local-runtime", import.meta.url)), directory, {recursive: true});
      const { stdout } = await promisify(execFile)(process.execPath, [`${directory}/temporal-sdk.mjs`], {timeout: 60000});
      expect(JSON.parse(stdout)).toEqual({text: "local result"});
    } finally { rmSync(directory, {recursive: true, force: true}); }
  }, 65000);

  it("recovers the documented Temporal run on a replacement worker and enforces activity timeout", async () => {
    const directory = mkdtempSync(fileURLToPath(new URL("./tmp-adoption-", import.meta.url)));
    try {
      cpSync(fileURLToPath(new URL("../../yaml/examples/local-runtime", import.meta.url)), directory, {recursive: true});
      const { stdout } = await promisify(execFile)(process.execPath, [`${directory}/recovery-demo.mjs`], {timeout: 90000});
      expect(JSON.parse(stdout)).toEqual([
        {scenario: "retry", attempts: [1, 2], sameRun: true, retryRecordedBeforeStop: true, status: "COMPLETED", maximumAttempts: 2, startToCloseSeconds: 10, result: {text: "local result"}},
        {scenario: "timeout", attempts: [1], sameRun: true, retryRecordedBeforeStop: false, status: "FAILED", maximumAttempts: 1, startToCloseSeconds: 0.5, result: null},
      ]);
    } finally { rmSync(directory, {recursive: true, force: true}); }
  }, 95000);

  it("waits for a delayed failure report before shutting down the documented worker", async () => {
    const directory = mkdtempSync(fileURLToPath(new URL("./tmp-adoption-", import.meta.url)));
    try {
      cpSync(fileURLToPath(new URL("../../yaml/examples/local-runtime", import.meta.url)), directory, {recursive: true});
      writeFileSync(`${directory}/delayed-report.mjs`, delayedFailureRunner);
      const { stdout } = await promisify(execFile)(process.execPath, [`${directory}/delayed-report.mjs`], {timeout: 90000});
      const observations = JSON.parse(stdout);
      expect(observations[0]).toMatchObject({attempts: [1, 2], sameRun: true, retryRecordedBeforeStop: true});
      expect(observations[1].status).toBe("FAILED");
    } finally { rmSync(directory, {recursive: true, force: true}); }
  }, 95000);

});
