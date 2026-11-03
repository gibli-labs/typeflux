/** Non-durable, foreground YAML execution through the existing activity pipeline. */
import { randomUUID } from "node:crypto";
import {
  ActivityCancelledError, collectMetadata, executeActivity, fanOut, prepareSessionCache,
  ProviderCacheUnavailableError, type ActivityDescriptor, type CachedSessionHandle,
  type ExecuteActivityOptions,
} from "@typeflux/temporal";
import { projectSubworkflowResolver } from "./build-workflow.js";
import { RuntimeCapabilityError, validateLocalCapabilities } from "./engine.js";
import { compileLocalContract, type LocalContract } from "./local-contract.js";
import { LocalRuntimeContributor } from "./local-metadata.js";
import { assembleRuntimeServices, type YamlRuntimeOptions } from "./runtime-assembly.js";
import { typefluxYamlSpec, type TypefluxYamlSpec } from "./spec.js";
import { effectiveSubjectIds } from "./subjects.js";
import { DEFAULT_MAP_CONCURRENCY, evaluatePlanWhen, resolveContextPath, utf8ByteLength,
  type WorkflowPlan, type WorkflowPlanStep } from "./workflow-plan.js";

export interface LocalRuntimeOptions extends YamlRuntimeOptions {
  deps?: unknown;
  tenant?: Record<string, string>;
}
export interface LocalExecutionOptions {
  signal?: AbortSignal;
  subjectIds?: readonly string[];
}
export interface LocalYamlRuntime {
  /** A detached copy for inspection; mutating it cannot change the admitted program. */
  readonly plan: WorkflowPlan;
  executeWorkflow(input: unknown, options?: LocalExecutionOptions): Promise<unknown>;
}

export class LocalWorkflowError extends Error {
  constructor(public readonly workflow: string, public readonly step: string, cause: unknown) {
    super(`workflow ${JSON.stringify(workflow)} step ${JSON.stringify(step)} failed: ${cause instanceof Error ? cause.message : String(cause)}`, {cause});
    this.name = "LocalWorkflowError";
  }
}

/** Root input rejection, distinct from an activity or workflow result failure. */
export class LocalWorkflowInputError extends Error {
  constructor(public readonly workflow: string, cause: unknown) {
    super(`workflow ${JSON.stringify(workflow)} input rejected: ${cause instanceof Error ? cause.message : String(cause)}`, {cause});
    this.name = "LocalWorkflowInputError";
  }
}

function check(signal: AbortSignal): void {
  if (signal.aborted) throw new ActivityCancelledError("local workflow cancelled", {cause: signal.reason});
}

function portable(descriptors: Record<string, ActivityDescriptor<any, any>>, workflow: string): void {
  for (const [key, descriptor] of Object.entries(descriptors)) {
    if (typeof descriptor !== "object" || descriptor === null || key !== descriptor.name ||
        (descriptor.kind !== undefined && descriptor.kind !== "ai" && descriptor.kind !== "code") ||
        typeof descriptor.input?.parse !== "function" || typeof descriptor.output?.parse !== "function" ||
        (descriptor.kind === "code" ? typeof descriptor.handler !== "function" : descriptor.prompt === undefined)) {
      throw new RuntimeCapabilityError(workflow, `extraActivities.${key}`, "unsupported activity descriptor",
        "Supply a portable defineActivity/defineCodeActivity descriptor; Temporal worker functions require Temporal.");
    }
    for (const field of ["temporal", "retry", "startToCloseTimeout", "heartbeatTimeout", "taskQueue",
      "start_to_close_timeout_seconds", "heartbeat_timeout_seconds", "startToCloseTimeoutMs", "heartbeatTimeoutMs", "retryPolicy", "task_queue"]) {
      if (Object.hasOwn(descriptor, field)) throw new RuntimeCapabilityError(workflow, `extraActivities.${key}.${field}`,
        "Temporal activity policy", "Use runtime.engine: temporal for this activity.");
    }
  }
}

/**
 * Admit the full closure, then assemble shared services and resolve its prompts. The caller
 * owns injected transports/observers. No server, background worker or activity retry is added.
 */
