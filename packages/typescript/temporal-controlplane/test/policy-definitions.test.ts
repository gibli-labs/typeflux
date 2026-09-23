import {
  emptyProfileSources,
  composeProjectPolicyIds,
  loadPolicySpec,
  loadProjectSpec,
  type ProjectBundleSources,
} from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { buildPolicyDefinition, buildPolicySummaries, ProjectControlPlaneError } from "../src/index.js";

/** Assert `fn` throws a `ProjectControlPlaneError` with the given HTTP status + message (the contract
 * the HTTP adapter maps on — a bare `Error` or wrong status would 500 instead of the expected 422). */
const expectStatus = (fn: () => unknown, status: number, message: RegExp): void => {
  let caught: unknown;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  expect(caught).toBeInstanceOf(ProjectControlPlaneError);
  expect((caught as ProjectControlPlaneError).status).toBe(status);
  expect((caught as Error).message).toMatch(message);
};

const PROJECT = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
  - { id: intake, path: intake.yaml }
policies:
  base: policies/base.yaml
  strict: policies/strict.yaml
environments:
  prod: envs/prod.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [strict] }
    prod-intake: { workflows: [intake], environment: prod, policies: [base, strict] }
`);

// `base` has NO description (→ null/omitted); `strict` extends it and carries one.
const sources: ProjectBundleSources = {
  policies: {
    base: loadPolicySpec("name: base\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n"),
    strict: loadPolicySpec("name: strict\ndescription: Strict floor\nextends: [base]\n"),
  },
  environments: {},
  workflows: {},
  profiles: emptyProfileSources(),
};

describe("buildPolicySummaries (#563 slice 2b)", () => {
  it("lists declared policies sorted by id with description present (null when absent) — no exclude_none", () => {
    expect(buildPolicySummaries(PROJECT, sources)).toEqual([
      { id: "base", name: "base", description: null, path: "policies/base.yaml" },
      { id: "strict", name: "strict", description: "Strict floor", path: "policies/strict.yaml" },
    ]);
  });
});

describe("buildPolicyDefinition (#563 slice 2b)", () => {
  it("projects rules + extends + composed policy_hash + used_by for a policy that extends another", () => {
    const def = buildPolicyDefinition(PROJECT, sources, "strict");
    expect(def).toMatchObject({
      id: "strict",
      name: "strict",
      description: "Strict floor",
      extends: ["base"],
      used_by: ["intake", "review"], // both targets select `strict`, sorted + deduped
    });
    // policy_hash equals the direct composition of the same single-policy closure.
    expect(def.policy_hash).toBe(composeProjectPolicyIds(PROJECT, sources, ["strict"]).policyHash);
    // rules carry EXACTLY the config subtrees — no identity field (version/name/description/extends)
    // may leak in (locks the destructure against a future spec field being missed).
    expect(Object.keys(def.rules).sort()).toEqual([
      "artifacts",
      "composition",
      "imports",
      "observability",
      "providers",
      "review",
      "risk_tiers",
      "runtime",
      "secrets",
      "semantics",
    ]);
  });

  it("omits description (exclude_none) for a policy that declares none, and used_by reflects only its targets", () => {
    const def = buildPolicyDefinition(PROJECT, sources, "base");
    expect("description" in def).toBe(false); // exclude_none on the detail route
    expect(def.extends).toEqual([]);
    expect(typeof def.policy_hash).toBe("string");
    expect(def.used_by).toEqual(["intake"]); // only prod-intake selects `base` directly
  });

  it("rejects a mis-keyed policy (spec name != manifest id) as a 422 from BOTH list and detail (codex)", () => {
    // `strict`'s source declares name "loose" — a mis-filed policy must not be listed or inspected.
    const misKeyed: ProjectBundleSources = {
      ...sources,
      policies: { ...sources.policies, strict: loadPolicySpec("name: loose\n") },
    };
    expectStatus(() => buildPolicySummaries(PROJECT, misKeyed), 422, /name must match the project policy id/);
    expectStatus(() => buildPolicyDefinition(PROJECT, misKeyed, "strict"), 422, /name must match the project policy id/);
  });

  it("rejects a policy id that is not a local reference (contains /, \\, or :) as a 422 from list AND detail (codex)", () => {
    // `loadProjectSpec`'s policy-key check is only non-empty/trimmed, so a path-like id slips through
    // to the CP — which must reject it (Python `_validate_policy_id`), not project it as valid.
    const malformed = loadProjectSpec(
      'version: "1"\nname: acme\nworkflows:\n  - { id: review, path: review.yaml }\npolicies:\n  "bad/id": policies/bad.yaml\n',
    );
    const emptySources: ProjectBundleSources = { policies: {}, environments: {}, workflows: {}, profiles: emptyProfileSources() };
    expectStatus(() => buildPolicySummaries(malformed, emptySources), 422, /local project references/);
    expectStatus(() => buildPolicyDefinition(malformed, emptySources, "bad/id"), 422, /local project references/);
  });

  it("renders rules as Python's exact null skeleton (#571 — payload parity for /policies/{id})", () => {
    // The expected tree is Python's actual output for the same YAML:
    //   TypefluxProjectPolicySpec.model_validate({...}).model_dump(
    //       mode="json", exclude={"version", "name", "description", "extends"})
    // — every unset optional leaf is an explicit null; an unset optional SUBTREE is one null.
    const parity: ProjectBundleSources = {
      policies: {
        base: loadPolicySpec(
          "name: base\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n" +
            "runtime: { temporal: { require_tls: true } }\n",
        ),
      },
      environments: {},
      workflows: {},
      profiles: emptyProfileSources(),
    };
    expect(buildPolicyDefinition(PROJECT, parity, "base").rules).toEqual({
      artifacts: { allowed_media_types: null, allowed_sources: null, max_bytes: null },
      composition: {
        allow_map_over_workflow: null,
        max_parallel_nesting: null,
        max_parallel_width: null,
        max_steps: null,
        max_subworkflow_depth: null,
        max_total_steps: null,
      },
      imports: {
        allow_absolute_activity_modules: null,
        allow_moderator_callable: null,
        allow_observability_class: null,
        allow_provider_class: null,
        allow_registry_class: null,
        allowed_module_roots: null,
      },
      observability: {
        allowed_backends: null,
        redaction: { preserve_typeflux_metadata: null, require_custom_rules: null, required: null },
        required: null,
      },
      providers: { allowed: { openai: { base_urls: null, models: ["gpt-4o-mini"] } } },
      review: { invalid_user_decision: null, require_review_routes: null },
      // #300: each per-tier block is a nested null skeleton (an unset macro requirement is
      // null); min_tier/require_declared are scalar nulls — parity with Python's model_dump.
      risk_tiers: {
        min_tier: null,
        require_declared: null,
        safe: { require_review: null, require_moderation: null, require_redaction: null, require_payload_codec: null, require_compensation: null, constrain_providers: null },
        policy_gated: { require_review: null, require_moderation: null, require_redaction: null, require_payload_codec: null, require_compensation: null, constrain_providers: null },
        human_gated: { require_review: null, require_moderation: null, require_redaction: null, require_payload_codec: null, require_compensation: null, constrain_providers: null },
        prohibited: { require_review: null, require_moderation: null, require_redaction: null, require_payload_codec: null, require_compensation: null, constrain_providers: null },
      },
      runtime: {
        provider_limits: null,
        provider_retry: null,
        registry: { allowed_hosts: null },
        temporal: {
          address_regions: null,
          allowed_addresses: null,
          allowed_namespaces: null,
          allowed_regions: null,
          require_api_key: null,
          require_payload_codec: null,
          require_tls: true,
        },
      },
      secrets: { require_secret_references: null },
      semantics: { categories: null, require_block: null, required: null, score_threshold: null },
    });
  });

  it("deep-copies rules — mutating the response cannot corrupt the loaded spec", () => {
    const def = buildPolicyDefinition(PROJECT, sources, "strict");
    (def.rules as { providers?: unknown }).providers = "MUTATED";
    expect(buildPolicyDefinition(PROJECT, sources, "strict").rules).toHaveProperty("providers");
    expect(buildPolicyDefinition(PROJECT, sources, "strict").rules.providers).not.toBe("MUTATED");
  });
});
