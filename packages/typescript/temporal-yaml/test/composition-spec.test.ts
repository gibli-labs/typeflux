// Composition surface (#55 slice 1): `parallel:` blocks + `when:` gating — spec
// parsing (incl. every load-time rejection), plan building, and the digest-stability
// regressions that pin V1 specs byte-identical through the new code paths.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { canonicalJson } from "@typeflux/temporal";

import { workflowPlanDigest } from "../src/frozen-version.js";
import { loadYamlSpec } from "../src/loader.js";
import { workflowPlanFromSpec, workflowSchemaChainError } from "../src/build-workflow.js";
import type { ParallelPlanStep, WorkflowPlan } from "../src/workflow-plan.js";

const here = dirname(fileURLToPath(import.meta.url));

/** A full spec around the given workflow steps (activity refs chain the design example). */
const specWith = (steps: string, workflow = ""): string => `
project: p
name: disclosure_review
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - { name: classify_disclosure, input: "schemas:Disclosure", output: "schemas:Classification", prompt: p/classify }
    - { name: legal_screen, input: "schemas:Classification", output: "schemas:LegalScreen", prompt: p/legal_screen }
    - { name: legal_assess, input: "schemas:LegalScreen", output: "schemas:LegalAssessment", prompt: p/legal_assess }
    - { name: medical_review, input: "schemas:Classification", output: "schemas:MedicalReview", prompt: p/medical }
    - { name: consolidate_reviews, input: "schemas:ReviewBundle", output: "schemas:ReviewOutcome", prompt: p/consolidate }
workflow:
  name: DisclosureReviewWorkflow
  input: schemas:Disclosure
  output: schemas:ReviewOutcome
${workflow}
  steps:
${steps}
`;

/** The design's §3.1 example, verbatim in shape. */
const COMPOSITION_STEPS = `
    - id: classify
      activity: classify_disclosure
    - id: reviews
      parallel:
        branches:
          - id: legal
            when: { path: classify.needs_legal, eq: true }
            steps:
              - id: legal_screen_step
                activity: legal_screen
              - id: legal_assess_step
                activity: legal_assess
          - id: medical
            steps:
              - id: medical_review_step
                activity: medical_review
        collect:
          output: schemas:ReviewBundle
          max_bytes: 1500000
    - id: consolidate
      activity: consolidate_reviews
`;

