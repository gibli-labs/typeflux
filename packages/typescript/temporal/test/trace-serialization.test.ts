import { describe, expect, it } from "vitest";

import type { ObservationRecord, TraceRecord } from "../src/index.js";
import { serializeTraceRecord } from "../src/index.js";

// Direct tests for serializeTraceRecord (#390) — Python model_dump(exclude_none=True)
// semantics for already-JSON inputs, tested without going through TraceWriter.

describe("serializeTraceRecord field-level null exclusion", () => {
  it("serializes a minimal record with collection defaults, dropping absent fields", () => {
    expect(serializeTraceRecord({ trace_id: "t-1" })).toEqual({
      trace_id: "t-1",
      metadata: {},
      observations: [],
    });
  });

  it("drops fields that are explicitly null, exactly like absent ones", () => {
    // A backend's JSON can carry explicit nulls; exclude_none treats them as absent.
    const record = { trace_id: "t-1", name: null, timestamp: null } as unknown as TraceRecord;
    expect(serializeTraceRecord(record)).toEqual({ trace_id: "t-1", metadata: {}, observations: [] });
  });

  it("keeps present fields verbatim and omits the retrieval key when unset", () => {
    const out = serializeTraceRecord({
      trace_id: "t-1",
      name: "run",
      timestamp: "2026-01-01T00:00:00Z",
      metadata: { env: "test" },
    });
    expect(out).toEqual({
      trace_id: "t-1",
      name: "run",
      timestamp: "2026-01-01T00:00:00Z",
      metadata: { env: "test" },
      observations: [],
    });
    expect("retrieval" in out).toBe(false);
  });

  it("exclude_none is field-level only: nested nulls inside free-form values survive", () => {
    const out = serializeTraceRecord({
      trace_id: "t-1",
      input: { claim: null, text: "x" },
      output: [null, "y"],
    });
    expect(out.input).toEqual({ claim: null, text: "x" });
    expect(out.output).toEqual([null, "y"]);
    // ...but a null FIELD value is dropped.
    expect("input" in serializeTraceRecord({ trace_id: "t", input: null })).toBe(false);
  });
});

describe("nested observation serialization", () => {
  it("serializes each observation, dropping its null fields and defaulting metadata", () => {
    const observations: ObservationRecord[] = [
      {
        observation_id: "o-1",
        name: "llm-call",
        type: "GENERATION",
        level: "DEFAULT",
        input: { prompt: "p" },
        output: { text: "r" },
        metadata: { model: "m" },
        start_time: "2026-01-01T00:00:00Z",
        end_time: "2026-01-01T00:00:01Z",
      },
      { observation_id: "o-2" }, // everything else absent
    ];
    const out = serializeTraceRecord({ trace_id: "t-1", observations });
    expect(out.observations).toEqual([
      {
        observation_id: "o-1",
        name: "llm-call",
        type: "GENERATION",
        level: "DEFAULT",
        input: { prompt: "p" },
        output: { text: "r" },
        metadata: { model: "m" },
        start_time: "2026-01-01T00:00:00Z",
        end_time: "2026-01-01T00:00:01Z",
      },
      { observation_id: "o-2", metadata: {} },
    ]);
  });

  it("preserves observation order", () => {
    const out = serializeTraceRecord({
      trace_id: "t-1",
      observations: [{ name: "first" }, { name: "second" }, { name: "third" }],
    });
    expect((out.observations as { name: string }[]).map((o) => o.name)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });
});

describe("retrieval info serialization", () => {
  it("keeps falsy-but-present values, defaults warnings to [], drops an absent cursor", () => {
    const out = serializeTraceRecord({
      trace_id: "t-1",
      retrieval: {
        backend: "langfuse",
        complete: false,
        pages_read: 0,
        observations_read: 0,
        page_size: 50,
        max_pages: 20,
      },
    });
    expect(out.retrieval).toEqual({
      backend: "langfuse",
      complete: false, // false is not none — must survive exclude_none
      pages_read: 0,
      observations_read: 0,
      page_size: 50,
      max_pages: 20,
      warnings: [],
    });
  });

  it("emits next_cursor and non-empty warnings when present", () => {
    const out = serializeTraceRecord({
      trace_id: "t-1",
      retrieval: {
        backend: "langfuse",
        complete: false,
        pages_read: 2,
        observations_read: 100,
        page_size: 50,
        max_pages: 2,
        next_cursor: "cursor-3",
        warnings: ["truncated at max_pages"],
      },
    });
    expect(out.retrieval).toMatchObject({
      next_cursor: "cursor-3",
      warnings: ["truncated at max_pages"],
    });
  });
});
