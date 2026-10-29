import assert from "node:assert/strict";
import { z } from "zod";
import { executeActivity } from "@typeflux/temporal";
import { FakeProvider } from "@typeflux/temporal/testing";
import {
  defineActivitiesFromSpec, evaluatePlanWhen, inlineRegistryFromSpec,
  loadYamlSpec, workflowPlanFromSpec, validateLocalCapabilities, requireTemporalEngine, RuntimeCapabilityError,
} from "@typeflux/yaml";

const yaml = `
project: planning_fixture
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
const local = loadYamlSpec(yaml.replace("task_queue: existing-temporal-default\n", "")
  .replace("  temporal: {}", "  engine: local"));
assert.deepEqual(validateLocalCapabilities(local), ["demo"]);
assert.throws(() => requireTemporalEngine(local), RuntimeCapabilityError);
const spec = loadYamlSpec(yaml);
const plan = workflowPlanFromSpec(spec);
assert.equal(plan.steps.length, 1);
assert.equal(plan.steps[0].id, "echo");
assert.equal(evaluatePlanWhen(plan.steps[0].when, { input: { text: "go" } }), true);
assert.equal(evaluatePlanWhen(plan.steps[0].when, { input: { text: "stop" } }), false);
assert.throws(() => workflowPlanFromSpec(loadYamlSpec(yaml.replace("input.text", "future.text"))), /future/);

// Use the existing activity executor; this probe is not a YAML workflow runner.
const activities = defineActivitiesFromSpec(spec, { schemas: { "schemas:Value": z.object({ text: z.string() }) } });
const result = await executeActivity(activities.echo, { text: "hello" }, {
  registry: inlineRegistryFromSpec(spec), provider: new FakeProvider([{ text: "accepted" }]),
});
assert.deepEqual(result, { text: "accepted" });
console.log("TypeScript YAML planning and activity execution passed without Temporal");
