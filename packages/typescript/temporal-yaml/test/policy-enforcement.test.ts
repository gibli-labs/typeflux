import { describe, expect, it } from "vitest";

import {
  composeProjectPolicies,
  enforcePolicyCompliance,
  loadPolicySpec,
  loadYamlSpec,
  ProjectPolicyEnforcementError,
  RuntimePolicyGuard,
  validatePolicyCompliance,
  type ComposedProjectPolicy,
} from "../src/index.js";

/** A minimal, valid workflow spec with overridable runtime blocks. */
function workflowSpec(runtime: string, activities?: string): string {
  return `
project: p
name: w
task_queue: q
runtime:
${runtime}
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
${activities ?? ""}
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      activity: a
`;
}

const policyOf = (yaml: string): ComposedProjectPolicy =>
  composeProjectPolicies([{ id: "org", spec: loadPolicySpec(`name: org\n${yaml}`) }], ["org"]);

const check = (checks: ReturnType<typeof validatePolicyCompliance>, code: string) =>
  checks.find((c) => c.code === code);

/**
 * A fully-parameterized spec: `runtime` is an indented block, `moderation`
 * appends to the single activity definition, and `lifecycle` appends under the
 * workflow — enough surface to exercise every require-side validator.
 */
function fullSpec(opts: { runtime: string; defExtra?: string; lifecycle?: string }): string {
  return `
project: p
name: w
task_queue: q
runtime:
${opts.runtime}
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
${opts.defExtra ?? ""}
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      activity: a
${opts.lifecycle ?? ""}
`;
}

/** The baseline runtime the require-side tests layer their own blocks onto. */
const BASE_RUNTIME = "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }";

