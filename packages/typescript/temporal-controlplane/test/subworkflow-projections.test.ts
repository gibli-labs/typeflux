// Sub-workflow projections through the control plane (#55 slice 3): a project whose PARENT
// workflow references a CHILD workflow by manifest id resolves+embeds the child in every
// read/operate projection (bundle, topology, resolved-plan). Proves the CP threads the
// project-scoped `subworkflows` resolver into `workflowPlanFromSpec` at each call site.

import {
  emptyProfileSources,
  loadEnvironmentSpec,
  loadPolicySpec,
  loadProjectSpec,
  type LoadedProjectBundle,
} from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { ProjectControlPlane, ProjectControlPlaneError } from "../src/index.js";

const CHILD = `
project: claims
name: ClaimAssessment
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: assess, input: schemas:Item, output: schemas:Assessment, prompt: p/x }]
workflow:
  name: ClaimAssessment
  input: schemas:Item
  output: schemas:Assessment
  steps: [{ id: assess, activity: assess }]
`;

const PARENT = `
project: claims
name: Intake
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: []
workflow:
  name: Intake
  input: schemas:Item
  output: schemas:Assessment
  steps: [{ id: assess_claim, workflow: claim_assessment }]
`;

const PROJECT = loadProjectSpec(`
version: "1"
name: claims
workflows:
  - { id: intake, path: intake.yaml }
  - { id: claim_assessment, path: claim_assessment.yaml }
environments:
  prod: envs/prod.yaml
`);

const bundle: LoadedProjectBundle = {
  project: PROJECT,
  sources: {
    policies: {},
    environments: { prod: loadEnvironmentSpec("name: prod\noverrides: {}\n") },
    workflows: { intake: PARENT, claim_assessment: CHILD },
    profiles: emptyProfileSources(),
  },
};

const schemas = {
  "schemas:Item": z.object({ id: z.string() }),
  "schemas:Assessment": z.object({ ok: z.boolean() }),
};

