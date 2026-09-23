import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

import {
  buildRuntime,
  buildSubjectAwarePayloadCodec,
  InMemorySubjectKeystore,
  loadYamlSpec,
  SubjectKeyShreddedError,
  SubjectScopedPayloadCodec,
  subjectKid,
  type SubjectBindingClient,
} from "../src/index.js";

// Crypto-shred live proof (#715 slice 4) — the TS mirror of the Python
// `test_live_subject_shred.py`, through the REAL build path (`buildRuntime` with a spec
// declaring `payload_codec.subject_scope` + an injected in-memory keystore):
//
// 1. a workflow started WITH a subject id completes, and its SERVER history payloads
//    (start input+plan, activity results, workflow result — fetched raw with a
//    codec-less plain client) are sealed under the `tfsubj1:` subject kid;
// 2. after `destroySubjectKey`, decoding those payloads — and reading the result
//    through the codec-aware client — throws the DISTINCT SubjectKeyShreddedError,
//    never plaintext; a post-shred start for the same subject fails closed too;
// 3. a NO-subject workflow on the same wired stack keeps the shared kid (`main`) and
//    stays readable before AND after the destroy;
// 4. the visibility-describe fallback: a SECOND codec with a FRESH registry resolves
//    the subjects from the live TypefluxSubjectIds attribute and encodes under the
//    subject kid with no registration (decode itself is kid-driven and never consults
//    the bindings — the encode assertion is the fallback proof).
//
// Run against a dev server:
//   temporal server start-dev
//   temporal operator search-attribute create --name TypefluxSubjectIds --type KeywordList
//   pnpm -r build
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test live-subject-shred
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

// A 32-byte (AES-256) shared key as utf-8 text (same convention as the Python live test).
const CODEC_KEY = "typeflux-live-codec-key-32bytes!";

class AssessProvider implements ModelProvider {
  structuredCall(_params: StructuredCallParams): unknown {
    return { ok: true };
  }
}

const CODEC_BLOCK = `
    payload_codec:
      type: aes
      current: main
      keys:
        - id: main
          value_from: { env: TYPEFLUX_LIVE_CODEC_KEY }
      subject_scope: {}
`;

const SUBJECT_SPEC = `
project: claims
name: claim_review_shred
task_queue: typeflux-live-subject-shred
runtime:
  temporal:
${CODEC_BLOCK}
  registry: { type: inline, prompts: { claims/assess: assess the claim } }
  provider: { type: openai }
activities:
  definitions:
    - name: assess_claim
      input: schemas:ClaimInput
      output: schemas:Assessment
      prompt: claims/assess
workflow:
  name: ClaimReviewShred
  input: schemas:ClaimInput
  output: schemas:Assessment
  subjects:
    - from: input.patient_ref
  steps:
    - id: assess
      activity: assess_claim
`;

/** The same wired stack with NO subjects block — the shared-key control lane. */
const PLAIN_SPEC = SUBJECT_SPEC.replace("name: claim_review_shred", "name: claim_review_shred_plain")
  .replace("task_queue: typeflux-live-subject-shred", "task_queue: typeflux-live-subject-shred-plain")
  .replace("name: ClaimReviewShred", "name: ClaimReviewShredPlain")
  .replace("  subjects:\n    - from: input.patient_ref\n", "");

const schemas = {
  "schemas:ClaimInput": z.object({ patient_ref: z.string() }),
  "schemas:Assessment": z.object({ ok: z.boolean() }),
};

interface RawPayload {
  metadata?: Record<string, Uint8Array | null | undefined> | null;
  data?: Uint8Array | null;
}

function metaText(payload: RawPayload, key: string): string | undefined {
  const value = payload.metadata?.[key];
  return value == null ? undefined : Buffer.from(value).toString("utf8");
}

/** Normalize a raw history payload into the strict `Payload` shape codecs accept. */
function asPayload(payload: RawPayload): { metadata: Record<string, Uint8Array>; data: Uint8Array } {
  const metadata: Record<string, Uint8Array> = {};
  for (const [key, value] of Object.entries(payload.metadata ?? {})) {
    if (value != null) metadata[key] = value;
  }
  return { metadata, data: payload.data ?? new Uint8Array() };
}