describe("policy enforcement — allow-lists (#454, fail-closed)", () => {
  it("provider/model allow-list: allowed passes, disallowed provider + disallowed model fail", () => {
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");

    const ok = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }"),
    );
    expect(check(validatePolicyCompliance(ok, policy), "policy_provider")?.status).toBe("passed");
    // The static allow-list marker check is present + passed (Python parity, right after policy_selection).
    expect(check(validatePolicyCompliance(ok, policy), "policy_allowlists")?.status).toBe("passed");
    expect(() => enforcePolicyCompliance(ok, policy)).not.toThrow();

    const badModel = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, model: gpt-4o }"),
    );
    expect(() => enforcePolicyCompliance(badModel, policy)).toThrow(/model 'gpt-4o' is not allowed/);

    const badProvider = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: anthropic, model: gpt-4o-mini }"),
    );
    expect(() => enforcePolicyCompliance(badProvider, policy)).toThrow(/provider 'anthropic' is not allowed/);
  });

  it("a provider with no models constraint allows any model of that provider", () => {
    const policy = policyOf("providers: { allowed: { openai: {} } }");
    const spec = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, model: anything }"),
    );
    expect(() => enforcePolicyCompliance(spec, policy)).not.toThrow();
  });

  it("registry host allow-list: allowed host passes, other host fails, inline skips", () => {
    const policy = policyOf("runtime: { registry: { allowed_hosts: [cloud.langfuse.com] } }");

    const inline = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }"),
    );
    expect(check(validatePolicyCompliance(inline, policy), "policy_registry")?.status).toBe("passed");

    const good = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: langfuse, host: cloud.langfuse.com }\n  provider: { type: openai }"),
    );
    expect(() => enforcePolicyCompliance(good, policy)).not.toThrow();

    const bad = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: langfuse, host: evil.example.com }\n  provider: { type: openai }"),
    );
    expect(() => enforcePolicyCompliance(bad, policy)).toThrow(/host 'evil.example.com' is not allowed/);

    const noHost = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: langfuse }\n  provider: { type: openai }"),
    );
    expect(() => enforcePolicyCompliance(noHost, policy)).toThrow(/no registry host is configured/);
  });

  it("artifacts: source/media/max_bytes allow-lists are enforced (runtime + per-activity)", () => {
    const policy = policyOf(
      "artifacts: { allowed_sources: [local_path], allowed_media_types: ['image/*'], max_bytes: 1000 }",
    );
    const base = "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }";

    const good = loadYamlSpec(
      workflowSpec(
        `${base}\n  artifacts: { local_roots: ['/tmp'], allowed_sources: [local_path], allowed_media_types: ['image/png'], max_bytes: 500 }`,
      ),
    );
    expect(() => enforcePolicyCompliance(good, policy)).not.toThrow();

    const badSource = loadYamlSpec(
      workflowSpec(`${base}\n  artifacts: { allowed_sources: [url], allowed_media_types: ['image/png'], max_bytes: 500 }`),
    );
    expect(() => enforcePolicyCompliance(badSource, policy)).toThrow(/artifact source 'url' is not allowed/);

    const badMedia = loadYamlSpec(
      workflowSpec(`${base}\n  artifacts: { allowed_sources: [local_path], allowed_media_types: ['application/pdf'], max_bytes: 500 }`),
    );
    expect(() => enforcePolicyCompliance(badMedia, policy)).toThrow(/media type 'application\/pdf' is not allowed/);

    const badBytes = loadYamlSpec(
      workflowSpec(`${base}\n  artifacts: { allowed_sources: [local_path], allowed_media_types: ['image/png'], max_bytes: 5000 }`),
    );
    expect(() => enforcePolicyCompliance(badBytes, policy)).toThrow(/max_bytes 5000 exceeds 1000/);
  });

  it("resolves the EFFECTIVE model (params.model / per-type default), not just provider.model (Bugbot)", () => {
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    // Model set via params, not provider.model — must resolve to the effective value.
    const paramsModel = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, params: { model: gpt-4o } }"),
    );
    expect(() => enforcePolicyCompliance(paramsModel, policy)).toThrow(/model 'gpt-4o' is not allowed/);
    // No model anywhere → falls back to the openai per-type default (gpt-4o-mini), which IS allowed.
    const defaulted = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }"),
    );
    expect(() => enforcePolicyCompliance(defaulted, policy)).not.toThrow();
  });

  it("an OMITTED runtime.artifacts is validated against its defaults — a source policy can't be bypassed (Bugbot High)", () => {
    // Policy forbids local_path; a workflow with NO artifacts block still declares
    // the default source local_path, so it must FAIL, not silently pass.
    const policy = policyOf("artifacts: { allowed_sources: [url] }");
    const noArtifacts = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }"),
    );
    expect(() => enforcePolicyCompliance(noArtifacts, policy)).toThrow(/artifact source 'local_path' is not allowed/);
    // When the policy DOES allow local_path, the same omitted-block workflow passes.
    expect(() => enforcePolicyCompliance(noArtifacts, policyOf("artifacts: { allowed_sources: [local_path] }"))).not.toThrow();
  });

  it("a provider named like an Object.prototype key is NOT silently admitted (/code-review fail-open)", () => {
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    for (const proto of ["toString", "constructor", "valueOf", "hasOwnProperty"]) {
      const spec = loadYamlSpec(
        workflowSpec(`  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: ${proto} }`),
      );
      expect(() => enforcePolicyCompliance(spec, policy)).toThrow(new RegExp(`provider '${proto}' is not allowed`));
    }
  });

  it("an empty-string provider.model falls through to the effective default (truthiness parity)", () => {
    // `model: ${VAR:-}` with VAR unset loads as "" — the runtime uses the openai
    // default (gpt-4o-mini), so enforcement must validate the default, not "".
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    const emptyModel = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, model: '' }"),
    );
    expect(() => enforcePolicyCompliance(emptyModel, policy)).not.toThrow();
    // …and if that default is NOT allowed, it fails on the default, not on "".
    const strict = policyOf("providers: { allowed: { openai: { models: [o1] } } }");
    expect(() => enforcePolicyCompliance(emptyModel, strict)).toThrow(/model 'gpt-4o-mini' is not allowed/);
  });

  it("checks SKIP dimensions the policy does not constrain", () => {
    const policy = policyOf("providers: { allowed: { openai: {} } }"); // only providers governed
    const spec = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: langfuse, host: anything }\n  provider: { type: openai }"),
    );
    const checks = validatePolicyCompliance(spec, policy);
    expect(check(checks, "policy_registry")?.status).toBe("skipped");
    expect(check(checks, "policy_artifacts")?.status).toBe("skipped");
    expect(check(checks, "policy_provider")?.status).toBe("passed");
  });

  it("the error carries the failed checks for evidence", () => {
    const policy = policyOf("providers: { allowed: { openai: {} } }");
    const spec = loadYamlSpec(
      workflowSpec("  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: gemini }"),
    );
    try {
      enforcePolicyCompliance(spec, policy);
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectPolicyEnforcementError);
      const checks = (error as ProjectPolicyEnforcementError).checks;
      expect(checks.find((c) => c.code === "policy_provider")?.status).toBe("failed");
    }
  });
});

