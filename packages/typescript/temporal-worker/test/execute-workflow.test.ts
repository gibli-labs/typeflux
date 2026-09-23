import { describe, expect, it } from "vitest";
import { defineSearchAttributeKey, SearchAttributeType, TypedSearchAttributes } from "@temporalio/common";

import { workflowStartOptions } from "../src/index.js";

describe("workflowStartOptions (#450)", () => {
  it("assembles minimal options with args defaulting to []", () => {
    expect(workflowStartOptions({ workflowType: "wf", taskQueue: "q", workflowId: "id-1" })).toEqual({
      taskQueue: "q",
      workflowId: "id-1",
      args: [],
    });
  });

  it("passes positional args through", () => {
    const o = workflowStartOptions({ workflowType: "wf", taskQueue: "q", workflowId: "id-1", args: [1, "x"] });
    expect(o.args).toEqual([1, "x"]);
  });

  it("merges the startOptions passthrough; core fields win", () => {
    const o = workflowStartOptions({
      workflowType: "wf",
      taskQueue: "q",
      workflowId: "id-1",
      startOptions: { workflowExecutionTimeout: "1m" },
    });
    expect(o.taskQueue).toBe("q");
    expect(o.workflowId).toBe("id-1");
    expect(o.workflowExecutionTimeout).toBe("1m");
  });

  it("throws on a missing taskQueue or workflowId", () => {
    expect(() => workflowStartOptions({ workflowType: "wf", taskQueue: "", workflowId: "id" })).toThrow(
      /taskQueue is required/,
    );
    expect(() => workflowStartOptions({ workflowType: "wf", taskQueue: "q", workflowId: "" })).toThrow(
      /workflowId is required/,
    );
  });
});

describe("keyword search attributes (#495 PR-B, Python _workflow_start_search_attributes)", () => {
  it("builds TypedSearchAttributes pairing the configured key, preserving caller pairs", () => {
    const callerKey = defineSearchAttributeKey("CallerAttr", SearchAttributeType.KEYWORD);
    const conflictKey = defineSearchAttributeKey("TypefluxWorkflow", SearchAttributeType.KEYWORD);
    const options = workflowStartOptions({
      workflowType: "W",
      taskQueue: "q",
      workflowId: "id-1",
      keywordSearchAttributes: { TypefluxWorkflow: "claim_review" },
      startOptions: {
        typedSearchAttributes: new TypedSearchAttributes([
          { key: callerKey, value: "kept" },
          { key: conflictKey, value: "overridden" },
        ]),
      },
    });
    const attrs = options.typedSearchAttributes as TypedSearchAttributes;
    expect(attrs.get(conflictKey)).toBe("claim_review"); // the configured key is authoritative
    expect(attrs.get(callerKey)).toBe("kept"); // caller pairs preserved
  });

  it("omits typedSearchAttributes entirely when none configured", () => {
    const options = workflowStartOptions({ workflowType: "W", taskQueue: "q", workflowId: "id-1" });
    expect("typedSearchAttributes" in options).toBe(false);
  });

  it("builds a KEYWORD_LIST pair for TypefluxSubjectIds alongside a keyword attr (#715)", () => {
    const subjectKey = defineSearchAttributeKey("TypefluxSubjectIds", SearchAttributeType.KEYWORD_LIST);
    const workflowKey = defineSearchAttributeKey("TypefluxWorkflow", SearchAttributeType.KEYWORD);
    const options = workflowStartOptions({
      workflowType: "W",
      taskQueue: "q",
      workflowId: "id-1",
      keywordSearchAttributes: { TypefluxWorkflow: "claim_review" },
      keywordListSearchAttributes: { TypefluxSubjectIds: ["pt-1", "pt-2"] },
    });
    const attrs = options.typedSearchAttributes as TypedSearchAttributes;
    expect(attrs.get(subjectKey)).toEqual(["pt-1", "pt-2"]);
    expect(attrs.get(workflowKey)).toBe("claim_review");
  });
});
