# @typeflux/temporal-yaml

```bash
npm install @typeflux/temporal-yaml   # Node 22 (pulls the worker + core)
```

Opt-in **declarative YAML runtime** for the Typeflux TypeScript SDK — the parity of
Python's `typeflux.yaml`. A `typeflux.yaml` describes a project's provider,
prompt registry, activities, and workflow declaratively; this package loads,
validates, and runs it. It is a companion package so the core
[`@typeflux/temporal`](../temporal) stays dependency-light.

## Status (#452)

- **`loadYamlSpec(text, { env? })`** — strict-parses a `typeflux.yaml`, env-interpolates
  it, and validates it into a typed `TypefluxYamlSpec`:
  - **Strict parse** — duplicate mapping keys are rejected (no silent last-wins), and
    document size (1 MiB) + alias expansion are bounded (parity with the Python strict
    loader).
  - **Env interpolation** — `${VAR}`, `${VAR:-default}`, and `$${VAR}` (escape →
    literal `${VAR}`). A missing variable with no default throws. **Prompt-text paths are
    skipped** so a literal `${VAR}` in prompt content reaches the model verbatim.
  - **Validation** — a **strict** Zod schema (parity with Python's `extra="forbid"`, #490):
    unknown keys are rejected (a typo'd field fails loudly instead of silently getting a
    default), and any Python config block the TS SDK doesn't honor is rejected with a
    pointer error naming why (a tracking issue, or a documented permanent divergence).
    A field the spec ACCEPTS is a field the runtime HONORS — "spec model = wired".

- **`defineActivitiesFromSpec(spec, { schemas, hooks? })` / `inlineRegistryFromSpec(spec)`**
  — build the SDK's `defineActivity` descriptors + an `InlinePromptRegistry` from the spec.
  Schemas/functions are resolved via an **injected resolver** (vs Python's import-path
  resolution) so it stays unit-testable.
- **`workflowPlanFromSpec(spec)` + `typefluxYamlWorkflow`** — the declarative workflow. The
  spec's `steps`/`map` model is derived into a `WorkflowPlan`, and the generic
  `typefluxYamlWorkflow(plan, input)` interpreter (a code-defined workflow) executes it:
  activity steps via `proxyActivities`, map steps via bounded `fanOut`, threading each
  result through a context (parity with Python `create_workflow`). The plan is a workflow
  **argument** (TS's static-bundle adaptation of Python's dynamic workflow class).

- **`buildRuntime(spec, { provider, schemas, … })` / `assembleYamlRuntime(...)`** — the
  one-call capstone (parity with Python `build_runtime`). `assembleYamlRuntime` is the pure
  core (activity map + workflow plan); `buildRuntime` adds a connected
  `createTypefluxWorker` and a `runWorkflow(client, input, { workflowId })` helper that
  starts `typefluxYamlWorkflow` with the derived plan. A `runtime.provider.type` of
  `openai`/`anthropic`/`gemini` builds **out of the box** over the official SDK (optional
  peers, lazy-loaded per type) from `runtime.provider.api_key` or the vendor's standard env
  key; an injected `provider`/`transports` always wins, and a missing key throws.

```ts
import { loadYamlSpec, buildRuntime } from "@typeflux/temporal-yaml";
import { Client } from "@temporalio/client";
import { readFileSync } from "node:fs";

const spec = loadYamlSpec(readFileSync("typeflux.yaml", "utf-8"), { sourceLabel: "typeflux.yaml" });
const runtime = await buildRuntime(spec, {
  // No `provider` needed for the vendor types (openai/anthropic/gemini — built
  // from env); inject `provider`/`transports` for custom providers/endpoints.
  schemas: { "schemas:ClaimInput": ClaimInput, "schemas:ClaimReviewPacket": ClaimReviewPacket /* … */ },
});
// Serve the worker and start a workflow:
await runtime.worker.runUntil(
  runtime.runWorkflow(new Client(), claimInput, { workflowId: "review-123" }),
);
```

## Examples

Runnable YAML-runtime examples live in [`examples/`](./examples) — offline
(scripted provider, no API key) ports of the Python example suite, each
exercised in CI. [`insurance-claim-review`](./examples/insurance-claim-review)
covers map fan-out + `provider_limits`; [`lifecycle-review`](./examples/lifecycle-review)
covers the human-in-the-loop review gate + injected hooks.

## Running the live workflow test

`test/live-yaml-workflow.test.ts` runs a YAML-defined map+activity workflow end to end
against a local dev server (skipped in CI):

```sh
temporal server start-dev
pnpm -r build   # so the workflow bundle resolves @typeflux/temporal/composition from dist
TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test
```

## What the spec wires

The declarative runtime is functionally complete — load + validate a `typeflux.yaml`,
build its typed activities + prompt registry, derive its workflow, and run it on a
Temporal worker via `buildRuntime`:

- **Activity definitions** — `moderation` (#480), timeouts (#479/#484), bounded `retry`
  (#486), **artifacts** end to end (#481: `runtime.artifacts` policy, per-activity
  declarations incl. `cache: reference`, resolution, attachment, provider mapping),
  **`provider_params`** (#495), and the **session `cache:` block** (#478 — prep once per
  map fan-out, thread the handle to every item, best-effort release; fail-soft
  throughout).

  > **Two caches, two keys — do not confuse them.** The `cache:` key is the **session
  > (provider prefix) cache** (#478): it caches provider-side CONTEXT once and reuses it
  > across the ITEMS of one map fan-out. The `cross_run_cache:` key is the **cross-run
  > output cache** (#398/#753): it memoizes an activity's **validated output** across
  > separate RUNS in a `CacheStore`, so an identical input skips the provider call
  > entirely. They live on different axes and are fully independent — a definition may
  > declare both, one, or neither.

- **Cross-run output cache** (#398/#753) — the `cross_run_cache:` block on an activity
  definition (fields `enabled` (default `true`) and `bypass_reads_env`, mirroring the
  Python `CacheConfig` vocabulary) makes cross-run memoization reachable from YAML. It
  only ENGAGES when a `cacheStore` is threaded into the runtime — pass one to
  `assembleYamlRuntime`/`buildRuntime` (`cacheStore: new InMemoryCacheStore()`), or from a
  worker `bindings` module. With a store wired, a second run with the same input serves the
  cached output and makes **zero** provider calls; `bypass_reads_env` names an env var
  whose presence forces regeneration (skips reads, still writes). A cache HIT re-runs the
  activity's `outputCheck` (#745), so tightening a check invalidates stale entries. Declaring
  the block with no store wired is an inert no-op. Cross-run caching applies to
  provider-backed activities only — pure-code activities (#746) reject it.

  ```yaml
  activities:
    definitions:
      - name: summarize
        input: schemas:Doc
        output: schemas:Summary
        prompt: p/summarize
        cross_run_cache: { enabled: true }        # memoize validated output across RUNS
        # cache: { enabled: true }                # (independent) session prefix cache for map items
  ```
- **Workflow** — activity + `map` steps (bounded fan-out with `collect`), the full
  **lifecycle** surface (#482: progress/status query, cooperative cancel, the
  human-in-the-loop review gate with forward-only routes and timeouts),
  **`collect.max_bytes`** (#495 — defaults ON at 1.5MB like Python: an actionable
  failure before Temporal's opaque ~2MB payload limit; `0` disables), and
  **`workflow.version`** (#530 — a frozen pointer to ONE graph: every start stamps
  the plan digest into the execution memo, and starting an EDITED graph under a
  frozen label is refused; enforcement is at start time — TS's plan-as-argument
  already makes in-flight executions immune to graph edits).
- **Runtime** — `provider.params` / `allow_prompt_model_override` (#495: the params
  precedence is call defaults < prompt < activity, and backend prompt models are
  stripped unless allowed), **`provider_retry`** (in-activity exponential backoff
  with a distinct rate-limit class, Retry-After flooring, and the
  `retry_rate_limits`/`retry_transient_errors` class selection, #529),
  **`provider_limits`** (#529: per-provider/per-model `max_concurrent` +
  `min_interval_seconds`, one shared limiter per policy key across all
  activities; model > provider > default),
  `workflow_search_attribute` (a keyword attribute carrying the workflow's logical name
  on every start — register it in the namespace first), `activity_retry`, observability
  (#451), and inline/transport prompt registries.
- **Code-defined activities** — `assembleYamlRuntime`'s **`extraActivities`** merges
  injected activity descriptors into the map so workflow steps can reference them by
  name (#496 — the TS replacement for Python's module loading). Both authoring modes
  inject here: provider-backed `defineActivity` descriptors AND **pure-code**
  `defineCodeActivity({ name, input, output, handler })` descriptors (#746 — the TS
  analogue of Python's `activities.modules`, a deterministic no-LLM step). A workflow
  step referencing a code activity plans, composes (parallel/collect), and traces
  exactly like an AI step; the passthrough-into-collect pattern lets a zero-cost code
  branch echo sibling context into the collect so a terminal code step sees both. A
  colliding name (spec definition vs injected descriptor) throws at assembly.
- **Sub-workflow composition** — `workflow:` / `map.workflow` steps invoke sibling
  workflows resolved through a `projectSubworkflowResolver`. A composed worker serves ONE
  runtime prompt registry, and #748 makes that registry the **merge** of the parent's and
  every (transitively) referenced child's `runtime.registry`: a child's prompts no longer
  have to be duplicated into the parent spec. A prompt name present in one spec is merged
  in; a name present in several is merged once only when the entries are **byte-identical**
  — a genuine conflict is a loud load-time error naming the prompt, both source workflows,
  and the first differing field. Non-inline registries (langfuse/langsmith) hold nothing to
  merge, but the closure must agree on ONE backend: a child declaring a different registry
  type or config (label/host) than the parent is rejected at load. Child activity
  definitions merge under the same identical-or-reject rule (#55).

**Permanent divergences** (rejected with messages saying why): `provider.base_url` /
`vertex` and the `class` fields (clients/registries are injected in TS — endpoint and
implementation wiring belong to your adapter), `moderation.model` (moderators are
injected functions), `activities.modules` + `runtime.imports` (importlib machinery —
inject descriptors instead). No tracked deferrals remain — every accepted spec
field is honored by the runtime.

Behavioral parity, not byte-identical (Zod-validated spec vs Pydantic).
