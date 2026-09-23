import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

import { InMemoryCacheStore } from "@typeflux/temporal";

import { buildRuntime, listExecutionsForSubject, loadYamlSpec } from "../src/index.js";

// Subject-index live proof (#715 slice 1). Runs a YAML workflow whose `subjects:`
// block extracts a subject id, then verifies the TypefluxSubjectIds keyword-list
// search attribute is stamped and enumerable, end to end against a dev server:
//   temporal server start-dev
//   temporal operator search-attribute create --name TypefluxSubjectIds --type KeywordList
//   pnpm -r build
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test live-subject-index
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

class AssessProvider implements ModelProvider {
  structuredCall(_params: StructuredCallParams): unknown {
    return { ok: true };
  }
}

const TASK_QUEUE = "typeflux-live-subject";

const SPEC = `
project: claims
name: claim_review
task_queue: ${TASK_QUEUE}
runtime:
  temporal: {}
  registry: { type: inline, prompts: { claims/assess: assess the claim } }
  provider: { type: openai }
activities:
  definitions:
    - name: assess_claim
      input: schemas:ClaimInput
      output: schemas:Assessment
      prompt: claims/assess
workflow:
  name: ClaimReview
  input: schemas:ClaimInput
  output: schemas:Assessment
  subjects:
    - from: input.patient_ref
  steps:
    - id: assess
      activity: assess_claim
`;

/** The SPEC with cross-run caching on, so the run writes a cache record (#715 carrier e2e). */
const CACHED_SPEC = SPEC.replace(
  "      prompt: claims/assess\n",
  "      prompt: claims/assess\n      cross_run_cache: { enabled: true }\n",
);

/** A store recording every written record so the subjects carrier is observable. */
class RecordingStore extends InMemoryCacheStore {
  written: unknown[] = [];
  override set(key: Parameters<InMemoryCacheStore["set"]>[0], record: Parameters<InMemoryCacheStore["set"]>[1]): void {
    this.written.push(record);
    super.set(key, record);
  }
}

const schemas = {
  "schemas:ClaimInput": z.object({ patient_ref: z.string() }),
  "schemas:Assessment": z.object({ ok: z.boolean() }),
};

describe.skipIf(!LIVE)("live subject-index runtime e2e (#715 slice 1)", () => {
  it("stamps TypefluxSubjectIds from the subjects block and enumerates by subject", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(SPEC), {
        provider: new AssessProvider(),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const subjectId = `subject-${Date.now()}`;
      const workflowId = `yaml-subject-${Date.now()}`;

      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { patient_ref: subjectId }, { workflowId }),
      );
      expect(result).toEqual({ ok: true });

      // Stamped: the execution's TypefluxSubjectIds carries the extracted subject.
      const description = await client.workflow.getHandle(workflowId).describe();
      expect(description.searchAttributes?.["TypefluxSubjectIds"]).toEqual([subjectId]);

      // Enumerated: the seam finds the execution by subject (eventually consistent).
      let found: Awaited<ReturnType<typeof listExecutionsForSubject>>["executions"] = [];
      for (let attempt = 0; attempt < 20; attempt += 1) {
        found = (await listExecutionsForSubject(client, subjectId)).executions;
        if (found.length > 0) break;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      expect(found.map((ref) => ref.executionId)).toEqual([workflowId]);
      // Slice 4: the enumeration also carries the row's own subject set + status
      // classification (the delete driver's classification inputs).
      expect(found[0]!.subjectIds).toContain(subjectId);
    } finally {
      await connection.close();
    }
  });

  it("a subjects-declared workflow through the REAL worker writes cache records carrying subjects", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const cacheStore = new RecordingStore();
      const runtime = await buildRuntime(loadYamlSpec(CACHED_SPEC), {
        provider: new AssessProvider(),
        schemas,
        cacheStore,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const subjectId = `subject-cache-${Date.now()}`;
      const workflowId = `yaml-subject-cache-${Date.now()}`;

      // The end-to-end carrier proof the review demanded (#715 finding 1): the
      // workflow interpreter reads its own TypefluxSubjectIds, threads them
      // through the activity's third argument, and the worker-side cache write
      // records them — through the REAL Temporal worker, not a hand-fed fake.
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { patient_ref: subjectId }, { workflowId }),
      );
      expect(result).toEqual({ ok: true });
      expect(cacheStore.written).toHaveLength(1);
      expect((cacheStore.written[0] as { subjects?: string[] }).subjects).toEqual([subjectId]);
    } finally {
      await connection.close();
    }
  });
});