describe("policy enforcement — require-side posture (#454 slice 2, fail-closed)", () => {
  it("secrets: require_secret_references rejects literal credentials, accepts value_from", () => {
    const policy = policyOf("secrets: { require_secret_references: true }");
    const literal = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, api_key: sk-hardcoded }",
      }),
    );
    expect(() => enforcePolicyCompliance(literal, policy)).toThrow(/runtime.provider.api_key must use a value_from/);

    const referenced = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, api_key: { value_from: { env: OPENAI_API_KEY } } }",
      }),
    );
    expect(() => enforcePolicyCompliance(referenced, policy)).not.toThrow();
    // No secrets policy → the dimension is skipped.
    const noPolicy = policyOf("providers: { allowed: { openai: {} } }");
    expect(check(validatePolicyCompliance(literal, noPolicy), "policy_secrets")?.status).toBe("skipped");
  });

  it("secrets: literal observability credentials fail like literal api_keys (#793)", () => {
    const policy = policyOf("secrets: { require_secret_references: true }");
    const literal = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n" +
          "  provider: { type: openai, api_key: { value_from: { env: OPENAI_API_KEY } } }\n" +
          "  observability:\n    type: langfuse\n    langfuse: { public_key: pk-literal, secret_key: sk-live-hardcoded }",
      }),
    );
    expect(() => enforcePolicyCompliance(literal, policy)).toThrow(
      /runtime\.observability\.langfuse\.public_key must use a value_from/,
    );
  });

  it("observability: backend required + redaction required, defaults read as ON (Python parity)", () => {
    const required = policyOf("observability: { required: true }");
    const none = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(none, required)).toThrow(/observability backend is required/);

    const withBackend = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  observability: { type: langfuse }` }),
    );
    expect(() => enforcePolicyCompliance(withBackend, required)).not.toThrow();

    // allowed_backends excludes the configured backend → fail (the policy enum is
    // {none, langfuse}, so a `[none]`-only allow-list rejects a langfuse backend).
    const restricted = policyOf("observability: { allowed_backends: [none] }");
    expect(() => enforcePolicyCompliance(withBackend, restricted)).toThrow(
      /observability backend 'langfuse' is not allowed/,
    );

    // redaction.required: an EXPLICIT enabled:false fails; an unset field defaults ON.
    const redactionRequired = policyOf("observability: { redaction: { required: true } }");
    const redactionOff = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  observability: { type: langfuse, redaction: { enabled: false } }` }),
    );
    expect(() => enforcePolicyCompliance(redactionOff, redactionRequired)).toThrow(
      /observability redaction is required/,
    );
    const redactionDefault = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  observability: { type: langfuse }` }),
    );
    expect(() => enforcePolicyCompliance(redactionDefault, redactionRequired)).not.toThrow();
  });

  it("temporal: namespace/address allow-lists, require_tls and require_api_key", () => {
    const nsPolicy = policyOf("runtime: { temporal: { allowed_namespaces: [prod] } }");
    const prod = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { namespace: prod }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(prod, nsPolicy)).not.toThrow();
    const dev = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { namespace: dev }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(dev, nsPolicy)).toThrow(/Temporal namespace 'dev' is not allowed/);

    const tlsPolicy = policyOf("runtime: { temporal: { require_tls: true } }");
    const tlsOff = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { tls: false }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(tlsOff, tlsPolicy)).toThrow(/Temporal TLS is required/);
    const tlsOn = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { tls: true }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(tlsOn, tlsPolicy)).not.toThrow();

    // require_api_key: a value_from whose env var resolves passes; an unresolved one fails.
    const keyPolicy = policyOf("runtime: { temporal: { require_api_key: true } }");
    process.env["TF_TEST_TEMPORAL_KEY"] = "resolved-secret";
    const withKey = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: { api_key: { value_from: { env: TF_TEST_TEMPORAL_KEY } } }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }",
      }),
    );
    expect(() => enforcePolicyCompliance(withKey, keyPolicy)).not.toThrow();
    delete process.env["TF_TEST_TEMPORAL_KEY"];
    const missingKey = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: { api_key: { value_from: { env: TF_TEST_TEMPORAL_KEY_UNSET } } }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }",
      }),
    );
    expect(() => enforcePolicyCompliance(missingKey, keyPolicy)).toThrow(/Temporal API key is required/);
  });

  it("temporal: require_payload_codec rejects a codec-less spec, accepts a declared codec (#188 D188-2)", () => {
    const policy = policyOf("runtime: { temporal: { require_payload_codec: true } }");
    const noCodec = loadYamlSpec(
      fullSpec({ runtime: "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(noCodec, policy)).toThrow(
      /Temporal payload codec is required by project policy/,
    );
    const withCodec = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal:\n    payload_codec:\n      type: aes\n      current: k1\n      keys:\n        - { id: k1, value_from: { env: TF_CODEC_KEY } }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }",
      }),
    );
    expect(() => enforcePolicyCompliance(withCodec, policy)).not.toThrow();
  });

  it("observability: require_custom_rules fails-closed listing missing names, unions on composition (#188 D188-4)", () => {
    const policy = policyOf(
      "observability: { redaction: { require_custom_rules: [case_id, iban] } }",
    );
    const withRules = loadYamlSpec(
      fullSpec({
        runtime: `${BASE_RUNTIME}\n  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: case_id, pattern: 'CASE-\\d{6}', replacement: '[C]' }\n        - { name: iban, pattern: 'GB\\d{2}', replacement: '[I]' }`,
      }),
    );
    expect(() => enforcePolicyCompliance(withRules, policy)).not.toThrow();

    const missing = loadYamlSpec(
      fullSpec({
        runtime: `${BASE_RUNTIME}\n  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: case_id, pattern: 'CASE-\\d{6}', replacement: '[C]' }`,
      }),
    );
    expect(() => enforcePolicyCompliance(missing, policy)).toThrow(
      /missing required custom rules: iban/,
    );

    // OR-merge = union of required names across composed policies.
    const merged = composeProjectPolicies(
      [
        { id: "org", spec: loadPolicySpec("name: org\nobservability: { redaction: { require_custom_rules: [case_id] } }") },
        { id: "tenant", spec: loadPolicySpec("name: tenant\nobservability: { redaction: { require_custom_rules: [iban] } }") },
      ],
      ["org", "tenant"],
    );
    const required = (merged.payload as { observability?: { redaction?: { require_custom_rules?: string[] } } })
      .observability?.redaction?.require_custom_rules;
    expect([...(required ?? [])].sort()).toEqual(["case_id", "iban"]);
  });

  it("observability: require_custom_rules implies redaction MUST run (enabled:false rejected) (#188)", () => {
    const policy = policyOf("observability: { redaction: { require_custom_rules: [case_id] } }");
    // The named rule is present but redaction is disabled → the whole redactor no-ops, so
    // requiring the rule while it never runs is fail-open. Must be REJECTED, naming both knobs.
    const disabled = loadYamlSpec(
      fullSpec({
        runtime: `${BASE_RUNTIME}\n  observability:\n    type: langfuse\n    redaction:\n      enabled: false\n      custom_rules:\n        - { name: case_id, pattern: 'CASE-\\d{6}', replacement: '[C]' }`,
      }),
    );
    expect(() => enforcePolicyCompliance(disabled, policy)).toThrow(
      /requires custom redaction rules \[case_id\] but observability.redaction.enabled is false/,
    );
    // Default-on redaction (enabled unset) with the rule present → accepted.
    const defaultOn = loadYamlSpec(
      fullSpec({
        runtime: `${BASE_RUNTIME}\n  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: case_id, pattern: 'CASE-\\d{6}', replacement: '[C]' }`,
      }),
    );
    expect(() => enforcePolicyCompliance(defaultOn, policy)).not.toThrow();
    // A policy with NO require_custom_rules leaves enabled:false untouched (existing behavior).
    const noRequire = policyOf("observability: { allowed_backends: [none, langfuse] }");
    expect(() => enforcePolicyCompliance(disabled, noRequire)).not.toThrow();
  });

  it("temporal: address→region mapping is policy truth; a bad self-attested region conflicts", () => {
    const policy = policyOf(
      "runtime: { temporal: { allowed_regions: [us-east], address_regions: { 'temporal.prod:7233': us-east } } }",
    );
    const inRegion = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { address: 'temporal.prod:7233' }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(inRegion, policy)).not.toThrow();
    // An address with no mapping entry fails closed.
    const unmapped = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { address: 'temporal.rogue:7233' }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(unmapped, policy)).toThrow(/has no region mapping/);
  });

  it("review: require_review_routes and invalid_user_decision match", () => {
    const routesRequired = policyOf("review: { require_review_routes: true }");
    const noReview = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(noReview, routesRequired)).toThrow(/review routes are required/);

    const reviewLifecycle =
      "  lifecycle:\n    enabled: true\n    review:\n      after_step: s\n      user_decisions:\n        approve: { route: s }\n      invalid_user_decision: fail";
    const withReview = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME, lifecycle: reviewLifecycle }));
    expect(() => enforcePolicyCompliance(withReview, routesRequired)).not.toThrow();

    // invalid_user_decision must MATCH the policy.
    const mustFail = policyOf("review: { invalid_user_decision: fail }");
    expect(() => enforcePolicyCompliance(withReview, mustFail)).not.toThrow();
    const warnLifecycle = reviewLifecycle.replace("invalid_user_decision: fail", "invalid_user_decision: warn");
    const warnReview = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME, lifecycle: warnLifecycle }));
    expect(() => enforcePolicyCompliance(warnReview, mustFail)).toThrow(/does not match policy 'fail'/);
  });

  it("review: a multi-gate `gates` workflow satisfies require_review_routes; each gate honors invalid_user_decision (#55 slice 4)", () => {
    const routesRequired = policyOf("review: { require_review_routes: true }");
    const gatesLifecycle =
      "  lifecycle:\n    enabled: true\n    gates:\n      - id: g1\n        after_step: s\n        user_decisions:\n          approve: { route: s }\n        invalid_user_decision: fail";
    const withGates = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME, lifecycle: gatesLifecycle }));
    expect(() => enforcePolicyCompliance(withGates, routesRequired)).not.toThrow();
    // A gateless workflow still fails the requirement.
    const gateless = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(gateless, routesRequired)).toThrow(/review routes are required/);
    // The invalid_user_decision constraint quantifies over EVERY gate, naming the offender.
    const mustFail = policyOf("review: { invalid_user_decision: fail }");
    expect(() => enforcePolicyCompliance(withGates, mustFail)).not.toThrow();
    const warnGates = gatesLifecycle.replace("invalid_user_decision: fail", "invalid_user_decision: warn");
    const withWarnGate = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME, lifecycle: warnGates }));
    expect(() => enforcePolicyCompliance(withWarnGate, mustFail)).toThrow(/gate 'g1' invalid_user_decision 'warn' does not match policy 'fail'/);
  });

  it("semantics: required moderation on every definition, require_block mandates on_violation=block", () => {
    const requireModeration = policyOf("semantics: { required: true }");
    const noModeration = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(noModeration, requireModeration)).toThrow(/must declare moderation/);

    const blockModeration = "      moderation: { provider: openai, on_violation: block }";
    const withBlock = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME, defExtra: blockModeration }));
    expect(() => enforcePolicyCompliance(withBlock, requireModeration)).not.toThrow();

    // require_block rejects on_violation=flag.
    const requireBlock = policyOf("semantics: { require_block: true }");
    const flagModeration = "      moderation: { provider: openai, on_violation: flag }";
    const withFlag = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME, defExtra: flagModeration }));
    expect(() => enforcePolicyCompliance(withFlag, requireBlock)).toThrow(/must use on_violation='block'/);
    expect(() => enforcePolicyCompliance(withBlock, requireBlock)).not.toThrow();
  });

  it("provider_retry: max_attempts + retry-class flags may only tighten", () => {
    const policy = policyOf("runtime: { provider_retry: { max_attempts: 2, retry_rate_limits: false } }");
    const tooMany = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  provider_retry: { max_attempts: 5 }` }),
    );
    expect(() => enforcePolicyCompliance(tooMany, policy)).toThrow(/max_attempts 5 exceeds 2/);
    // retry_rate_limits defaults true; the policy forbids it → fail.
    const broadRateLimits = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  provider_retry: { max_attempts: 2 }` }),
    );
    expect(() => enforcePolicyCompliance(broadRateLimits, policy)).toThrow(/retry_rate_limits is broader/);
    const compliant = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  provider_retry: { max_attempts: 2, retry_rate_limits: false }` }),
    );
    expect(() => enforcePolicyCompliance(compliant, policy)).not.toThrow();
    // No provider_retry block → effective 1 attempt, but retry_rate_limits defaults true → still fails the flag.
    const noRetry = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(noRetry, policy)).toThrow(/retry_rate_limits is broader/);
  });

  it("provider_limits: default tier + per-model tier bound the runtime tier", () => {
    const policy = policyOf("runtime: { provider_limits: { default: { max_concurrent: 4 } } }");
    const missing = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(missing, policy)).toThrow(/provider_limits must be configured/);
    const tooWide = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  provider_limits: { default: { max_concurrent: 8 } }` }),
    );
    expect(() => enforcePolicyCompliance(tooWide, policy)).toThrow(/max_concurrent must be configured and <= 4/);
    const compliant = loadYamlSpec(
      fullSpec({ runtime: `${BASE_RUNTIME}\n  provider_limits: { default: { max_concurrent: 2 } }` }),
    );
    expect(() => enforcePolicyCompliance(compliant, policy)).not.toThrow();

    // A per-model policy tier binds only when the model is pinned; min_interval is a floor.
    const modelPolicy = policyOf(
      "runtime: { provider_limits: { providers: { openai: { models: { gpt-4o: { min_interval_seconds: 2 } } } } } }",
    );
    const tooFast = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, model: gpt-4o }\n  provider_limits: { providers: { openai: { models: { gpt-4o: { min_interval_seconds: 1 } } } } }",
      }),
    );
    expect(() => enforcePolicyCompliance(tooFast, modelPolicy)).toThrow(/min_interval_seconds must be configured and >= 2/);
  });

  it("imports policy is reported skipped (structurally satisfied by the TS injection model)", () => {
    const policy = policyOf("imports: { allow_provider_class: false, allowed_module_roots: [acme] }");
    const spec = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(check(validatePolicyCompliance(spec, policy), "policy_imports")?.status).toBe("skipped");
    // …and it never fails a TS workflow.
    expect(() => enforcePolicyCompliance(spec, policy)).not.toThrow();
  });

  it("a multi-dimension policy fails closed on the FIRST violated dimension and reports every check", () => {
    const policy = policyOf(
      "providers: { allowed: { openai: { models: [gpt-4o-mini] } } }\nsecrets: { require_secret_references: true }\nobservability: { required: true }",
    );
    const spec = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }",
      }),
    );
    // provider passes (allowed model), but observability (none) fails.
    const checks = validatePolicyCompliance(spec, policy);
    expect(check(checks, "policy_provider")?.status).toBe("passed");
    expect(check(checks, "policy_secrets")?.status).toBe("passed");
    expect(check(checks, "policy_observability")?.status).toBe("failed");
    expect(() => enforcePolicyCompliance(spec, policy)).toThrow(ProjectPolicyEnforcementError);
  });
});

describe("policy enforcement — fail-open fixes (#454 slice 2, codex/finder review)", () => {
  const inlineRuntime = (extra: string) =>
    `  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai${extra} }`;

  it("require_tls fails when temporal.tls is UNSET (Python defaults tls=false → unset is disabled)", () => {
    const policy = policyOf("runtime: { temporal: { require_tls: true } }");
    // temporal: {} → tls undefined. Pre-fix this passed (undefined !== false); now it fails closed.
    const unset = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(unset, policy)).toThrow(/Temporal TLS is required/);
    // Explicit true satisfies it.
    const on = loadYamlSpec(
      fullSpec({ runtime: "  temporal: { tls: true }\n  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }" }),
    );
    expect(() => enforcePolicyCompliance(on, policy)).not.toThrow();
  });

  it("Temporal address/namespace allow-lists honor the Python spec defaults when the field is omitted", () => {
    // Omitted address/namespace resolve to Python's defaults (localhost:7233 / default).
    const allowsDefaults = policyOf("runtime: { temporal: { allowed_addresses: ['localhost:7233'], allowed_namespaces: [default] } }");
    const omitted = loadYamlSpec(fullSpec({ runtime: BASE_RUNTIME }));
    expect(() => enforcePolicyCompliance(omitted, allowsDefaults)).not.toThrow();
    // A policy that does NOT allow the default address rejects the omitted-field spec.
    const other = policyOf("runtime: { temporal: { allowed_addresses: ['prod.temporal:7233'] } }");
    expect(() => enforcePolicyCompliance(omitted, other)).toThrow(/Temporal address 'localhost:7233' is not allowed/);
  });

  it("a per-activity provider_params.model override is checked against the allow-list (codex fail-open)", () => {
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    // runtime.provider defaults to the allowed model, but the activity pins gpt-4o —
    // the runtime would execute under it, so admission must catch the override.
    const override = loadYamlSpec(
      fullSpec({ runtime: inlineRuntime(""), defExtra: "      provider_params: { model: gpt-4o }" }),
    );
    expect(() => enforcePolicyCompliance(override, policy)).toThrow(/provider model 'gpt-4o' is not allowed/);
    // An allowed override passes.
    const allowed = loadYamlSpec(
      fullSpec({ runtime: inlineRuntime(""), defExtra: "      provider_params: { model: gpt-4o-mini }" }),
    );
    expect(() => enforcePolicyCompliance(allowed, policy)).not.toThrow();
  });

  it("a model allow-list + allow_prompt_model_override on a NON-inline registry fails closed", () => {
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    const backend = loadYamlSpec(
      fullSpec({
        runtime:
          "  temporal: {}\n  registry: { type: langfuse, host: cloud.langfuse.com }\n  provider: { type: openai, allow_prompt_model_override: true }",
      }),
    );
    expect(() => enforcePolicyCompliance(backend, policy)).toThrow(/allow_prompt_model_override/);
    // An inline registry is fully static, so the same override flag is exempt.
    const inline = loadYamlSpec(fullSpec({ runtime: inlineRuntime(", allow_prompt_model_override: true") }));
    expect(() => enforcePolicyCompliance(inline, policy)).not.toThrow();
  });

  it("a review block under a DISABLED lifecycle does not satisfy require_review_routes (codex fail-open)", () => {
    const policy = policyOf("review: { require_review_routes: true }");
    const reviewBlock =
      "\n    review:\n      after_step: s\n      user_decisions:\n        approve: { route: s }";
    // enabled: false → the review gate never runs, so the policy is NOT satisfied.
    const disabled = loadYamlSpec(
      fullSpec({ runtime: BASE_RUNTIME, lifecycle: `  lifecycle:\n    enabled: false${reviewBlock}` }),
    );
    expect(() => enforcePolicyCompliance(disabled, policy)).toThrow(/review routes are required/);
    // enabled: true → the gate runs, satisfying the policy.
    const enabled = loadYamlSpec(
      fullSpec({ runtime: BASE_RUNTIME, lifecycle: `  lifecycle:\n    enabled: true${reviewBlock}` }),
    );
    expect(() => enforcePolicyCompliance(enabled, policy)).not.toThrow();
  });
});

describe("RuntimePolicyGuard — per-call enforcement (#454 slice 3)", () => {
  it("enforceProviderModel allows a listed model and rejects an unlisted one", () => {
    const guard = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }"), "openai");
    expect(() =>
      guard.enforceProviderModel({ providerName: "openai", model: "gpt-4o-mini", activityName: "a", promptName: "p/x" }),
    ).not.toThrow();
    expect(() =>
      guard.enforceProviderModel({ providerName: "openai", model: "gpt-4o", activityName: "a", promptName: "p/x" }),
    ).toThrow(/model 'gpt-4o' is not allowed/);
    // The error carries the failed check + a context suffix.
    try {
      guard.enforceProviderModel({ providerName: "openai", model: "gpt-4o", activityName: "a", promptName: "p/x" });
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectPolicyEnforcementError);
      expect((error as ProjectPolicyEnforcementError).message).toMatch(/activity=a, prompt=p\/x/);
    }
  });

  it("enforceProviderModel checks the SPEC provider identity, not the caller-derived name", () => {
    // Constructed with the spec identity 'openai'; a call reporting 'anthropic' is
    // still checked against the openai allow-list (Python `self.provider_name or ...`).
    const guard = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }"), "openai");
    expect(() =>
      guard.enforceProviderModel({ providerName: "anthropic", model: "gpt-4o-mini", activityName: "a", promptName: "p" }),
    ).not.toThrow();
    // With no bound identity, it falls back to the call's providerName.
    const unbound = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: {} } }"));
    expect(() =>
      unbound.enforceProviderModel({ providerName: "gemini", model: "x", activityName: "a", promptName: "p" }),
    ).toThrow(/provider 'gemini' is not allowed/);
  });

  it("enforceProviderModel is a no-op when the policy does not constrain providers", () => {
    const guard = new RuntimePolicyGuard(policyOf("artifacts: { max_bytes: 10 }"), "openai");
    expect(() =>
      guard.enforceProviderModel({ providerName: "openai", model: "anything", activityName: "a", promptName: "p" }),
    ).not.toThrow();
  });

  it("enforceProviderModel FOLDS the provider-type default for an undefined model (matches admission)", () => {
    // An undefined resolved model (nothing pinned it) folds the type default —
    // openai → gpt-4o-mini, the same value admission's effectiveModelFor validated.
    const allowsDefault = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }"), "openai");
    expect(() =>
      allowsDefault.enforceProviderModel({ providerName: "openai", model: undefined, activityName: "a", promptName: "p" }),
    ).not.toThrow();
    // …and if the type default is NOT on the allow-list, the undefined call fails on it.
    const forbidsDefault = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: { models: [o1] } } }"), "openai");
    expect(() =>
      forbidsDefault.enforceProviderModel({ providerName: "openai", model: undefined, activityName: "a", promptName: "p" }),
    ).toThrow(/model 'gpt-4o-mini' is not allowed/);
  });

  it("enforceModerationConfig requires moderation on a code-defined activity and mandates on_violation=block", () => {
    const required = new RuntimePolicyGuard(policyOf("semantics: { required: true }"));
    expect(() => required.enforceModerationConfig({ activityName: "a", moderationConfigured: false, onViolation: undefined })).toThrow(
      /must declare moderation/,
    );
    expect(() => required.enforceModerationConfig({ activityName: "a", moderationConfigured: true, onViolation: "flag" })).not.toThrow();

    const mustBlock = new RuntimePolicyGuard(policyOf("semantics: { require_block: true }"));
    expect(() => mustBlock.enforceModerationConfig({ activityName: "a", moderationConfigured: true, onViolation: "flag" })).toThrow(
      /must use on_violation='block'/,
    );
    expect(() => mustBlock.enforceModerationConfig({ activityName: "a", moderationConfigured: true, onViolation: "block" })).not.toThrow();
    // require_block also implies moderation must be present.
    expect(() => mustBlock.enforceModerationConfig({ activityName: "a", moderationConfigured: false, onViolation: undefined })).toThrow(
      /must declare moderation/,
    );
    // No semantics policy → never fails.
    const none = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: {} } }"));
    expect(() => none.enforceModerationConfig({ activityName: "a", moderationConfigured: false, onViolation: undefined })).not.toThrow();
  });

  it("moderationPolicyBlock escalates a disallowed category or an at-threshold score, else undefined", () => {
    const guard = new RuntimePolicyGuard(policyOf("semantics: { categories: [violence, hate], score_threshold: 0.8 }"));
    expect(guard.moderationPolicyBlock({ activityName: "a", categories: ["violence"], maxScore: undefined })).toMatch(
      /disallowed category violence/,
    );
    expect(guard.moderationPolicyBlock({ activityName: "a", categories: ["hate", "violence"], maxScore: undefined })).toMatch(
      /disallowed categories hate, violence/,
    );
    expect(guard.moderationPolicyBlock({ activityName: "a", categories: [], maxScore: 0.8 })).toMatch(
      /score 0.8 >= threshold 0.8/,
    );
    expect(guard.moderationPolicyBlock({ activityName: "a", categories: ["safe"], maxScore: 0.5 })).toBeUndefined();
    // No semantics policy → never escalates.
    const noSemantics = new RuntimePolicyGuard(policyOf("providers: { allowed: { openai: {} } }"));
    expect(noSemantics.moderationPolicyBlock({ activityName: "a", categories: ["violence"], maxScore: 1 })).toBeUndefined();
  });
});
