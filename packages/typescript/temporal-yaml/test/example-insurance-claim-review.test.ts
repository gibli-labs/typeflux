/**
 * Executes the shipped insurance-claim-review example on every CI run — the
 * example's REAL typeflux.yaml + schemas + scripted provider, assembled via
 * `assembleYamlRuntime` and invoked directly (no Temporal server), so the
 * documented example can never rot silently.
 */

import { readFileSync } from "node:fs";

import { CollectingObserver } from "@typeflux/temporal";
import { describe, expect, it } from "vitest";

import { ScriptedInsuranceProvider } from "../examples/insurance-claim-review/fakes.js";
import { sampleClaim } from "../examples/insurance-claim-review/sample-claim.js";
import { ClaimReviewPacket, EvidenceReview, schemas } from "../examples/insurance-claim-review/schemas.js";
import { assembleYamlRuntime, loadYamlSpec, workflowPlanFromSpec } from "../src/index.js";

const EXAMPLE_YAML = new URL("../examples/insurance-claim-review/typeflux.yaml", import.meta.url);

describe("insurance-claim-review example (#455)", () => {
  const spec = () =>
    loadYamlSpec(readFileSync(EXAMPLE_YAML, "utf-8"), {
      sourceLabel: "examples/insurance-claim-review/typeflux.yaml",
    });

  it("the shipped YAML loads, derives the documented plan, and wires provider_limits", () => {
    const loaded = spec();
    expect(loaded.workflow.name).toBe("InsuranceClaimReviewWorkflow");
    expect(loaded.runtime.provider_limits?.providers["openai"]?.models["gpt-4o-mini"]).toEqual({
      max_concurrent: 1,
      min_interval_seconds: 0.25,
    });
    expect(workflowPlanFromSpec(loaded).steps.map((step) => step.kind)).toEqual(["map", "activity"]);
  });

  it("both activities run end to end against the scripted provider", async () => {
    const provider = new ScriptedInsuranceProvider();
    const observer = new CollectingObserver();
    const { activities } = assembleYamlRuntime(spec(), { provider, schemas, observer });

    const claim = sampleClaim();
    const reviews = [];
    for (const item of claim.evidence) {
      reviews.push(EvidenceReview.parse(await activities["review_evidence_item"]!(item)));
    }
    expect(reviews).toHaveLength(4);
    expect(reviews[1]!.decision).toBe("needs_follow_up");
    expect(reviews[1]!.risk_signals).toEqual(["duplicate invoice"]);

    const packet = ClaimReviewPacket.parse(
      await activities["consolidate_claim_review"]!({ reviews }),
    );
    expect(packet.recommendation).toBe("investigate");
    expect(packet.approval_required).toBe(true);
    expect(packet.evidence_count).toBe(4);

    // The rendered prompts carried the evidence fields and the collected reviews.
    const firstCall = provider.calls[0]!;
    const rendered = firstCall.messages.map((m) => (typeof m.content === "string" ? m.content : "")).join("\n");
    expect(rendered).toContain("EV-001");
    expect(rendered).toContain("insurance claim evidence reviewer");
    const consolidateCall = provider.calls.at(-1)!;
    const consolidateText = consolidateCall.messages
      .map((m) => (typeof m.content === "string" ? m.content : ""))
      .join("\n");
    // The render fix (#455): the collected batch reaches the model as JSON,
    // never "[object Object]".
    expect(consolidateText).toContain("pending-2");
    expect(consolidateText).not.toContain("[object Object]");

    // The spec's provider_limits MODEL tier actually engaged for the scripted
    // stand-in (it reports the provider name the policy targets — codex).
    const controls = observer.activities.map(
      (a) => (a.metadata as Record<string, Record<string, unknown>>)["typeflux.provider_controls"],
    );
    expect(controls.length).toBeGreaterThan(0);
    for (const entry of controls) {
      expect(entry).toMatchObject({
        policy_source: "model",
        policy_key: "provider:openai/model:gpt-4o-mini",
        max_concurrent: 1,
      });
    }
  });
});
