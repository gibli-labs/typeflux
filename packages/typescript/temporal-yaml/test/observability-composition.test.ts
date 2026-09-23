/**
 * #756 review round: the ONE required-observability gate sits in assembleYamlRuntime — the
 * site every build route traverses — so a DIRECT caller passing `policy` with no observer
 * fails closed too (not just the autoWire routes), and an explicitly-supplied observer
 * satisfies the requirement by construction. Plus the closure consistency rule at the #748
 * altitude: a composed worker builds ONE observer (the parent's), so a child declaring a
 * DIFFERENT backend is a loud load error, never silently-ignored config.
 */

import { describe, expect, it } from "vitest";
import { z } from "zod";

import { NO_OP_OBSERVER, type ModelProvider } from "@typeflux/temporal";

import {
  assembleYamlRuntime,
  composeProjectPolicies,
  loadPolicySpec,
  loadYamlSpec,
  ObservabilityCompositionError,
  projectSubworkflowResolver,
} from "../src/index.js";

const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
const schemas = { "schemas:X": z.object({ id: z.string() }) };

/** A workflow spec with a configurable observability block ("" = absent) and steps. */
const specText = (name: string, observability: string, steps: string): string => `
project: p
name: ${name}
task_queue: q
runtime:
  temporal: {}
${observability === "" ? "" : `  observability:\n    type: ${observability}\n`}  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities:
  definitions:
    - { name: act_${name}, input: "schemas:X", output: "schemas:X", prompt: p/x }
workflow:
  name: ${name}
  input: schemas:X
  output: schemas:X
  steps:
${steps}
`;

const policyWith = (yaml: string) =>
  composeProjectPolicies([{ id: "org", spec: loadPolicySpec(`name: org\n${yaml}`) }], ["org"]);

const REQUIRED = () => policyWith("observability: { required: true }");
const NOT_REQUIRED = () => policyWith("observability: { required: false }");

describe("required-observability gate in assembleYamlRuntime (#756 review round)", () => {
  const spec = () => loadYamlSpec(specText("solo", "langfuse", "    - { id: s, activity: act_solo }\n"));

  it("a DIRECT assembleYamlRuntime caller with a required policy and NO observer fails closed", () => {
    // The direct path never runs autoWireRuntimeOptions, so no observer can appear from the
    // environment — under observability.required that is an untraced run, refused with the
    // exact remedy (export the keys and build through buildRuntime, or inject an observer).
    expect(() => assembleYamlRuntime(spec(), { provider, schemas, policy: REQUIRED() })).toThrow(
      /observability\.required.*LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY/s,
    );
  });

  it("names the langsmith env var for a langsmith spec", () => {
    const langsmith = loadYamlSpec(specText("solo", "langsmith", "    - { id: s, activity: act_solo }\n"));
    expect(() => assembleYamlRuntime(langsmith, { provider, schemas, policy: REQUIRED() })).toThrow(
      /observability\.required.*LANGSMITH_API_KEY/s,
    );
  });

  it("an explicitly-supplied observer satisfies the requirement by construction", () => {
    const { activities } = assembleYamlRuntime(spec(), {
      provider,
      schemas,
      policy: REQUIRED(),
      observer: NO_OP_OBSERVER,
    });
    expect(Object.keys(activities)).toContain("act_solo");
  });

  it("a policy that does NOT require observability leaves the no-observer path unchanged (back-compat)", () => {
    const { activities } = assembleYamlRuntime(spec(), { provider, schemas, policy: NOT_REQUIRED() });
    expect(Object.keys(activities)).toContain("act_solo");
  });

  it("no policy at all assembles ungated (back-compat)", () => {
    const { activities } = assembleYamlRuntime(spec(), { provider, schemas });
    expect(Object.keys(activities)).toContain("act_solo");
  });
});

describe("composed-closure observability consistency (#756 at the #748 altitude)", () => {
  const parentText = (observability: string) =>
    specText("Parent", observability, "    - { id: own, activity: act_Parent }\n    - { id: sub, workflow: child }\n");
  const resolverFor = (specs: Record<string, string>) =>
    projectSubworkflowResolver("parent", (id) => (id in specs ? loadYamlSpec(specs[id]!) : undefined));

  it("a child declaring a DIFFERENT backend is a loud load error naming both workflows and both types", () => {
    // The finder's exact scenario: parent langfuse (credentials or not — this is structural),
    // child langsmith. Before this check the child's declaration was dead config: the worker
    // built ONE langfuse observer and never resolved or credential-checked langsmith.
    const parent = loadYamlSpec(parentText("langfuse"));
    const specs = { child: specText("Child", "langsmith", "    - { id: run, activity: act_Child }\n") };
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor(specs), observer: NO_OP_OBSERVER }),
    ).toThrow(ObservabilityCompositionError);
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor(specs), observer: NO_OP_OBSERVER }),
    ).toThrow(/"child".*"langsmith".*"parent".*"langfuse"/s);
  });

  it("a child with ABSENT observability inherits the parent's (admission's absent ≡ none collapse)", () => {
    const parent = loadYamlSpec(parentText("langfuse"));
    const specs = { child: specText("Child", "", "    - { id: run, activity: act_Child }\n") };
    const { activities } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor(specs),
      observer: NO_OP_OBSERVER,
    });
    expect(Object.keys(activities)).toContain("act_Child");
  });

  it("an explicit `type: none` child inherits too (identical to absent, mirroring admission)", () => {
    const parent = loadYamlSpec(parentText("langfuse"));
    const specs = { child: specText("Child", "none", "    - { id: run, activity: act_Child }\n") };
    const { activities } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor(specs),
      observer: NO_OP_OBSERVER,
    });
    expect(Object.keys(activities)).toContain("act_Child");
  });

  it("a child declaring a backend the parent does NOT declare conflicts (parent none, child langfuse)", () => {
    // The inverted gap: the parent's effective backend is none, so NO observer is ever built —
    // the child's langfuse declaration would be silently ignored.
    const parent = loadYamlSpec(parentText(""));
    const specs = { child: specText("Child", "langfuse", "    - { id: run, activity: act_Child }\n") };
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor(specs) }),
    ).toThrow(/"child".*"langfuse".*"parent".*"none"/s);
  });

  it("a consistent closure under a REQUIRED policy with an observer supplied assembles and serves both", () => {
    const parent = loadYamlSpec(parentText("langfuse"));
    const specs = { child: specText("Child", "langfuse", "    - { id: run, activity: act_Child }\n") };
    const { activities } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor(specs),
      policy: REQUIRED(),
      observer: NO_OP_OBSERVER,
    });
    expect(Object.keys(activities)).toEqual(expect.arrayContaining(["act_Parent", "act_Child"]));
  });
});
