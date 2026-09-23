// TypeScript conformance to the Temporal binding contract (#618 slice 2).
//
// Asserts this SDK's exported wire constants against
// contracts/temporal-binding/binding.v1.json — the normative document. A
// divergence here is a bug in the SDK, not in the document.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { canonicalJson } from "@typeflux/temporal";

import { workflowIdentityMemo, workflowPlanDigest } from "../src/frozen-version.js";
import { disabledLifecycleStatus } from "../src/lifecycle.js";
import { ENCRYPTED_ENCODING, KEY_ID_METADATA_KEY, NONCE_LEN, TAG_LEN } from "../src/payload-codec.js";
import { YAML_WORKFLOW_TYPE } from "../src/runtime.js";
import { PLAN_INTERPRETER_VERSION, type WorkflowPlan } from "../src/workflow-plan.js";
import { lifecycleStatusQuery, requestCancelSignal, submitReviewSignal } from "../src/workflows.js";

const here = dirname(fileURLToPath(import.meta.url));
const binding = JSON.parse(
  readFileSync(
    resolve(here, "../../../../contracts/temporal-binding/binding.v1.json"),
    "utf-8",
  ),
) as {
  shared: {
    memo: Record<string, string>;
    signals: { request_cancel: { name: string }; submit_review: { name: string } };
    queries: { lifecycle_status: { name: string } };
    workflow_lifecycle_status: { fields: string[]; states: string[] };
    payload_codec: {
      algorithm: string;
      encrypted_payload: {
        metadata: { encoding: string; "typeflux-key-id": string };
        data: string;
        data_layout: { nonce: string; tag: string };
      };
    };
  };
  profiles: {
    "ts-plan-argument": {
      workflow_type: { constant: string };
      start_args: string[];
      memo_extra: Record<string, string>;
      digest: { algorithm: string; version_field: string; version_value: number };
    };
  };
};
const profile = binding.profiles["ts-plan-argument"];
const shared = binding.shared;

describe("Temporal binding conformance (#618)", () => {
  it("registers the constant generic workflow type", () => {
    expect(YAML_WORKFLOW_TYPE).toBe(profile.workflow_type.constant);
    expect(profile.start_args).toEqual(["plan", "input"]);
  });

  it("signal and query names match the binding contract", () => {
    expect(requestCancelSignal.name).toBe(shared.signals.request_cancel.name);
    expect(submitReviewSignal.name).toBe(shared.signals.submit_review.name);
    expect(lifecycleStatusQuery.name).toBe(shared.queries.lifecycle_status.name);
  });

  it("identity memo keys match, with the version key only when set", () => {
    const spec = { project: "demo", workflow: { name: "Flow" } } as never;
    const versioned = { project: "demo", workflow: { name: "Flow", version: "v7" } } as never;

    expect(new Set(Object.keys(workflowIdentityMemo(spec, "d".repeat(64))))).toEqual(
      new Set(Object.keys(shared.memo)),
    );
    expect(new Set(Object.keys(workflowIdentityMemo(versioned, "d".repeat(64))))).toEqual(
      new Set([...Object.keys(shared.memo), ...Object.keys(profile.memo_extra)]),
    );
  });

  it("plan digest identifiers match the binding contract", () => {
    const plan = { steps: [] } as unknown as WorkflowPlan;
    const expected = createHash("sha256")
      .update(
        canonicalJson({
          algorithm: profile.digest.algorithm,
          [profile.digest.version_field]: profile.digest.version_value,
          plan,
        }),
        "utf-8",
      )
      .digest("hex");

    expect(PLAN_INTERPRETER_VERSION).toBe(profile.digest.version_value);
    expect(workflowPlanDigest(plan)).toBe(expected);
  });

  it("the disabled lifecycle status carries the full contract field set", () => {
    const status = disabledLifecycleStatus();
    expect(new Set(Object.keys(status))).toEqual(
      new Set(shared.workflow_lifecycle_status.fields),
    );
    expect(status.state).toBe("disabled");
    expect(shared.workflow_lifecycle_status.states).toContain("disabled");
  });

  it("the AES-256-GCM payload codec wire constants match the binding contract (#188)", () => {
    const codec = shared.payload_codec;
    expect(codec.algorithm.startsWith("AES-256-GCM")).toBe(true);
    expect(codec.encrypted_payload.metadata.encoding.split(" ")[0]).toBe(ENCRYPTED_ENCODING);
    expect(codec.encrypted_payload.metadata).toHaveProperty(KEY_ID_METADATA_KEY);
    expect(codec.encrypted_payload.data).toBe("nonce || ciphertext || tag");
    expect(codec.encrypted_payload.data_layout.nonce.startsWith(`${NONCE_LEN} bytes`)).toBe(true);
    expect(codec.encrypted_payload.data_layout.tag.startsWith(`${TAG_LEN}-byte`)).toBe(true);
  });
});
