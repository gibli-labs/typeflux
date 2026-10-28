import { describe, expect, it } from "vitest";
import { loadYamlSpec, workflowPlanFromSpec, evaluatePlanWhen } from "../src/index.js";

const yaml = `
project: p
name: demo
task_queue: existing-temporal-default
runtime:
  temporal: {}
  registry: {type: inline, prompts: {echo: 'Echo {{text}}'}}
  provider: {type: fake}
activities:
  definitions:
    - {name: echo, input: 'schemas:Value', output: 'schemas:Value', prompt: echo}
workflow:
  name: Demo
  input: schemas:Value
  output: schemas:Value
  steps:
    - id: echo
      activity: echo
      when: {path: input.text, neq: stop}
`;

describe("shared YAML planning", () => {
  it("loads and plans the existing language through the neutral entry point", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(yaml));
    expect(plan.steps).toHaveLength(1);
    expect(plan.steps[0]).toMatchObject({kind: "activity", id: "echo", activity: "echo"});
    const when = plan.steps[0]!.when!;
    expect(evaluatePlanWhen(when, {input: {text: "go"}})).toBe(true);
    expect(evaluatePlanWhen(when, {input: {text: "stop"}})).toBe(false);
  });

  it("rejects unavailable context roots through the shared planner", () => {
    expect(() => workflowPlanFromSpec(loadYamlSpec(yaml.replace("input.text", "future.text"))))
      .toThrow(/future/);
  });
});
