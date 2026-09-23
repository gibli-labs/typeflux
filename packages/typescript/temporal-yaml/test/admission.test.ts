import { describe, expect, it } from "vitest";

import {
  admitSpec,
  loadPolicySpec,
  loadProjectSpec,
  type ProjectPolicySources,
  type TypefluxProjectPolicySpec,
} from "../src/index.js";

/**
 * Spec admission seam (#298 Phase B) — TS edition. Mirrors Python `test_admission`:
 * the bounded parse, the external-origin structural gate (structurally satisfied in
 * the TS injection model), the composed-policy binding (fail-closed for an ungoverned
 * external submission), and a composition-ceiling failure surfacing in the report.
 */

const PROJECT = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: a.yaml }
policies:
  comp: comp.yaml
environments:
  prod: prod.yaml
validation:
  targets:
    prod-review:
      workflows: [review]
      environment: prod
      policies: [comp]
`);

function sourcesOf(...policies: string[]): ProjectPolicySources {
  const map: Record<string, TypefluxProjectPolicySpec> = {};
  for (const yaml of policies) {
    const spec = loadPolicySpec(yaml);
    map[spec.name] = spec;
  }
  return { policies: map };
}

const COMP_POLICY = "name: comp\ncomposition: { max_steps: 5 }\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";

const workflow = (steps: string): string => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  output: schemas:Out
  steps:
${steps}
`;

const codes = (report: { checks: { code: string; status: string }[] }): Record<string, string> =>
  Object.fromEntries(report.checks.map((c) => [c.code, c.status]));

describe("admitSpec (#298 Phase B; Python admit_spec)", () => {
  it("admits an external spec that satisfies the bound composed policy", () => {
    const report = admitSpec(workflow("    - { id: s, activity: a }"), {
      project: PROJECT,
      policies: sourcesOf(COMP_POLICY),
      environmentId: "prod",
      origin: "external",
      workflowId: "review",
      loadOptions: { env: {} },
    });
    expect(report.admitted).toBe(true);
    const c = codes(report);
    expect(c["admission_external_modules_forbidden"]).toBe("passed");
    expect(c["policy_composition_ceilings"]).toBe("passed");
    expect(report.policyHash).toBeDefined();
    // The report carries the resolved slot and the exact evaluated spec — build the
    // runtime FROM report.spec so what runs is what was admitted (#298 review).
    expect(report.workflowId).toBe("review");
    expect(report.spec).toBeDefined();
    expect(report.spec?.name).toBe("n");
  });

  it("resolves the slot from the spec's own name when workflowId is omitted", () => {
    // The spec is named "n" (no target binds it), so an external submission with no
    // explicit slot resolves to "n" and fails closed on policy selection — the SAME
    // resolved slot drives selection and the report (Python parity).
    const report = admitSpec(workflow("    - { id: s, activity: a }"), {
      project: PROJECT,
      policies: sourcesOf(COMP_POLICY),
      environmentId: "prod",
      origin: "external",
      loadOptions: { env: {} },
    });
    expect(report.workflowId).toBe("n");
    expect(report.admitted).toBe(false);
    expect(codes(report)["admission_policy_selection"]).toBe("failed");
  });

  it("surfaces a composition-ceiling violation as a failed check", () => {
    const report = admitSpec(
      workflow("    - { id: s0, activity: a }\n    - { id: s1, activity: a }\n    - { id: s2, activity: a }\n    - { id: s3, activity: a }\n    - { id: s4, activity: a }\n    - { id: s5, activity: a }"),
      {
        project: PROJECT,
        policies: sourcesOf(COMP_POLICY),
        environmentId: "prod",
        origin: "external",
        workflowId: "review",
        loadOptions: { env: {} },
      },
    );
    expect(report.admitted).toBe(false);
    expect(codes(report)["policy_composition_ceilings"]).toBe("failed");
  });

  it("fails closed for an external submission with no governing policy", () => {
    const report = admitSpec(workflow("    - { id: s, activity: a }"), {
      project: PROJECT,
      policies: sourcesOf(COMP_POLICY),
      environmentId: "prod",
      origin: "external",
      loadOptions: { env: {} },
    });
    expect(report.admitted).toBe(false);
    expect(codes(report)["admission_policy_selection"]).toBe("failed");
  });

  it("skips (and admits) an operator submission with no governing policy", () => {
    const report = admitSpec(workflow("    - { id: s, activity: a }"), {
      project: PROJECT,
      policies: sourcesOf(COMP_POLICY),
      environmentId: "prod",
      origin: "operator",
      loadOptions: { env: {} },
    });
    expect(report.admitted).toBe(true);
    expect(codes(report)["admission_policy_selection"]).toBe("skipped");
  });

  it("fails closed for an explicit workflowId the manifest does not declare", () => {
    // An explicit undeclared slot must not proceed with a missing manifest slot —
    // profile selection would silently be skipped (Bugbot; Python parity).
    const report = admitSpec(workflow("    - { id: s, activity: a }"), {
      project: PROJECT,
      policies: sourcesOf(COMP_POLICY),
      environmentId: "prod",
      origin: "external",
      workflowId: "__unbound__",
      loadOptions: { env: {} },
    });
    expect(report.admitted).toBe(false);
    const unknown = report.checks.find((check) => check.code === "admission_unknown_workflow");
    expect(unknown?.status).toBe("failed");
    expect(unknown?.message).toContain("'__unbound__'");
    expect(unknown?.message).toContain("review");
  });

  it("reports a parse failure as a check, not a throw", () => {
    const report = admitSpec("this: [is not valid", {
      project: PROJECT,
      policies: sourcesOf(COMP_POLICY),
      environmentId: "prod",
      origin: "external",
      loadOptions: { env: {} },
    });
    expect(report.admitted).toBe(false);
    expect(report.checks[0]?.code).toBe("admission_spec_shape");
    expect(report.checks[0]?.status).toBe("failed");
  });
});
