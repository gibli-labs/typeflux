import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { TraceRecord } from "../src/index.js";
import { promptRefToDict, serializeTraceRecord } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "../../../../contracts");

function golden(rel: string): unknown {
  return JSON.parse(readFileSync(resolve(contractsDir, rel), "utf-8"));
}

describe("prompt-ref contract (#390)", () => {
  it("reproduces the golden (version null, label set; promptType not serialized)", () => {
    const dict = promptRefToDict({
      name: "support/classify",
      label: "production",
      promptType: "chat",
    });
    expect(dict).toEqual(golden("prompt-ref/golden/prompt_ref.json"));
    expect("promptType" in dict).toBe(false);
  });

  it("pins a version with a null label", () => {
    expect(promptRefToDict({ name: "x", version: 7 })).toEqual({
      name: "x",
      version: 7,
      label: null,
    });
  });

  it("throws when version and label are both set (mutually exclusive)", () => {
    expect(() => promptRefToDict({ name: "x", version: 3, label: "production" })).toThrow();
  });
});

describe("trace contract (#390)", () => {
  it("reproduces the golden trace record (exclude_none, keeps empty warnings)", () => {
    const record: TraceRecord = {
      trace_id: "trace-golden",
      name: "SupportTriageWorkflow",
      timestamp: "2026-01-01T00:00:00Z",
      metadata: { workflow_name: "SupportTriageWorkflow", environment: "golden" },
      observations: [
        {
          observation_id: "obs-1",
          name: "classify_ticket",
          type: "GENERATION",
          level: "DEFAULT",
          metadata: { activity_name: "classify_ticket" },
          start_time: "2026-01-01T00:00:00Z",
          end_time: "2026-01-01T00:00:01Z",
        },
      ],
      retrieval: {
        backend: "langfuse",
        complete: true,
        pages_read: 1,
        observations_read: 1,
        page_size: 50,
        max_pages: 10,
      },
    };
    expect(serializeTraceRecord(record)).toEqual(golden("trace/golden/trace_record.json"));
  });

  it("drops null/undefined fields but keeps empty collections", () => {
    const out = serializeTraceRecord({ trace_id: "t", metadata: {}, observations: [] });
    expect(out).toEqual({ trace_id: "t", metadata: {}, observations: [] });
  });

  it("keeps nested nulls in metadata/input verbatim (exclude_none is field-level)", () => {
    // Verified against Python: model_dump(exclude_none=True) drops only the
    // model field (the observation's null `input`), not nulls inside free-form
    // metadata/input dicts.
    const out = serializeTraceRecord({
      trace_id: "t",
      metadata: { k: null, ok: 1 },
      input: { a: null },
      observations: [{ name: "x", input: null }],
    });
    expect(out).toEqual({
      trace_id: "t",
      metadata: { k: null, ok: 1 },
      input: { a: null },
      observations: [{ metadata: {}, name: "x" }],
    });
  });
});