/** Raw payloads per history-event kind, fetched with a CODEC-LESS client. */
async function collectHistoryPayloads(
  plainClient: { workflow: { getHandle(id: string): { fetchHistory(): Promise<unknown> } } },
  workflowId: string,
): Promise<Record<"input" | "activityResults" | "result", RawPayload[]>> {
  const history = (await plainClient.workflow.getHandle(workflowId).fetchHistory()) as {
    events?: Array<{
      workflowExecutionStartedEventAttributes?: { input?: { payloads?: RawPayload[] | null } | null } | null;
      activityTaskCompletedEventAttributes?: { result?: { payloads?: RawPayload[] | null } | null } | null;
      workflowExecutionCompletedEventAttributes?: { result?: { payloads?: RawPayload[] | null } | null } | null;
    }> | null;
  };
  const collected: Record<"input" | "activityResults" | "result", RawPayload[]> = {
    input: [],
    activityResults: [],
    result: [],
  };
  for (const event of history.events ?? []) {
    collected.input.push(...(event.workflowExecutionStartedEventAttributes?.input?.payloads ?? []));
    collected.activityResults.push(...(event.activityTaskCompletedEventAttributes?.result?.payloads ?? []));
    collected.result.push(...(event.workflowExecutionCompletedEventAttributes?.result?.payloads ?? []));
  }
  return collected;
}