export async function buildLocalRuntime(source: TypefluxYamlSpec, supplied: LocalRuntimeOptions): Promise<LocalYamlRuntime> {
  const spec = typefluxYamlSpec.parse(structuredClone(source));
  const specs = new WeakMap<TypefluxYamlSpec, TypefluxYamlSpec>([[source, spec]]);
  const subworkflows = supplied.subworkflows === undefined ? undefined : projectSubworkflowResolver(
    supplied.subworkflows.selfId ?? spec.workflow.name, id => {
      const sourceChild = supplied.subworkflows!.specFor(id);
      if (sourceChild === undefined) return undefined;
      let child = specs.get(sourceChild);
      if (child === undefined) { child = typefluxYamlSpec.parse(structuredClone(sourceChild)); specs.set(sourceChild, child); }
      return child;
    });
  validateLocalCapabilities(spec, subworkflows);
  portable(supplied.extraActivities ?? {}, spec.workflow.name);
  if (spec.runtime.observability?.type && spec.runtime.observability.type !== "none" && supplied.observer === undefined) {
    throw new Error("runtime.observability is configured; supply observer explicitly to buildLocalRuntime and manage its transport lifecycle");
  }
  const extraActivities = Object.fromEntries(Object.entries(supplied.extraActivities ?? {}).map(([name, descriptor]) => {
    if (descriptor.kind === "code") return [name, {...descriptor}];
    return [name, {
      ...descriptor, prompt: {...descriptor.prompt},
      ...(descriptor.cache === undefined ? {} : {cache: {...descriptor.cache}}),
      ...(descriptor.sessionCache === undefined ? {} : {sessionCache: {...descriptor.sessionCache}}),
      ...(descriptor.providerParams === undefined ? {} : {providerParams: structuredClone(descriptor.providerParams)}),
      ...(descriptor.artifacts === undefined ? {} : {artifacts: structuredClone(descriptor.artifacts)}),
    }];
  }));
  const options: LocalRuntimeOptions = {...supplied, extraActivities, schemas: {...supplied.schemas},
    ...(subworkflows === undefined ? {} : {subworkflows})};
  let contract!: LocalContract;
  const assembled = assembleRuntimeServices(spec, options, (descriptors, plan) => {
    contract = compileLocalContract(spec, plan, options.schemas, descriptors, id => subworkflows?.specFor(id));
  });
  // Prompt resolution is preflight, not activity execution. Even skipped branches must resolve.
  for (const descriptor of Object.values(assembled.descriptors)) {
    if (descriptor.kind !== "code") await assembled.options.registry!.resolve(descriptor.prompt);
  }
  const shared: ExecuteActivityOptions = {...assembled.options,
    ...(options.deps === undefined ? {} : {deps: options.deps}),
    ...(options.tenant === undefined ? {} : {tenant: {...options.tenant}})};
  const contributor = new LocalRuntimeContributor();

  return {
    get plan() { return structuredClone(assembled.plan); },
    async executeWorkflow(input, execution = {}) {
      const controller = new AbortController();
      const abort = () => controller.abort(execution.signal?.reason);
      if (execution.signal?.aborted) abort();
      execution.signal?.addEventListener("abort", abort, {once: true});
      const signal = controller.signal;
      const correlationId = randomUUID();
      // Every nested call shares cancellation and limits, while keeping its own input/context.
      async function invoke(owner: LocalContract, value: unknown, inheritedSubjects?: readonly string[]): Promise<unknown> {
        check(signal);
        let parsed: unknown;
        try { parsed = owner.input.parse(value); }
        catch (error) {
          if (owner === contract) throw new LocalWorkflowInputError(owner.spec.workflow.name, error);
          throw error;
        }
        // Resolve identity at the root only; an inherited empty list is still authoritative.
        const subjects = inheritedSubjects ?? effectiveSubjectIds(execution.subjectIds, parsed,
          (owner.spec.workflow.subjects ?? []).map(s => ({fromPath: s.from, required: s.required})));
        const metadata = collectMetadata({correlationId, contract: owner, descriptors: assembled.descriptors}, [contributor]);
        const observer = shared.observer;
        const callOptions: ExecuteActivityOptions = {...shared, cancellationSignal: signal,
          ...(subjects.length === 0 ? {} : {context: {subjectIds: subjects}}),
          ...(observer === undefined ? {} : {observer: {
            observeActivity(params) {
              const span = observer.observeActivity(params);
              span.updateMetadata(metadata);
              return span;
            },
            flush: () => observer.flush(),
          }})};
        const activity = async (name: string, input: unknown, handle?: CachedSessionHandle): Promise<unknown> => {
          check(signal);
          const descriptor = assembled.descriptors[name]!;
          const validated = descriptor.input.parse(input);
          const run = (cachedSession?: CachedSessionHandle) => executeActivity(descriptor, validated,
            {...callOptions, ...(cachedSession === undefined ? {} : {cachedSession})});
          try { return await run(handle); }
          catch (error) {
            // Same one-shot cache-expiry recovery as the Temporal adapter.
            if (error instanceof ProviderCacheUnavailableError && handle?.supported && handle.style === "reference") {
              return run();
            }
            throw error;
          }
        };
        const concurrent = <T, R>(items: readonly T[], run: (item: T) => Promise<R>, concurrency: number) =>
          fanOut(items, async item => {
            check(signal);
            try { return await run(item); }
            catch (error) { controller.abort(error); throw error; }
          }, {concurrency});
        async function sequence(steps: readonly WorkflowPlanStep[], initial: unknown, context: Record<string, unknown>): Promise<unknown> {
          let current = initial;
          for (const step of steps) {
            check(signal);
            try {
              if (step.when !== undefined && !evaluatePlanWhen(step.when, context)) break;
              if (step.kind === "activity") current = await activity(step.activity, current);
              else if (step.kind === "subworkflow") current = await invoke(owner.children.get(step.id)!, current, subjects);
              else if (step.kind === "parallel") {
                const incoming = current;
                const completed = await concurrent(step.branches, async branch => {
                  const scoped: Record<string, unknown> = Object.assign(Object.create(null), structuredClone(context));
                  const value = branch.when !== undefined && !evaluatePlanWhen(branch.when, scoped) ? null
                    : await sequence(branch.steps, structuredClone(incoming), scoped);
                  return {id: branch.id, value, scoped};
                }, Math.max(1, step.branches.length));
                current = Object.fromEntries(completed.map(branch => [branch.id, branch.value]));
                for (const branch of completed) for (const [id, value] of Object.entries(branch.scoped)) {
                  if (!Object.hasOwn(context, id)) context[id] = value;
                }
              } else {
                const items: unknown = resolveContextPath(context, step.over);
                if (!Array.isArray(items)) throw new Error(`map.over ${step.over} must resolve to an array`);
                let handle: CachedSessionHandle | undefined;
                try {
                  if (step.kind === "map" && step.sessionCache?.enabled && items.length > 0) {
                    const descriptor = assembled.descriptors[step.activity]!;
                    handle = await prepareSessionCache(descriptor, {...callOptions,
                      createdAt: new Date().toISOString(), inputValue: descriptor.input.parse(items[0])});
                  }
                  // Cache preparation is awaited even after abort so any created handle can be released.
                  check(signal);
                  const results = await concurrent(items, item => step.kind === "map"
                    ? activity(step.activity, structuredClone(item), handle)
                    : invoke(owner.children.get(step.id)!, structuredClone(item), subjects),
                  step.concurrency ?? DEFAULT_MAP_CONCURRENCY);
                  current = step.collectField === undefined ? results : {[step.collectField]: results};
                } finally {
                  // fanOut drains all started calls before release, including failure/cancellation.
                  if (handle?.cache_id != null) {
                    try { await shared.provider.releaseCachedSession?.(handle); } catch { /* best effort; provider TTL remains */ }
                  }
                }
              }
              if (step.kind === "map" || step.kind === "subworkflowMap" || step.kind === "parallel") {
                const collect = owner.collects.get(step.id);
                if (collect !== undefined) current = collect.parse(current);
                const limit = step.collectMaxBytes;
                if (limit !== undefined && limit > 0 && utf8ByteLength(JSON.stringify(current)) > limit) {
                  throw new Error(`collected payload exceeds collect.max_bytes=${limit}; shrink outputs or raise the limit`);
                }
              }
              check(signal);
              context[step.id] = current;
            } catch (error) {
              if (error instanceof ActivityCancelledError || error instanceof LocalWorkflowError) throw error;
              throw new LocalWorkflowError(owner.spec.workflow.name, step.id, error);
            }
          }
          return current;
        }
        return owner.output.parse(await sequence(owner.plan.steps, parsed, Object.assign(Object.create(null), {input: parsed})));
      }
      try { return await invoke(contract, input); }
      finally { execution.signal?.removeEventListener("abort", abort); }
    },
  };
}