describe("parallel/when spec parsing and plan building (#55)", () => {
  it("builds the composition plan: normalized when, embedded branch sequences, collect bound", () => {
    const spec = loadYamlSpec(specWith(COMPOSITION_STEPS));
    const plan = workflowPlanFromSpec(spec);
    expect(plan.steps).toEqual([
      { kind: "activity", id: "classify", activity: "classify_disclosure" },
      {
        kind: "parallel",
        id: "reviews",
        branches: [
          {
            id: "legal",
            when: { mode: "leaf", predicates: [{ path: "classify.needs_legal", op: "eq", value: true }] },
            steps: [
              { kind: "activity", id: "legal_screen_step", activity: "legal_screen" },
              { kind: "activity", id: "legal_assess_step", activity: "legal_assess" },
            ],
          },
          {
            id: "medical",
            steps: [{ kind: "activity", id: "medical_review_step", activity: "medical_review" }],
          },
        ],
        collectMaxBytes: 1_500_000,
      },
      { kind: "activity", id: "consolidate", activity: "consolidate_reviews" },
    ]);
    expect(workflowSchemaChainError(spec)).toBeUndefined();
  });

  it("collect.max_bytes defaults ON at 1.5MB and an explicit 0 disables (map parity)", () => {
    const defaulted = loadYamlSpec(
      specWith(COMPOSITION_STEPS.replace("\n          max_bytes: 1500000", "")),
    );
    const block = workflowPlanFromSpec(defaulted).steps[1] as ParallelPlanStep;
    expect(block.collectMaxBytes).toBe(1_500_000);
    const disabled = loadYamlSpec(specWith(COMPOSITION_STEPS.replace("max_bytes: 1500000", "max_bytes: 0")));
    const disabledBlock = workflowPlanFromSpec(disabled).steps[1] as ParallelPlanStep;
    expect(disabledBlock.collectMaxBytes).toBeUndefined();
  });

  it("normalizes all/any composition (one level) and every operator", () => {
    const spec = loadYamlSpec(
      specWith(
        COMPOSITION_STEPS.replace(
          "when: { path: classify.needs_legal, eq: true }",
          "when: { any: [ { path: classify.risk, gte: 0.5 }, { path: classify.kind, in: [legal, mixed] }, { path: classify.flag, exists: true } ] }",
        ),
      ),
    );
    const block = workflowPlanFromSpec(spec).steps[1] as ParallelPlanStep;
    expect(block.branches[0]?.when).toEqual({
      mode: "any",
      predicates: [
        { path: "classify.risk", op: "gte", value: 0.5 },
        { path: "classify.kind", op: "in", value: ["legal", "mixed"] },
        { path: "classify.flag", op: "exists", value: true },
      ],
    });
  });

  it("rejects a leaf with zero or two operators", () => {
    expect(() =>
      loadYamlSpec(
        specWith(COMPOSITION_STEPS.replace("when: { path: classify.needs_legal, eq: true }", "when: { path: classify.needs_legal }")),
      ),
    ).toThrow(/exactly one operator/);
    expect(() =>
      loadYamlSpec(
        specWith(
          COMPOSITION_STEPS.replace(
            "when: { path: classify.needs_legal, eq: true }",
            "when: { path: classify.needs_legal, eq: true, gte: 1 }",
          ),
        ),
      ),
    ).toThrow(/exactly one operator/);
  });

  it("rejects nested all/any (one composition level, decision D1)", () => {
    expect(() =>
      loadYamlSpec(
        specWith(
          COMPOSITION_STEPS.replace(
            "when: { path: classify.needs_legal, eq: true }",
            "when: { all: [ { any: [ { path: classify.risk, gte: 0.5 } ] } ] }",
          ),
        ),
      ),
    ).toThrow(/invalid Typeflux spec/);
  });

  it("rejects when.predicate with the named-injected-predicates pointer", () => {
    expect(() =>
      loadYamlSpec(
        specWith(
          COMPOSITION_STEPS.replace("when: { path: classify.needs_legal, eq: true }", "when: { predicate: legal_gate }"),
        ),
      ),
    ).toThrow(/named injected predicates/);
  });

  it("rejects a map step declaring BOTH activity and workflow (#55 §3.4 — exactly one)", () => {
    expect(() =>
      loadYamlSpec(
        specWith("    - id: s\n      map:\n        workflow: child\n        activity: classify_disclosure\n        over: input.items\n        concurrency: 2\n        collect: { output: schemas:X, field: items }\n"),
      ),
    ).toThrow(/exactly one of `activity` or `workflow`/);
  });

  it("rejects a standalone workflow: step at plan derivation (needs a project manifest) (#55 §1)", () => {
    // The spec PARSES (workflow: is a real field now), but a sub-workflow reference resolves
    // only through a project manifest — a standalone spec has no siblings to resolve against.
    const workflowStep = loadYamlSpec(specWith("    - id: s\n      workflow: child_pipeline\n"));
    expect(() => workflowPlanFromSpec(workflowStep)).toThrow(/loaded standalone|project manifest/);
    const mapStep = loadYamlSpec(
      specWith("    - id: s\n      map:\n        workflow: child\n        over: input.items\n        concurrency: 2\n        collect: { output: schemas:X, field: items }\n"),
    );
    expect(() => workflowPlanFromSpec(mapStep)).toThrow(/loaded standalone|project manifest/);
  });

  it("rejects a parallel block without collect and with empty branches/steps", () => {
    expect(() =>
      loadYamlSpec(
        specWith(
          "    - id: p\n      parallel:\n        branches:\n          - id: b\n            steps: [{ id: s, activity: classify_disclosure }]\n",
        ),
      ),
    ).toThrow(/invalid Typeflux spec/);
    expect(() =>
      loadYamlSpec(specWith("    - id: p\n      parallel:\n        branches: []\n        collect: { output: schemas:X }\n")),
    ).toThrow(/parallel.branches must not be empty/);
    expect(() =>
      loadYamlSpec(
        specWith("    - id: p\n      parallel:\n        branches: [{ id: b, steps: [] }]\n        collect: { output: schemas:X }\n"),
      ),
    ).toThrow(/parallel branch steps must not be empty/);
  });

  it("rejects a step with both activity and parallel (exactly one kind)", () => {
    const spec = loadYamlSpec(
      specWith(
        "    - id: s\n      activity: classify_disclosure\n      parallel:\n        branches: [{ id: b, steps: [{ id: n, activity: classify_disclosure }] }]\n        collect: { output: schemas:X }\n",
      ),
    );
    expect(() => workflowPlanFromSpec(spec)).toThrow(/exactly one of `activity`, `map`, `parallel`, or `workflow`/);
  });

  it("enforces the flat id namespace across branches and nested steps", () => {
    // Branch id duplicating a top-level step id.
    const dupBranch = loadYamlSpec(specWith(COMPOSITION_STEPS.replace("- id: legal\n", "- id: classify\n")));
    expect(() => workflowPlanFromSpec(dupBranch)).toThrow(/duplicate workflow step id "classify"/);
    // Nested step id duplicating a sibling branch's step id.
    const dupNested = loadYamlSpec(
      specWith(COMPOSITION_STEPS.replace("- id: medical_review_step\n", "- id: legal_screen_step\n")),
    );
    expect(() => workflowPlanFromSpec(dupNested)).toThrow(/duplicate workflow step id "legal_screen_step"/);
    // The reserved context key, nested.
    const reserved = loadYamlSpec(specWith(COMPOSITION_STEPS.replace("- id: medical_review_step\n", "- id: input\n")));
    expect(() => workflowPlanFromSpec(reserved)).toThrow(/reserved/);
  });

  it("rejects parallel nesting beyond 3 with the sub-workflow hint (decision D3)", () => {
    const nest = (depth: number): string => {
      if (depth === 0) {
        return "{ id: leaf, activity: classify_disclosure }";
      }
      return `{ id: p${depth}, parallel: { branches: [{ id: b${depth}, steps: [ ${nest(depth - 1)} ] }], collect: { output: "schemas:X" } } }`;
    };
    const three = loadYamlSpec(specWith(`    - ${nest(3)}\n`, "").replace("  output: schemas:ReviewOutcome\n", ""));
    expect(() => workflowPlanFromSpec(three)).not.toThrow();
    const four = loadYamlSpec(specWith(`    - ${nest(4)}\n`, "").replace("  output: schemas:ReviewOutcome\n", ""));
    expect(() => workflowPlanFromSpec(four)).toThrow(/deeper than 3.*sub-workflow/s);
  });

  it("rejects a when path whose root is not available at the gate", () => {
    // A sibling branch's result is not addressable (it may not exist yet).
    const sibling = loadYamlSpec(
      specWith(COMPOSITION_STEPS.replace("path: classify.needs_legal", "path: medical_review_step.flag")),
    );
    expect(() => workflowPlanFromSpec(sibling)).toThrow(/sibling-branch results are not addressable/);
    // A forward reference to a step that has not run.
    const forward = loadYamlSpec(
      specWith(COMPOSITION_STEPS.replace("path: classify.needs_legal", "path: consolidate.flag")),
    );
    expect(() => workflowPlanFromSpec(forward)).toThrow(/not\s+available there/);
    // Own-branch earlier steps ARE available.
    const ownBranch = loadYamlSpec(
      specWith(
        COMPOSITION_STEPS.replace(
          "- id: legal_assess_step\n                activity: legal_assess",
          "- id: legal_assess_step\n                activity: legal_assess\n                when: { path: legal_screen_step.flag, eq: true }",
        ),
      ),
    );
    expect(() => workflowPlanFromSpec(ownBranch)).not.toThrow();
  });

  it("a gated branch's inner results are NOT roots after the block (conditionally absent)", () => {
    const gateAfterBlock = (path: string): string =>
      COMPOSITION_STEPS.replace(
        "- id: consolidate\n      activity: consolidate_reviews",
        `- id: consolidate\n      when: { path: ${path}, exists: true }\n      activity: consolidate_reviews`,
      );
    // legal_screen_step lives in the when-gated legal branch: it may never have run even
    // though the block completed, so a later gate must not read it.
    expect(() => workflowPlanFromSpec(loadYamlSpec(specWith(gateAfterBlock("legal_screen_step.flag"))))).toThrow(
      /"legal_screen_step" is not\s+available/,
    );
    // medical_review_step is in an UNGATED branch: guaranteed once the block completed.
    expect(() => workflowPlanFromSpec(loadYamlSpec(specWith(gateAfterBlock("medical_review_step.flag"))))).not.toThrow();
  });

  it("review routes: a routed tail gate must not read a root the jump skips", () => {
    const spec = loadYamlSpec(
      specWith(
        COMPOSITION_STEPS.replace(
          "- id: consolidate\n      activity: consolidate_reviews",
          "- id: consolidate\n      activity: consolidate_reviews\n" +
            "    - id: finalize\n      when: { path: consolidate.flag, eq: true }\n      activity: consolidate_reviews\n",
        ),
        "  lifecycle:\n    enabled: true\n    review:\n      after_step: reviews\n      user_decisions:\n        skip: { route: finalize }\n",
      ),
    );
    // Routing reviews -> finalize skips consolidate, whose result finalize's gate reads.
    expect(() => workflowPlanFromSpec(spec)).toThrow(
      /routes past step "consolidate", but the when predicate on step "finalize"/,
    );
  });

  it("review routes: a gated tail must exit with workflow.output (a false gate returns the checkpoint value)", () => {
    // Codex round: the normal path types OutcomeBatch -> gated map -> OutcomeBatch
    // (the main-walk gate check passes), but a route jumping from the Classification
    // checkpoint straight to the gated MAP step — which consumes no running value, so
    // the tail chain check cannot object — would, on a false gate, complete the
    // workflow with the checkpoint value despite workflow.output. Load-time rejection,
    // exactly like the top-level early-exit rule (Python enforces the same on
    // resolved Pydantic types).
    const gatedTailSpec = (route: string): string => `
project: p
name: gated_tail
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - { name: classify_disclosure, input: "schemas:Disclosure", output: "schemas:Classification", prompt: p/c }
    - { name: to_batch, input: "schemas:Classification", output: "schemas:OutcomeBatch", prompt: p/b }
    - { name: review_attachment, input: "schemas:Attachment", output: "schemas:ReviewOutcome", prompt: p/r }
workflow:
  name: GatedTailWorkflow
  input: schemas:Disclosure
  output: schemas:OutcomeBatch
  lifecycle:
    enabled: true
    review:
      after_step: classify
      user_decisions:
        go: { route: ${route} }
  steps:
    - id: classify
      activity: classify_disclosure
    - id: to_batch
      activity: to_batch
    - id: review_attachments
      when: { path: classify.needs_legal, eq: true }
      map:
        activity: review_attachment
        over: input.attachments
        collect: { output: schemas:OutcomeBatch, field: outcomes }
`;
    expect(() => workflowPlanFromSpec(loadYamlSpec(gatedTailSpec("review_attachments")))).toThrow(
      /routes into a when-gated tail.*would complete the workflow with "schemas:Classification"/s,
    );
    expect(() => workflowPlanFromSpec(loadYamlSpec(gatedTailSpec("to_batch")))).not.toThrow();
  });

  it("gate typing: a top-level gate must exit with workflow.output", () => {
    // Gating `consolidate` would complete the workflow with the collect bundle, not ReviewOutcome.
    const spec = loadYamlSpec(
      specWith(
        COMPOSITION_STEPS.replace(
          "- id: consolidate\n      activity: consolidate_reviews",
          "- id: consolidate\n      when: { path: classify.route, eq: consolidate }\n      activity: consolidate_reviews",
        ),
      ),
    );
    expect(workflowSchemaChainError(spec)).toMatch(/would complete the workflow with "schemas:ReviewBundle"/);
  });

  it("gate typing: a mid-branch gate must exit with the branch's terminal type", () => {
    // Gating legal_assess_step makes schemas:LegalScreen a potential branch result while the
    // branch terminates with schemas:LegalAssessment.
    const spec = loadYamlSpec(
      specWith(
        COMPOSITION_STEPS.replace(
          "- id: legal_assess_step\n                activity: legal_assess",
          "- id: legal_assess_step\n                activity: legal_assess\n                when: { path: legal_screen_step.flag, eq: true }",
        ),
      ),
    );
    expect(workflowSchemaChainError(spec)).toMatch(
      /gate on step "legal_assess_step".*branch "legal".*terminates with "schemas:LegalAssessment"/s,
    );
  });

  it("chain: a step after the block consumes the collect output", () => {
    const spec = loadYamlSpec(
      specWith(COMPOSITION_STEPS.replace("output: schemas:ReviewBundle", "output: schemas:SomethingElse")),
    );
    expect(workflowSchemaChainError(spec)).toMatch(/"consolidate" expects input "schemas:ReviewBundle"/);
  });

  it("chain: branch first steps consume the block input", () => {
    const spec = loadYamlSpec(
      specWith(COMPOSITION_STEPS.replace("    - id: classify\n      activity: classify_disclosure\n", "")),
    );
    // Without classify, the block input is schemas:Disclosure but legal_screen expects Classification.
    expect(workflowSchemaChainError(spec)).toMatch(/"legal_screen_step" expects input "schemas:Classification"/);
  });
});