describe.skipIf(!LIVE)("live subject crypto-shred runtime e2e (#715 slice 4)", () => {
  it("seals subject history under tfsubj1, shreds on destroy, and leaves no-subject runs untouched", async () => {
    process.env["TYPEFLUX_LIVE_CODEC_KEY"] = CODEC_KEY;
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    // Clean up the encrypted executions this test creates: they share the GLOBAL
    // `typefluxYamlWorkflow` type, and leaving encrypted history on the shared dev
    // server breaks other suites' type-wide visibility scans (live-run finding).
    const createdWorkflowIds: string[] = [];
    try {
      const keystore = new InMemorySubjectKeystore();
      const subjectSpec = loadYamlSpec(SUBJECT_SPEC);
      const runtime = await buildRuntime(subjectSpec, {
        provider: new AssessProvider(),
        schemas,
        subjectKeystore: keystore,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const subjectCodec = runtime.dataConverter?.payloadCodecs?.[0];
      expect(subjectCodec).toBeInstanceOf(SubjectScopedPayloadCodec);
      // The codec-aware client (runWorkflow fail-closes without it) and a PLAIN one.
      const client = new Client({ dataConverter: runtime.dataConverter! });
      const plainClient = new Client();

      const subjectId = `subject-${Date.now()}`;
      const workflowId = `yaml-shred-${Date.now()}`;
      createdWorkflowIds.push(workflowId);
      const expectedKid = subjectKid([subjectId]);

      // ---- 1) The subject execution completes through the wired stack. ----
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { patient_ref: subjectId }, { workflowId }),
      );
      expect(result).toEqual({ ok: true });

      // The SERVER history (raw, codec-less read) is sealed under the subject kid —
      // client-encoded start payloads AND worker-encoded activity/workflow results.
      const raw = await collectHistoryPayloads(plainClient, workflowId);
      expect(raw.input.length).toBeGreaterThan(0);
      expect(raw.activityResults.length).toBeGreaterThan(0);
      expect(raw.result.length).toBeGreaterThan(0);
      for (const payloads of Object.values(raw)) {
        for (const payload of payloads) {
          expect(metaText(payload, "encoding")).toBe("binary/encrypted");
          expect(metaText(payload, "typeflux-key-id")).toBe(expectedKid);
          expect(Buffer.from(payload.data ?? new Uint8Array()).includes(subjectId)).toBe(false);
        }
      }

      // Pre-shred sanity: the wired codec opens the on-server payloads.
      const codec = subjectCodec as SubjectScopedPayloadCodec;
      const opened = await codec.decode(raw.input.map(asPayload));
      expect(opened.some((p) => Buffer.from(p.data ?? new Uint8Array()).includes(subjectId))).toBe(true);

      // ---- 4) Visibility-describe fallback: a SECOND codec (fresh registry, same
      //         keystore) resolves the subjects from the live index and ENCODES under
      //         the subject kid with no registration. ----
      const secondCodec = buildSubjectAwarePayloadCodec(
        subjectSpec.runtime.temporal.payload_codec,
        keystore,
      ) as SubjectScopedPayloadCodec;
      expect(secondCodec).not.toBe(codec);
      // The visibility client must be CODEC-AWARE (describe decodes the identity memo,
      // which the codec sealed) — the same reason worker-entry builds its binding
      // client with runtime.dataConverter. A plain client's describe fails on the
      // encrypted memo (live-run finding).
      const secondVisibilityClient = new Client({ dataConverter: { payloadCodecs: [secondCodec] } });
      secondCodec.bindings.bindClient(secondVisibilityClient as unknown as SubjectBindingClient);
      expect(await secondCodec.bindings.resolve(workflowId)).toEqual([subjectId]);
      const sealedProbe = await secondCodec.encode(
        [{ metadata: { encoding: Buffer.from("json/plain") }, data: Buffer.from('{"probe":1}') }],
        { type: "workflow", namespace: "default", workflowId },
      );
      expect(metaText(sealedProbe[0]!, "typeflux-key-id")).toBe(expectedKid);
      // (Its decode of the on-server payloads is kid-driven — keystore sharing, not
      // a fallback proof; see the header comment.)
      expect((await secondCodec.decode(raw.result.map(asPayload))).length).toBe(
        raw.result.length,
      );

      // ---- 3, part A) A NO-subject workflow on the same wired stack keeps the
      //         ordinary shared kid. ----
      const plainRuntime = await buildRuntime(loadYamlSpec(PLAIN_SPEC), {
        provider: new AssessProvider(),
        schemas,
        subjectKeystore: keystore,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const plainRunClient = new Client({ dataConverter: plainRuntime.dataConverter! });
      const plainWorkflowId = `yaml-shred-plain-${Date.now()}`;
      createdWorkflowIds.push(plainWorkflowId);
      const plainResult = await plainRuntime.worker.runUntil(
        plainRuntime.runWorkflow(plainRunClient, { patient_ref: "no-subject-marker" }, { workflowId: plainWorkflowId }),
      );
      expect(plainResult).toEqual({ ok: true });
      const plainRaw = await collectHistoryPayloads(plainClient, plainWorkflowId);
      for (const payloads of Object.values(plainRaw)) {
        for (const payload of payloads) {
          expect(metaText(payload, "encoding")).toBe("binary/encrypted");
          expect(metaText(payload, "typeflux-key-id")).toBe("main");
        }
      }

      // ---- 2) THE SHRED. ----
      expect(keystore.destroySubjectKey(subjectId).keyExisted).toBe(true);

      // Direct decode of the on-server payloads now throws the DISTINCT shred error.
      await expect(codec.decode(raw.input.map(asPayload))).rejects.toThrow(
        SubjectKeyShreddedError,
      );
      await expect(
        secondCodec.decode(raw.result.map(asPayload)),
      ).rejects.toThrow(SubjectKeyShreddedError);

      // Reading the result through the codec-aware client fails with the shred —
      // never plaintext.
      await expect(client.workflow.getHandle(workflowId).result()).rejects.toThrow(/shredded/);

      // A post-shred START for the same subject fails closed at the client-side
      // encode (no key resurrection) — the start never reaches the server.
      await expect(
        runtime.runWorkflow(client, { patient_ref: subjectId }, { workflowId: `yaml-shred-after-${Date.now()}` }),
      ).rejects.toThrow(/shredded/);

      // ---- 3, part B) The no-subject workflow is STILL readable after the destroy. ----
      await expect(plainRunClient.workflow.getHandle(plainWorkflowId).result()).resolves.toEqual({ ok: true });
    } finally {
      try {
        const { Client } = await import("@temporalio/client");
        const cleanupClient = new Client();
        for (const createdId of createdWorkflowIds) {
          await cleanupClient.workflowService
            .deleteWorkflowExecution({
              namespace: "default",
              workflowExecution: { workflowId: createdId },
            })
            .catch(() => undefined); // best-effort cleanup, never masks the test outcome
        }
      } finally {
        await connection.close();
      }
    }
  });
});
