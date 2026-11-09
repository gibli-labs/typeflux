import { readFileSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { ActivityCancelledError, cachedSessionHandleSchema, CollectingObserver, defineCodeActivity,
  InMemoryCacheStore, ProviderTransientError, TraceWriter, type EmittedTrace, type ModelProvider, type StructuredCallParams } from "@typeflux/temporal";
import { buildLocalRuntime, typefluxYamlSpec, type TypefluxYamlSpec } from "../src/index.js";
import { composeProjectPolicies } from "../src/policy-composition.js";
import { loadPolicySpec } from "../src/policy.js";

const fixtures = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/execution-cases.json", import.meta.url), "utf8"));
const Item = z.object({n: z.number().int()});
const Batch = z.object({items: z.array(Item)});
const schemas = {"models:Item": Item, "models:Batch": Batch, "models:Branches": z.object({left: Item, right: Item.nullable()})};
const spec = (patch: Record<string, unknown> = {}): TypefluxYamlSpec => typefluxYamlSpec.parse({...structuredClone(fixtures.base), ...patch});
const mapSpec = (): TypefluxYamlSpec => spec({workflow: {name: "Root", ...fixtures.cases[4].workflow}});
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
class Provider implements ModelProvider {
  providerName = "fake";
  calls: number[] = [];
  active = 0;
  peak = 0;
  async structuredCall(params: StructuredCallParams) {
    const n = Number(params.messages.at(-1)!.content);
    this.calls.push(n); this.active++; this.peak = Math.max(this.peak, this.active);
    try { await delay(10 - Math.min(n, 9)); return {n: n + 1}; }
    finally { this.active--; }
  }
}

describe("local YAML execution", () => {
  it("matches the shared Python and TypeScript execution fixtures", async () => {
    for (const test of fixtures.cases) {
      const provider = new Provider();
      const source = (id: string, workflow: object) => {
        const patch = test.spec_overrides?.[id] ?? {};
        return spec({...patch, name: id, runtime: {...structuredClone(fixtures.base.runtime), ...patch.runtime}, workflow: {name: id, ...workflow}});
      };
      const root = source("root", test.workflow);
      const children = Object.fromEntries(Object.entries(test.children).map(([id, workflow]) => [id, source(id, workflow as object)]));
      if (test.error) {
        await expect(buildLocalRuntime(root, {schemas, provider, subworkflows: {selfId: "root", specFor: id => children[id]}})).rejects.toThrow(test.error);
        expect(provider.calls).toEqual([]);
        continue;
      }
      const runtime = await buildLocalRuntime(root, {schemas, provider, subworkflows: {selfId: "root", specFor: id => children[id]}});
      expect(await runtime.executeWorkflow(test.input), test.name).toEqual(test.expected);
      expect(test.call_order === "unordered" ? [...provider.calls].sort((a, b) => a - b) : provider.calls, test.name)
        .toEqual(test.call_order === "unordered" ? [...test.calls].sort((a, b) => a - b) : test.calls);
      expect(provider.peak).toBeLessThanOrEqual(2);
    }
  });

  it("executes omitted-engine YAML through the Local pipeline", async () => {
    const raw = structuredClone(fixtures.base);
    delete raw.runtime.engine;
    const provider = new Provider();
    const runtime = await buildLocalRuntime(typefluxYamlSpec.parse(raw), {schemas, provider});
    expect(await runtime.executeWorkflow({n: 1})).toEqual({n: 2});
    expect(provider.calls).toEqual([1]);
  });

  it("snapshots admission inputs and keeps invocations independent", async () => {
    const source = spec();
    const provider = new Provider();
    const runtime = await buildLocalRuntime(source, {schemas, provider});
    source.workflow.steps[0]!.activity = "missing";
    runtime.plan.steps.splice(0);
    expect(await Promise.all([runtime.executeWorkflow({n: 1}), runtime.executeWorkflow({n: 2})])).toEqual([{n: 2}, {n: 3}]);
    await expect(runtime.executeWorkflow({n: "bad"})).rejects.toThrow();
    expect(provider.calls).toEqual([1, 2]);
  });

  it("uses portable code descriptors and rejects worker functions and Temporal policies", async () => {
    const provider = new Provider();
    const source = spec({activities: {definitions: []}});
    const descriptor = defineCodeActivity({name: "echo", input: Item, output: Item, handler: input => ({n: input.n + 2})});
    const runtime = await buildLocalRuntime(source, {schemas, provider, extraActivities: {echo: descriptor}});
    expect(await runtime.executeWorkflow({n: 1})).toEqual({n: 3});
    expect(provider.calls).toEqual([]);
    for (const extra of [() => 3, {...descriptor, heartbeatTimeout: 100}, {...descriptor, name: "different"}]) {
      await expect(buildLocalRuntime(source, {schemas, provider, extraActivities: {echo: extra as never}})).rejects.toThrow(/portable|Temporal/);
    }
  });

  it("rejects the complete skipped closure before provider construction", async () => {
    const root = spec({workflow: {name: "Root", input: "models:Item", output: "models:Item", steps: [
      {id: "child", workflow: "child", when: {path: "input.n", eq: -1}},
    ]}});
    let constructed = 0;
    root.runtime.provider.type = "openai";
    const options = {schemas, transports: {get openai() {constructed++; return vi.fn() as never;}}};
    for (const failure of ["capability", "type", "reference", "cycle", "collision"] as const) {
      const child = spec({name: "Child"});
      if (failure === "capability") child.workflow.risk_tier = "human_gated";
      if (failure === "type") child.workflow.output = "models:Batch";
      if (failure === "reference") child.workflow.steps[0]!.activity = "missing";
      if (failure === "cycle") child.workflow.steps = [{id: "self", workflow: "child"}];
      if (failure === "collision") child.activities.definitions![0]!.validation_retries = 5;
      await expect(buildLocalRuntime(root, {...options, subworkflows: {specFor: () => child}})).rejects.toThrow();
    }
    expect(constructed).toBe(0);
  });

  it("rejects optional outputs at required workflow and map boundaries before execution", async () => {
    const handler = vi.fn(() => undefined);
    const descriptor = defineCodeActivity({name: "echo", input: Item, output: Item.optional(), handler});
    for (const source of [spec(), mapSpec()]) {
      source.activities.definitions = [];
      await expect(buildLocalRuntime(source, {schemas, provider: new Provider(), extraActivities: {echo: descriptor}}))
        .rejects.toThrow(/optional output cannot feed a required contract/);
    }
    expect(handler).not.toHaveBeenCalled();
    // A default consumes absent input and produces a required value; it is not optional output.
    const defaulted = defineCodeActivity({name: "echo", input: Item, output: Item.default({n: 2}), handler: () => ({n: 2})});
    const runtime = await buildLocalRuntime(spec({activities: {definitions: []}}),
      {schemas, provider: new Provider(), extraActivities: {echo: defaulted}});
    expect(await runtime.executeWorkflow({n: 1})).toEqual({n: 2});
  });

  it("validates resolved collection schemas before prompt resolution", async () => {
    const resolve = vi.fn();
    const source = mapSpec();
    source.runtime.registry = {type: "custom"};
    const provider = new Provider();
    for (const wrong of [z.object({other: z.array(Item)}), z.object({items: Item}), z.object({items: z.array(z.object({text: z.string()}))})]) {
      await expect(buildLocalRuntime(source, {provider, schemas: {...schemas, "models:Batch": wrong}, registry: {resolve}})).rejects.toThrow(/schema|array|field/);
    }
    expect(resolve).not.toHaveBeenCalled();
    expect(provider.calls).toEqual([]);
  });

  it("shares provider concurrency across maps and invocations", async () => {
    const source = mapSpec();
    source.runtime.provider_limits = {default: {max_concurrent: 1, min_interval_seconds: undefined}, providers: {}};
    const provider = new Provider();
    const runtime = await buildLocalRuntime(source, {schemas, provider});
    await Promise.all([runtime.executeWorkflow({items: [{n: 1}, {n: 2}]}), runtime.executeWorkflow({items: [{n: 3}]})]);
    expect(provider.peak).toBe(1);
    expect(provider.calls).toHaveLength(3);
  });

  it("reuses provider retry without retrying a failed activity", async () => {
    const source = spec();
    source.runtime.provider_retry = {max_attempts: 2, initial_backoff_seconds: 0, jitter_ratio: 0, backoff_multiplier: undefined, retry_rate_limits: true, retry_transient_errors: true};
    let calls = 0;
    const runtime = await buildLocalRuntime(source, {schemas, provider: {structuredCall() {
      if (++calls === 1) throw new ProviderTransientError("try again");
      return {n: 2};
    }}});
    expect(await runtime.executeWorkflow({n: 1})).toEqual({n: 2});
    expect(calls).toBe(2);
    calls = 0;
    const failed = await buildLocalRuntime(source, {schemas, provider: {structuredCall() { calls++; throw new Error("terminal"); }}});
    await expect(failed.executeWorkflow({n: 1})).rejects.toThrow(/Root.*first.*terminal/);
    expect(calls).toBe(1);
  });

  it("stops queued work, aborts siblings and drains before cache release", async () => {
    const source = mapSpec();
    source.activities.definitions![0]!.cache = {enabled: true};
    source.runtime.registry = {type: "inline", prompts: {echo: {messages: [{role: "system", content: "stable"}, {role: "user", content: "{{n}}"}]}}};
    const events: string[] = [];
    const provider: ModelProvider = {
      supportsSessionCache: true, sessionCacheStyle: "reference",
      prepareCachedSession: params => cachedSessionHandleSchema.parse({provider: "fake", identity_hash: params.identityHash, supported: true, style: "reference", cache_id: "cache"}),
      releaseCachedSession: () => { events.push("release"); },
      async structuredCall(params) {
        const n = Number(params.messages.at(-1)!.content); events.push(`start:${n}`);
        if (n === 1) { await delay(5); throw new Error("terminal"); }
        await new Promise<void>(resolve => params.signal!.addEventListener("abort", () => resolve(), {once: true}));
        events.push("drained"); return {n};
      },
    };
    const runtime = await buildLocalRuntime(source, {schemas, provider});
    await expect(runtime.executeWorkflow({items: [{n: 1}, {n: 2}, {n: 3}]})).rejects.toThrow(/terminal/);
    expect(events).toEqual(["start:1", "start:2", "drained", "release"]);
  });

  it("releases a cache created while cancellation was pending without starting items", async () => {
    const source = mapSpec();
    source.activities.definitions![0]!.cache = {enabled: true};
    source.runtime.registry = {type: "inline", prompts: {echo: {messages: [{role: "system", content: "stable"}, {role: "user", content: "{{n}}"}]}}};
    const controller = new AbortController();
    const released = vi.fn(); const called = vi.fn();
    const runtime = await buildLocalRuntime(source, {schemas, provider: {
      supportsSessionCache: true, sessionCacheStyle: "reference", structuredCall: called,
      async prepareCachedSession(params) {
        controller.abort(); await delay(5);
        return cachedSessionHandleSchema.parse({provider: "fake", identity_hash: params.identityHash, supported: true, style: "reference", cache_id: "cache"});
      },
      releaseCachedSession: released,
    }});
    await expect(runtime.executeWorkflow({items: [{n: 1}]}, {signal: controller.signal})).rejects.toBeInstanceOf(ActivityCancelledError);
    expect(called).not.toHaveBeenCalled(); expect(released).toHaveBeenCalledOnce();
  });

  it("enforces collect size and validates collected values at runtime", async () => {
    const source = mapSpec(); source.workflow.steps[0]!.map!.collect!.max_bytes = 1;
    const runtime = await buildLocalRuntime(source, {schemas, provider: new Provider()});
    await expect(runtime.executeWorkflow({items: [{n: 1}]})).rejects.toThrow(/collect.max_bytes=1/);
    const refined = Batch.refine(batch => batch.items.length < 2, "too many items");
    const checked = await buildLocalRuntime(mapSpec(), {schemas: {...schemas, "models:Batch": refined}, provider: new Provider()});
    await expect(checked.executeWorkflow({items: [{n: 1}, {n: 2}]})).rejects.toThrow(/too many items/);
  });

  it("measures validated map and parallel collections including schema defaults", async () => {
    const padded = {
      ...schemas,
      "models:Batch": Batch.extend({padding: z.string().default("é".repeat(100))}),
      "models:Branches": schemas["models:Branches"].extend({padding: z.string().default("é".repeat(100))}),
    };
    for (const test of [fixtures.cases[3], fixtures.cases[4]]) {
      const source = spec({workflow: {name: "Root", ...test.workflow}});
      const step = source.workflow.steps[0]!;
      (step.map?.collect ?? step.parallel!.collect).max_bytes = 50;
      const runtime = await buildLocalRuntime(source, {schemas: padded, provider: new Provider()});
      await expect(runtime.executeWorkflow(test.input)).rejects.toThrow(/collect.max_bytes=50/);
    }
  });

  it("threads root subjects unchanged through child workflows and workflow maps", async () => {
    const SubjectItem = Item.extend({subject: z.string().optional()});
    const subjectSchemas = {...schemas, "models:Item": SubjectItem,
      "models:Batch": z.object({items: z.array(SubjectItem), subject: z.string().optional()})};
    const scenarios: {value?: string; explicit?: string[]; expected: string[]}[] = [
      {expected: []}, {value: "root-subject", expected: ["root-subject"]},
      {explicit: ["override", "override"], expected: ["override"]},
    ];
    for (const test of [fixtures.cases[6], fixtures.cases[7]]) for (const scenario of scenarios) {
      const root = spec({workflow: {name: "Root", ...test.workflow,
        subjects: [{from: "input.subject", required: scenario.explicit !== undefined}]}});
      const children = Object.fromEntries(Object.entries(test.children).map(([id, workflow]) =>
        [id, spec({name: id, workflow: {name: id, ...(workflow as object),
          subjects: [{from: "input.missing", required: true}]}})]));
      for (const source of [root, ...Object.values(children)]) {
        source.activities.definitions![0]!.cross_run_cache = {enabled: true};
      }
      const seen: string[][] = [];
      const cacheStore = new InMemoryCacheStore();
      const writes = vi.spyOn(cacheStore, "set");
      const runtime = await buildLocalRuntime(root, {schemas: subjectSchemas, provider: new Provider(), cacheStore,
        subworkflows: {selfId: "root", specFor: id => children[id]},
        hooks: {echo: (_input, output, context) => { seen.push([...(context.subjectIds ?? [])]); return output; }},
      });
      const input = {...test.input, ...(scenario.value === undefined ? {} : {subject: scenario.value})};
      expect(await runtime.executeWorkflow(input,
        scenario.explicit === undefined ? {} : {subjectIds: scenario.explicit})).toEqual(test.expected);
      const count = test.calls.length;
      expect(seen).toEqual(Array.from({length: count}, () => scenario.expected));
      expect(writes).toHaveBeenCalledTimes(count);
      for (const [, record] of writes.mock.calls) expect(record.subjects ?? []).toEqual(scenario.expected);
    }
    const required = spec(); required.workflow.subjects = [{from: "input.subject", required: true}];
    const provider = new Provider();
    const runtime = await buildLocalRuntime(required, {schemas: subjectSchemas, provider});
    await expect(runtime.executeWorkflow({n: 1}, {subjectIds: []})).rejects.toThrow(/required subject/);
    expect(provider.calls).toEqual([]);
  });

  it("retains repair, output checks, hooks, moderation, caching and dependency context", async () => {
    const source = spec();
    Object.assign(source.activities.definitions![0]!, {cross_run_cache: {enabled: true}, moderation: {provider: "openai"}});
    let calls = 0;
    const hook = vi.fn((_input, output, context) => { expect(context.deps).toEqual({service: 1}); return {...output, n: output.n + 1}; });
    const moderator = vi.fn(() => ({flagged: false, categories: []}));
    const observer = new CollectingObserver();
    const runtime = await buildLocalRuntime(source, {schemas, observer, deps: {service: 1}, tenant: {team: "local"},
      provider: {structuredCall() { return {n: ++calls === 1 ? 0 : 2}; }},
      outputChecks: {echo: (_input, output) => (output as {n: number}).n === 0 ? [{message: "positive required"}] : []},
      hooks: {echo: hook}, moderators: {echo: moderator}, cacheStore: new InMemoryCacheStore(),
    });
    expect(await runtime.executeWorkflow({n: 1})).toEqual({n: 3});
    expect(await runtime.executeWorkflow({n: 1})).toEqual({n: 3});
    expect(calls).toBe(2); expect(hook).toHaveBeenCalledTimes(2); expect(moderator).toHaveBeenCalled();
    expect(observer.activities[0]!.metadata["typeflux.runtime"]).toEqual({engine: "local"});
    const local = observer.activities[0]!.metadata["typeflux.local"] as {manifest: Record<string, unknown>};
    expect(local.manifest).not.toHaveProperty("task_queue"); expect(local.manifest).not.toHaveProperty("temporal_run_id");
    expect(observer.activities[0]!.metadata).not.toHaveProperty("temporal.workflow_id");
  });

  it("preserves policy admission, including skipped child requirements and required observation", async () => {
    const policyOf = (yaml: string) => composeProjectPolicies([{id: "org", spec: loadPolicySpec(`name: org\n${yaml}`)}], ["org"]);
    const root = spec();
    const provider = new Provider();
    await expect(buildLocalRuntime(root, {schemas, provider, policy: policyOf("semantics: {required: true}")})).rejects.toThrow(/moderation/);
    root.runtime.observability = {type: "langsmith"};
    await expect(buildLocalRuntime(root, {schemas, provider, policy: policyOf("observability: {required: true}")})).rejects.toThrow(/observer|observability/);
    root.runtime.observability = {type: "none"};
    Object.assign(root.activities.definitions![0]!, {moderation: {provider: "openai"}});
    root.workflow.steps = [{id: "child", workflow: "child", when: {path: "input.n", eq: -1}}];
    const child = spec({name: "Child"});
    child.activities.definitions![0]!.name = "child_echo";
    child.workflow.steps[0]!.activity = "child_echo";
    await expect(buildLocalRuntime(root, {schemas, provider, policy: policyOf("semantics: {required: true}"),
      subworkflows: {specFor: () => child}})).rejects.toThrow(/moderation/);
    expect(provider.calls).toEqual([]);
  });

  it("redacts predicate literals and emits no Temporal identity", async () => {
    const traces: EmittedTrace[] = [];
    const observer = new TraceWriter({submitTrace(trace) {traces.push(trace);}}, {});
    const source = spec();
    source.workflow.steps[0]!.when = {path: "input.n", neq: "private@example.com"};
    const runtime = await buildLocalRuntime(source, {schemas, provider: new Provider(), observer});
    await runtime.executeWorkflow({n: 1});
    await observer.flush();
    expect(traces).toHaveLength(1);
    expect(JSON.stringify(traces[0])).not.toContain("private@example.com");
    expect(JSON.stringify(traces[0])).toContain("[REDACTED_EMAIL]");
    expect(traces[0]).not.toHaveProperty("workflowId");
    expect(traces[0]).not.toHaveProperty("runId");
  });

  it("keeps parallel branch mutation and context isolated", async () => {
    const source = spec({activities: {definitions: []}, workflow: {name: "Root", input: "models:Item", output: "models:Branches", steps: [
      {id: "joined", parallel: {branches: [
        {id: "left", steps: [{id: "left_step", activity: "change"}]},
        {id: "right", steps: [{id: "right_step", activity: "read"}]},
      ], collect: {output: "models:Branches"}}},
    ]}});
    const change = defineCodeActivity({name: "change", input: Item, output: Item, handler: input => {input.n = 50; return input;}});
    const read = defineCodeActivity({name: "read", input: Item, output: Item, handler: async input => {await delay(5); return input;}});
    const runtime = await buildLocalRuntime(source, {schemas, provider: new Provider(), extraActivities: {change, read}});
    expect(await runtime.executeWorkflow({n: 1})).toEqual({left: {n: 50}, right: {n: 1}});
  });


  it("passes admitted artifact files to the existing provider pipeline", async () => {
    const directory = mkdtempSync(join(tmpdir(), "typeflux-local-artifact-"));
    try {
      writeFileSync(join(directory, "evidence.txt"), "evidence");
      const raw = structuredClone(fixtures.base);
      raw.runtime.artifacts = {local_roots: [directory]};
      raw.activities.definitions[0].artifacts = [{name: "docs", from: "input.files"}];
      raw.activities.definitions[0].input = raw.workflow.input = "models:Files";
      let seen: StructuredCallParams | undefined;
      const runtime = await buildLocalRuntime(typefluxYamlSpec.parse(raw), {schemas: {...schemas,
        "models:Files": z.object({n: z.number().int(), files: z.array(z.string())})}, provider: {
        structuredCall(params) {seen = params; return {n: 2};},
      }});
      expect(await runtime.executeWorkflow({n: 1, files: ["evidence.txt"]})).toEqual({n: 2});
      expect(seen?.artifacts?.[0]?.artifacts[0]).toMatchObject({source_kind: "local_path", size_bytes: 8});
    } finally {rmSync(directory, {recursive: true, force: true});}
  });

  it("releases session caches on success and external cancellation", async () => {
    for (const cancel of [false, true]) {
      const raw = structuredClone(fixtures.base);
      raw.workflow = {name: "Root", ...fixtures.cases[4].workflow};
      raw.activities.definitions[0].cache = {enabled: true};
      raw.runtime.registry.prompts.echo = {messages: [{role: "system", content: "stable"}, {role: "user", content: "{{n}}"}]};
      const controller = new AbortController();
      const events: string[] = [];
      const runtime = await buildLocalRuntime(typefluxYamlSpec.parse(raw), {schemas, provider: {
        supportsSessionCache: true, sessionCacheStyle: "reference",
        prepareCachedSession: params => cachedSessionHandleSchema.parse({provider: "fake", identity_hash: params.identityHash,
          supported: true, style: "reference", cache_id: "cache"}),
        async structuredCall() {events.push("start"); if (cancel) controller.abort(); await delay(5); events.push("done"); return {n: 2};},
        releaseCachedSession() {events.push("release"); throw new Error("cleanup is best effort");},
      }});
      const run = runtime.executeWorkflow({items: [{n: 1}]}, {signal: controller.signal});
      if (cancel) await expect(run).rejects.toBeInstanceOf(ActivityCancelledError);
      else expect(await run).toEqual({items: [{n: 2}]});
      expect(events).toEqual(["start", "done", "release"]);
    }
  });


  it("enforces targeted cache erasure declared by a skipped child", async () => {
    const source = spec();
    source.workflow.steps = [{id: "child", workflow: "child", when: {path: "input.n", eq: -1}}];
    const child = spec({name: "Child"}); child.runtime.cache_erasure = "targeted";
    const provider = new Provider();
    await expect(buildLocalRuntime(source, {schemas, provider, cacheStore: {get() {return undefined;}, set() {}},
      subworkflows: {specFor: () => child}})).rejects.toThrow(/cache_erasure.*eraseSubject/);
    expect(provider.calls).toEqual([]);
  });

});