describe("digest stability (#55 §7)", () => {
  // Pinned digests. These moved once at #299 when PLAN_INTERPRETER_VERSION bumped 1 -> 2 (the
  // interpreter re-registers every type — the accepted pre-adoption cutover, mirroring Python's
  // GENERATOR_VERSION "4" -> "5"). The bump is the ONLY reason they changed: with the version
  // pinned back to 1 these examples produce their pre-#299 digests byte-for-byte (compensation is
  // present-only — a compensation-free plan serializes identically). A change here NOT explained by
  // an interpreter-version bump breaks every frozen workflow.version in the field.
  const V1_EXAMPLE_DIGESTS: Record<string, string> = {
    "insurance-claim-review": "2e5d714b6cfca877675a15526eded44c3e7221aee12f08e68479c72fa2761b47",
    "lifecycle-review": "7ab96eca6ed0b8657326a107fd82553a8f7cc5dc38fb22a75874b1ffe0ff4cf3",
    "policy-governed-review": "59e9e5bcb155a0a5aef3bfa0dace380c077fbf464bdce2ee6597b3de52a442a6",
    "session-cache-review": "2a4307febe2145ff9e72a9835f2f15a2447dfc03d44e955140bce4a025040c78",
  };

  it.each(Object.entries(V1_EXAMPLE_DIGESTS))("V1 example %s keeps its pre-composition digest", (name, digest) => {
    const yaml = readFileSync(resolve(here, `../examples/${name}/typeflux.yaml`), "utf-8");
    const plan = workflowPlanFromSpec(loadYamlSpec(yaml));
    expect(workflowPlanDigest(plan)).toBe(digest);
  });

  it("a V1 plan serializes with NO new keys (byte-identical canonical JSON)", () => {
    const yaml = readFileSync(resolve(here, "../examples/insurance-claim-review/typeflux.yaml"), "utf-8");
    const plan = workflowPlanFromSpec(loadYamlSpec(yaml));
    expect(canonicalJson(plan)).toBe(
      '{"steps":[{"activity":"review_evidence_item","collectField":"reviews","collectMaxBytes":1500000,' +
        '"concurrency":3,"id":"review_evidence","kind":"map","over":"input.evidence"},' +
        '{"activity":"consolidate_claim_review","id":"consolidate","kind":"activity"}]}',
    );
  });

  it("a composition digest is deterministic and moves on every new-surface edit", () => {
    const digestOf = (steps: string): string =>
      workflowPlanDigest(workflowPlanFromSpec(loadYamlSpec(specWith(steps))));
    const base = digestOf(COMPOSITION_STEPS);
    expect(base).toBe(digestOf(COMPOSITION_STEPS));
    expect(base).toMatch(/^[0-9a-f]{64}$/);
    // A `when` literal edit, a branch rename, and a collect-bound edit each move the digest.
    expect(digestOf(COMPOSITION_STEPS.replace("eq: true", "eq: false"))).not.toBe(base);
    expect(digestOf(COMPOSITION_STEPS.replace("- id: medical\n", "- id: medical2\n"))).not.toBe(base);
    expect(digestOf(COMPOSITION_STEPS.replace("max_bytes: 1500000", "max_bytes: 900000"))).not.toBe(base);
  });
});