describe("ProjectControlPlane — sub-workflow projections (#55 slice 3)", () => {
  const cp = () => new ProjectControlPlane(bundle, { schemas });

  it("projects the parent topology with a `workflow` node carrying the child manifest id", () => {
    const topology = cp().bundleTopology("intake", "prod");
    expect(topology.nodes).toEqual([{ id: "assess_claim", kind: "workflow", workflow: "claim_assessment" }]);
  });

  it("resolves the parent plan with the child plan + identity embedded (resolve_plan contract)", () => {
    const resolved = cp().resolvedPlan("intake", "prod");
    const step = resolved.plan.steps[0] as unknown as Record<string, unknown>;
    expect(step["kind"]).toBe("subworkflow");
    expect(step["workflowId"]).toBe("claim_assessment");
    expect(step["workflowName"]).toBe("ClaimAssessment");
    expect(typeof step["childDigest"]).toBe("string");
    expect((step["plan"] as { steps: unknown[] }).steps).toHaveLength(1);
  });

  it("builds the full resolved bundle (topology + steps + activities) for the parent", () => {
    const b = cp().bundle("intake", "prod");
    expect(b.topology.nodes.map((n) => n.kind)).toEqual(["workflow"]);
  });

  it("the bundle `steps` array DELIBERATELY omits sub-workflow steps (topology is the full view)", () => {
    // Sub-workflow steps call no activity of the parent's; per-activity effective options
    // belong to the CHILD's own bundle. The topology carries the `workflow` node instead.
    const b = cp().bundle("intake", "prod");
    expect(b.steps).toEqual([]);
    expect(b.topology.nodes.map((n) => n.id)).toEqual(["assess_claim"]);
  });

  it("422s a parent whose sub-workflow reference is undeclared in the manifest", () => {
    const broken: LoadedProjectBundle = {
      ...bundle,
      sources: {
        ...bundle.sources,
        workflows: { intake: PARENT.replace("workflow: claim_assessment", "workflow: nope"), claim_assessment: CHILD },
      },
    };
    expect(() => new ProjectControlPlane(broken, { schemas }).bundleTopology("intake", "prod")).toThrow(
      ProjectControlPlaneError,
    );
  });

  it("422s the PARENT bundle when the CHILD references an undeclared activity (#55 review round)", () => {
    // The child's step calls an activity its own spec never declares — the parent's projection
    // must reject here (assertDeclaredActivityGraph recurses into embedded child plans), not
    // defer the failure to a cryptic child-runtime error.
    const brokenChild = CHILD.replace("steps: [{ id: assess, activity: assess }]", "steps: [{ id: assess, activity: ghost }]");
    const broken: LoadedProjectBundle = {
      ...bundle,
      sources: { ...bundle.sources, workflows: { intake: PARENT, claim_assessment: brokenChild } },
    };
    let thrown: unknown;
    try {
      new ProjectControlPlane(broken, { schemas }).bundle("intake", "prod");
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(ProjectControlPlaneError);
    expect((thrown as ProjectControlPlaneError).status).toBe(422);
    expect((thrown as Error).message).toMatch(/sub-workflow "claim_assessment".*undeclared activities: ghost/s);
  });

  it("surfaces a declared-but-BROKEN sibling's real resolution error (not 'not declared')", () => {
    // The child is declared in the manifest but its spec is invalid YAML config — the parent's
    // projection must carry the child's real error, prefixed with the reference context.
    const broken: LoadedProjectBundle = {
      ...bundle,
      sources: {
        ...bundle.sources,
        workflows: { intake: PARENT, claim_assessment: `${CHILD}\nbogus_top_level_key: 1\n` },
      },
    };
    let thrown: unknown;
    try {
      new ProjectControlPlane(broken, { schemas }).bundleTopology("intake", "prod");
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(ProjectControlPlaneError);
    expect((thrown as ProjectControlPlaneError).status).toBe(422);
    expect((thrown as Error).message).toMatch(/claim_assessment.*failed to resolve/s);
    expect((thrown as Error).message).not.toMatch(/not declared in the project manifest/);
  });

  it("the bundle risk_tier reflects the ENFORCED (cascade-lifted) tier, cascade as explanation (#300)", () => {
    // A safe parent embedding a human_gated child: the top-level posture is what
    // admission enforces (effective human_gated, floor_source cascade:<member>, the
    // LIFTED tier's requirements); the cascade block explains the lift. Byte-parity
    // with Python `_bundle_risk_tier`'s cascade mapping.
    const riskyProject = loadProjectSpec(`
version: "1"
name: claims
workflows:
  - { id: intake, path: intake.yaml }
  - { id: claim_assessment, path: claim_assessment.yaml }
policies:
  risk: risk.policy.yaml
environments:
  prod: envs/prod.yaml
validation:
  targets:
    prod-intake: { workflows: [intake], environment: prod, policies: [risk] }
`);
    const withRisk: LoadedProjectBundle = {
      project: riskyProject,
      sources: {
        ...bundle.sources,
        policies: {
          risk: loadPolicySpec("name: risk\nrisk_tiers: { human_gated: { require_review: true } }\n"),
        },
        workflows: {
          intake: PARENT.replace("\n  input: schemas:Item\n", "\n  input: schemas:Item\n  risk_tier: safe\n"),
          claim_assessment: CHILD.replace(
            "\n  input: schemas:Item\n",
            "\n  input: schemas:Item\n  risk_tier: human_gated\n",
          ),
        },
      },
    };
    const b = new ProjectControlPlane(withRisk, { schemas }).bundle("intake", "prod");
    expect(b.risk_tier).toEqual({
      declared: "safe",
      effective: "human_gated", // the ENFORCED tier — never the pre-lift base
      floor: "safe",
      floor_source: "cascade:claim_assessment",
      requirements: [{ name: "require_review", satisfied: false }],
      cascade: {
        lifted_by: "claim_assessment",
        effective: "human_gated",
        requirements: [{ name: "require_review", satisfied: false }],
      },
    });
  });

  it("the bundle surfaces require_payload_codec from a human_gated tier (#188 D188-2)", () => {
    // A human_gated child with no runtime.temporal.payload_codec: the bundle must show
    // what admission enforces — the require_payload_codec requirement, UNSATISFIED.
    const codecProject = loadProjectSpec(`
version: "1"
name: claims
workflows:
  - { id: claim_assessment, path: claim_assessment.yaml }
policies:
  regulated: regulated.policy.yaml
environments:
  prod: envs/prod.yaml
validation:
  targets:
    prod-claim: { workflows: [claim_assessment], environment: prod, policies: [regulated] }
`);
    const withCodecPolicy: LoadedProjectBundle = {
      project: codecProject,
      sources: {
        ...bundle.sources,
        policies: {
          regulated: loadPolicySpec(
            "name: regulated\nrisk_tiers: { human_gated: { require_payload_codec: true } }\n",
          ),
        },
        workflows: {
          claim_assessment: CHILD.replace(
            "\n  input: schemas:Item\n",
            "\n  input: schemas:Item\n  risk_tier: human_gated\n",
          ),
        },
      },
    };
    const b = new ProjectControlPlane(withCodecPolicy, { schemas }).bundle("claim_assessment", "prod");
    expect(b.risk_tier?.effective).toBe("human_gated");
    expect(b.risk_tier?.requirements).toContainEqual({ name: "require_payload_codec", satisfied: false });
  });
});

describe("subworkflow_visibility validation notice (#55 §6 mitigation b)", () => {
  const checksFor = (parentText: string) => {
    const withParent: LoadedProjectBundle = {
      ...bundle,
      sources: { ...bundle.sources, workflows: { intake: parentText, claim_assessment: CHILD } },
    };
    const report = new ProjectControlPlane(withParent, { schemas }).validate({ environmentId: "prod" });
    const resolved = report.resolved_workflows.find((entry) => entry.workflow_id === "intake");
    return resolved?.checks ?? [];
  };

  it("emits the check for sub-workflow-using workflows, with the notice when version+no attribute", () => {
    const versioned = PARENT.replace("  output: schemas:Assessment\n", "  output: schemas:Assessment\n  version: v1\n");
    const checks = checksFor(versioned);
    const check = checks.find((entry) => entry.code === "subworkflow_visibility");
    expect(check?.status).toBe("passed"); // A notice, never a rejection.
    expect(check?.details).toMatchObject({
      search_attribute_configured: false,
      workflow_version: "v1",
    });
    expect(String((check?.details as Record<string, unknown>)["notice"])).toMatch(/configure\s+the search attribute/);
    // Placed immediately after the workflow_graph check (cross-edition order pin).
    const codes = checks.map((entry) => entry.code);
    expect(codes.indexOf("subworkflow_visibility")).toBe(codes.indexOf("workflow_graph") + 1);
  });

  it("omits the notice when no version label is declared; omits the CHECK for V1 workflows", () => {
    const noVersion = checksFor(PARENT).find((entry) => entry.code === "subworkflow_visibility");
    expect(noVersion?.status).toBe("passed");
    expect(Object.hasOwn(noVersion?.details ?? {}, "notice")).toBe(false);
    // The CHILD itself has no sub-workflow references -> no check at all (V1 output unchanged).
    const report = new ProjectControlPlane(bundle, { schemas }).validate({ environmentId: "prod" });
    const child = report.resolved_workflows.find((entry) => entry.workflow_id === "claim_assessment");
    expect(child?.checks.some((entry) => entry.code === "subworkflow_visibility")).toBe(false);
  });
});
