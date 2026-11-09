import assert from "node:assert/strict";
import { z } from "zod";
import { executeActivity } from "@typeflux/temporal";
import { FakeProvider } from "@typeflux/temporal/testing";
import {
  buildLocalRuntime, defineActivitiesFromSpec, evaluatePlanWhen, inlineRegistryFromSpec,
  loadYamlSpec, workflowPlanFromSpec, validateLocalCapabilities, requireTemporalEngine, RuntimeCapabilityError,
} from "@typeflux/yaml";

const yaml = `
project: planning_fixture
name: demo
task_queue: existing-temporal-default
runtime:
  engine: temporal
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
  .replace("  engine: temporal\n", ""));
assert.deepEqual(validateLocalCapabilities(local), ["demo"]);
assert.throws(() => requireTemporalEngine(local), RuntimeCapabilityError);
const spec = loadYamlSpec(yaml);
const plan = workflowPlanFromSpec(spec);
assert.equal(plan.steps.length, 1);
assert.equal(plan.steps[0].id, "echo");
assert.equal(evaluatePlanWhen(plan.steps[0].when, { input: { text: "go" } }), true);
assert.equal(evaluatePlanWhen(plan.steps[0].when, { input: { text: "stop" } }), false);
assert.throws(() => workflowPlanFromSpec(loadYamlSpec(yaml.replace("input.text", "future.text"))), /future/);

// Both direct activities and complete local YAML runs use the existing pipeline.
const activities = defineActivitiesFromSpec(spec, { schemas: { "schemas:Value": z.object({ text: z.string() }) } });
const result = await executeActivity(activities.echo, { text: "hello" }, {
  registry: inlineRegistryFromSpec(spec), provider: new FakeProvider([{ text: "accepted" }]),
});
assert.deepEqual(result, { text: "accepted" });
const runtime = await buildLocalRuntime(local, {
  schemas: {"schemas:Value": z.object({text: z.string()})},
  provider: new FakeProvider([{text: "local accepted"}]),
});
assert.deepEqual(await runtime.executeWorkflow({text: "hello"}), {text: "local accepted"});
assert.deepEqual(await runtime.executeWorkflow({text: "stop"}), {text: "stop"});
console.log("TypeScript YAML planning and local workflow execution passed without Temporal");
