/** Resolve local execution schemas before any runtime service is constructed. */
import { activitySlotJsonSchema, canonicalJson, type ActivityDescriptor } from "@typeflux/temporal";
import { z } from "zod";
import type { TypefluxYamlSpec, WorkflowStepSpec } from "./spec.js";
import type { WorkflowPlan, WorkflowPlanStep } from "./workflow-plan.js";

export interface LocalContract {
  spec: TypefluxYamlSpec;
  plan: WorkflowPlan;
  input: z.ZodType;
  output: z.ZodType;
  collects: Map<string, z.ZodType>;
  children: Map<string, LocalContract>;
}

function unwrap(schema: z.ZodType): z.ZodType {
  while (schema instanceof z.ZodOptional || schema instanceof z.ZodNullable || schema instanceof z.ZodDefault) {
    schema = schema.unwrap() as z.ZodType;
  }
  return schema;
}

function field(schema: z.ZodType, name: string, where: string): z.ZodType {
  const object = unwrap(schema);
  if (!(object instanceof z.ZodObject) || !Object.hasOwn(object.shape, name)) {
    throw new Error(`${where}: schema has no field ${JSON.stringify(name)}`);
  }
  return object.shape[name] as z.ZodType;
}

function same(actual: z.ZodType, expected: z.ZodType, where: string): void {
  if (actual.safeParse(null).success && !expected.safeParse(null).success) {
    throw new Error(`${where}: nullable output cannot feed a non-nullable contract`);
  }
  // Contracts use the same schema identity as activity definitions. This intentionally
  // requires equal shapes, rather than implementing another schema-subtyping system.
  if (canonicalJson(activitySlotJsonSchema(unwrap(actual), "output")) !==
      canonicalJson(activitySlotJsonSchema(unwrap(expected), "output"))) {
    throw new Error(`${where}: incompatible input/output schema contracts`);
  }
}

function collectFields(schema: z.ZodType, produced: readonly string[], where: string): void {
  const object = unwrap(schema);
  if (!(object instanceof z.ZodObject)) throw new Error(`${where}: collect.output must be an object schema`);
  for (const [name, slot] of Object.entries(object.shape)) {
    if (!produced.includes(name) && !(slot as z.ZodType).safeParse(undefined).success) {
      throw new Error(`${where}: collect requires field ${name} which the step does not produce`);
    }
  }
}

export function compileLocalContract(
  spec: TypefluxYamlSpec,
  plan: WorkflowPlan,
  schemas: Record<string, z.ZodType>,
  descriptors: Record<string, ActivityDescriptor<any, any>>,
  specFor: (id: string) => TypefluxYamlSpec | undefined,
): LocalContract {
  const resolve = (ref: string | undefined, where: string): z.ZodType => {
    if (ref === undefined || !Object.hasOwn(schemas, ref)) throw new Error(`${where}: unresolved schema ${JSON.stringify(ref)}`);
    return schemas[ref]!;
  };
  const collects = new Map<string, z.ZodType>();
  const children = new Map<string, LocalContract>();
  const first = plan.steps[0];
  const inferredInput = first?.kind === "activity" ? descriptors[first.activity]?.input as z.ZodType | undefined : undefined;
  const input = spec.workflow.input === undefined && inferredInput !== undefined
    ? inferredInput : resolve(spec.workflow.input, `${spec.workflow.name} workflow.input`);
  const sequence = (steps: WorkflowPlanStep[], specs: WorkflowStepSpec[], incoming: z.ZodType,
    context: Map<string, z.ZodType>): z.ZodType => {
    let current = incoming;
    const exits: z.ZodType[] = [];
    for (const [index, step] of steps.entries()) {
      const source = specs[index]!;
      const where = `${spec.workflow.name} step ${JSON.stringify(step.id)}`;
      if (step.when !== undefined) exits.push(current);
      if (step.kind === "activity") {
        const descriptor = descriptors[step.activity]!;
        same(current, descriptor.input, where);
        current = descriptor.output;
      } else if (step.kind === "parallel") {
        const collect = resolve(source.parallel!.collect.output, `${where} collect.output`);
        collectFields(collect, step.branches.map(branch => branch.id), where);
        const branchContexts: Map<string, z.ZodType>[] = [];
        for (const [i, branch] of step.branches.entries()) {
          const scoped = new Map(context);
          const output = sequence(branch.steps, source.parallel!.branches[i]!.steps, current, scoped);
          const slot = field(collect, branch.id, where);
          same(output, slot, `${where} branch ${branch.id}`);
          if (branch.when !== undefined && !slot.safeParse(null).success) {
            throw new Error(`${where} branch ${branch.id}: gated collect field must accept null`);
          }
          branchContexts.push(scoped);
        }
        // Branch ids are globally unique; siblings compile in isolated contexts.
        for (const scoped of branchContexts) for (const [id, schema] of scoped) context.set(id, schema);
        collects.set(step.id, collect);
        current = collect;
      } else {
        let targetInput: z.ZodType;
        let targetOutput: z.ZodType;
        if (step.kind === "subworkflow" || step.kind === "subworkflowMap") {
          const childSpec = specFor(step.workflowId);
          if (childSpec === undefined) throw new Error(`${where}: missing child ${step.workflowId}`);
          const child = compileLocalContract(childSpec, step.plan, schemas, descriptors, specFor);
          children.set(step.id, child);
          targetInput = child.input;
          targetOutput = child.output;
        } else {
          targetInput = descriptors[step.activity]!.input;
          targetOutput = descriptors[step.activity]!.output;
        }
        if (step.kind === "subworkflow") {
          same(current, targetInput, where);
          current = targetOutput;
        } else {
          const [root, ...path] = step.over.split(".");
          let over = context.get(root!);
          if (over === undefined) throw new Error(`${where}: unavailable map path ${step.over}`);
          for (const part of path) over = field(over, part, `${where} map.over`);
          const array = unwrap(over);
          if (!(array instanceof z.ZodArray)) throw new Error(`${where}: map.over must resolve to an array`);
          same(array.element as z.ZodType, targetInput, `${where} map item`);
          if (source.map!.collect !== undefined) {
            const collect = resolve(source.map!.collect.output, `${where} collect.output`);
            if (step.collectField === undefined) throw new Error(`${where}: declare collect.field for a typed collection`);
            collectFields(collect, [step.collectField], where);
            const slot = unwrap(field(collect, step.collectField, where));
            if (!(slot instanceof z.ZodArray)) throw new Error(`${where}: collect.field must be an array`);
            same(targetOutput, slot.element as z.ZodType, `${where} collect item`);
            collects.set(step.id, collect);
            current = collect;
          } else current = z.array(targetOutput);
        }
      }
      context.set(step.id, current);
    }
    for (const exit of exits) same(exit, current, `${spec.workflow.name} conditional sequence exit`);
    return current;
  };
  const terminal = sequence(plan.steps, spec.workflow.steps, input, new Map([["input", input]]));
  const output = spec.workflow.output === undefined ? terminal : resolve(spec.workflow.output, `${spec.workflow.name} workflow.output`);
  same(terminal, output, `${spec.workflow.name} workflow.output`);
  return { spec, plan, input, output, collects, children };
}
