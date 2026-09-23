/**
 * Executes the shipped policy-governed-review example on every CI run — the
 * example's REAL policies + typeflux.yaml + scripted provider, composed and
 * enforced via the same public API the runnable `main.ts` uses (no Temporal
 * server), so the governance demo can never rot silently.
 */

import { readFileSync } from "node:fs";

import { CollectingObserver } from "@typeflux/temporal";
import { describe, expect, it } from "vitest";

import { contentSafetyModerator, ScriptedReviewProvider } from "../examples/policy-governed-review/fakes.js";
import { sampleContent } from "../examples/policy-governed-review/sample-input.js";
import { schemas } from "../examples/policy-governed-review/schemas.js";
import {
  assembleYamlRuntime,
  composeProjectPolicies,
  loadPolicySpec,
  loadYamlSpec,
  ProjectPolicyEnforcementError,
  validatePolicyCompliance,
  type ComposedProjectPolicy,
} from "../src/index.js";

const dir = (file: string) => new URL(`../examples/policy-governed-review/${file}`, import.meta.url);
const loadPolicy = (file: string) =>
  loadPolicySpec(readFileSync(dir(`policies/${file}`), "utf-8"), { sourceLabel: `policies/${file}` });
const loadSpec = (file: string) => loadYamlSpec(readFileSync(dir(file), "utf-8"), { sourceLabel: file });

function composedPolicy(): ComposedProjectPolicy {
  const org = { id: "acme-org", spec: loadPolicy("org.yaml") };
  const tenant = { id: "acme-eu-tenant", spec: loadPolicy("tenant.yaml") };
  return composeProjectPolicies([org, tenant], [org.id, tenant.id]);
}

describe("policy-governed-review example (#454)", () => {
  it("the org + tenant policies compose to the intersection (gpt-4o and anthropic dropped)", () => {
    const policy = composedPolicy();
    expect(policy.appliedPolicyIds).toEqual(["acme-org", "acme-eu-tenant"]);
    expect(policy.policyHash).toMatch(/^[0-9a-f]{64}$/);
    const allowed = (policy.payload as { providers: { allowed: Record<string, { models: string[] }> } }).providers
      .allowed;
    expect(Object.keys(allowed)).toEqual(["openai"]);
    expect(allowed["openai"]?.models).toEqual(["gpt-4o-mini"]);
  });

  it("the compliant spec assembles under the policy pre-flight and the activity runs", async () => {
    const policy = composedPolicy();
    const compliant = loadSpec("typeflux.yaml");

    // Every governed dimension passes; unconstrained ones skip.
    const checks = validatePolicyCompliance(compliant, policy);
    const status = (code: string) => checks.find((c) => c.code === code)?.status;
    expect(status("policy_provider")).toBe("passed");
    expect(status("policy_observability")).toBe("passed");
    expect(status("policy_provider_limits")).toBe("passed");
    expect(status("policy_imports")).toBe("skipped"); // TS injection model — no import surface
    expect(checks.some((c) => c.status === "failed")).toBe(false);

    const { activities } = assembleYamlRuntime(compliant, {
      provider: new ScriptedReviewProvider(),
      schemas,
      policy,
      // The org policy sets observability.required, and #756 fail-closes assembly when no
      // observer resolves — the offline example satisfies it with an injected observer (the
      // documented explicit-observer rule; a real run exports the langfuse keys instead).
      observer: new CollectingObserver(),
    });
    const assessment = schemas["schemas:Assessment"].parse(await activities["assess_content"]!(sampleContent()));
    expect(assessment.category).toBe("review");
    expect(assessment.id).toBe("msg-1001");
  });

  it("the rogue spec is refused fail-closed on BOTH violated dimensions", () => {
    const policy = composedPolicy();
    const rogue = loadSpec("typeflux.rogue.yaml");

    const checks = validatePolicyCompliance(rogue, policy);
    expect(checks.find((c) => c.code === "policy_provider")?.status).toBe("failed");
    expect(checks.find((c) => c.code === "policy_observability")?.status).toBe("failed");

    // buildRuntime's pre-flight (assembleYamlRuntime runs it first) throws before building.
    try {
      assembleYamlRuntime(rogue, { provider: new ScriptedReviewProvider(), schemas, policy });
      expect.unreachable("the rogue spec must be refused by the policy pre-flight");
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectPolicyEnforcementError);
      const failed = (error as ProjectPolicyEnforcementError).checks.filter((c) => c.status === "failed");
      expect(failed.map((c) => c.code).sort()).toEqual(["policy_observability", "policy_provider"]);
    }
  });

  it("the moderated spec PASSES admission but its output is blocked at the runtime checkpoint (slice 3)", async () => {
    const policy = composedPolicy();
    const moderated = loadSpec("typeflux.moderated.yaml");

    // semantics.categories is a runtime control — admission does not fail on it.
    const checks = validatePolicyCompliance(moderated, policy);
    expect(checks.find((c) => c.code === "policy_semantics")?.status).toBe("skipped");
    expect(checks.some((c) => c.status === "failed")).toBe(false);

    // The runtime guard is wired: the lenient moderator only flags, but the policy
    // escalates its reported category to a non-retryable block at execution.
    const observer = new CollectingObserver();
    const { activities } = assembleYamlRuntime(moderated, {
      provider: new ScriptedReviewProvider(),
      schemas,
      policy,
      observer,
      moderators: { assess_content: contentSafetyModerator },
    });
    const error = await activities["assess_content"]!(sampleContent()).then(
      () => undefined,
      (e: unknown) => e as { message: string; type?: string; nonRetryable?: boolean },
    );
    expect(error?.message).toMatch(/disallowed category self_harm/);
    expect(error?.type).toBe("ModerationBlockedError");
    expect(error?.nonRetryable).toBe(true);
    // The escalated verdict is recorded on the trace despite the block (audit evidence).
    expect(observer.activities.at(-1)?.metadata["typeflux_moderation"]).toMatchObject({
      decision: "block",
      categories: ["self_harm"],
    });
  });
});
