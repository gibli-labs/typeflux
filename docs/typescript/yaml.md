# YAML Runtime (TypeScript)

The TypeScript counterpart of the [YAML Runtime](../yaml.md). A `typeflux.yaml`
describes a project's provider, prompt registry, activities, and workflow
declaratively; [`@typeflux/temporal-yaml`](../../packages/typescript/temporal-yaml)
loads it, validates it, derives its workflow, and runs it on a Temporal worker.

The YAML **schema is language-neutral** — the same `typeflux.yaml` describes a
project for either SDK. What differs is the *wiring*: the TS runtime injects
schemas, providers, moderators, and prompt registries as structural transports
rather than importing them by module path. This page covers the TS surface and
the [permanent divergences](#permanent-divergences); the Python doc is the fuller
reference for field semantics.

## Loading and running

`loadYamlSpec(text, { env? })` strict-parses a `typeflux.yaml`, env-interpolates
it, and validates it into a typed `TypefluxYamlSpec`:

- **Strict parse** — duplicate mapping keys are rejected (no silent last-wins);
  document size (1 MiB) and alias expansion are bounded (parity with the Python
  strict loader).
- **Env interpolation** — `${VAR}`, `${VAR:-default}`, and `$${VAR}` (escape →
  literal `${VAR}`). A missing variable with no default throws. Prompt-text paths
  are skipped so a literal `${VAR}` in prompt content reaches the model verbatim.
- **Validation** — a **strict** Zod schema (parity with Python's `extra="forbid"`,
  #490): unknown keys are rejected, and any Python config block the TS runtime
  doesn't honor is rejected with a pointer error naming why. **A field the spec
  ACCEPTS is a field the runtime HONORS** — "spec model = wired".

`buildRuntime(spec, { provider, schemas, … })` is the one-call capstone (parity
with Python `build_runtime`). It builds the typed activity map + prompt registry,
derives the workflow plan, and wires a connected worker plus a `runWorkflow`
helper. `assembleYamlRuntime(...)` is the pure core (activity map + plan) when you
want to own the worker yourself.

```ts
import { loadYamlSpec, buildRuntime } from "@typeflux/temporal-yaml";
import { Client } from "@temporalio/client";
import { readFileSync } from "node:fs";

const spec = loadYamlSpec(readFileSync("typeflux.yaml", "utf-8"), { sourceLabel: "typeflux.yaml" });
const runtime = await buildRuntime(spec, {
  // No `provider` needed for the vendor types: `provider.type:
  // openai`/`anthropic`/`gemini` builds over the official SDK from the standard
  // env key. Inject `provider` (a ModelProvider) or `transports` for custom
  // providers/endpoints — an injected one always wins.
  schemas: { "schemas:ClaimInput": ClaimInput, "schemas:ClaimReviewPacket": ClaimReviewPacket /* … */ },
});
await runtime.worker.runUntil(
  runtime.runWorkflow(new Client(), claimInput, { workflowId: "review-123" }),
);
```

The workflow itself is a **plan-as-argument** design (TS's static-bundle
adaptation of Python's dynamic workflow class): the spec's `steps`/`map` model is
derived into a `WorkflowPlan`, and one generic `typefluxYamlWorkflow(plan, input)`
interpreter executes it — activity steps via `proxyActivities`, map steps via
bounded `fanOut`. Because the plan is an argument, in-flight executions are immune
to graph edits, which is what makes [frozen versioning](#workflow-versioning)
enforce at start time only.

## What the spec wires

The declarative runtime is functionally complete. Accepted and honored:

- **Activity definitions** — `moderation` (#480), timeouts (#479/#484), bounded
  `retry` (#486), **artifacts** end to end (#481: `runtime.artifacts` policy,
  per-activity declarations incl. `cache: reference`, resolution, attachment,
  provider mapping), **`provider_params`** (#495), the **session `cache:`
  block** (#478 — prep once per map fan-out, thread the handle to every item,
  best-effort release, fail-soft throughout), and the **cross-run
  `cross_run_cache:` block** — see [Two caches](#two-caches-session-vs-cross-run).
- **Workflow** — activity + `map` steps (bounded fan-out with `collect`), the
  full **lifecycle** surface (#482: progress/status query, cooperative cancel,
  the human-in-the-loop review gate with forward-only routes and timeouts),
  **`collect.max_bytes`** (#495 — defaults ON at 1.5 MB like Python: an
  actionable failure before Temporal's opaque ~2 MB payload limit; `0` disables),
  **`workflow.version`** — see [Workflow versioning](#workflow-versioning) — and
  the **`subjects:`** block (#715: `subjects: [{ from: input.X }]` extracts subject
  id(s) at start; they stamp the `TypefluxSubjectIds` keyword-list search attribute
  — inherited by sub-workflows — set the Langfuse native `userId` (primary) + a
  `typeflux.subject:{id}` tag per id, and ride on the cross-run cache record — a
  `cacheStore` implementing the optional **`SubjectErasableCacheStore`** capability
  (`eraseSubject`, #715 slice 3; the reference `InMemoryCacheStore` does) invalidates
  a subject's memoized outputs from a write-time subject→key index, with a documented
  full-flush fallback for plain stores; an
  explicit `runWorkflow(client, input, { workflowId, subjectIds })` override wins.
  Register `TypefluxSubjectIds` on the namespace as a `KeywordList` before use.
  **⚠️ Subject ids land in plaintext channels no codec/redaction touches — use
  opaque pseudonymous handles, never raw PHI/PII; see
  [Privacy (TypeScript)](privacy.md)**).
- **Runtime** — `provider.params` / `allow_prompt_model_override` (#495: params
  precedence is call defaults < prompt < activity, backend prompt models stripped
  unless allowed), **`provider_retry`** (in-activity exponential backoff with a
  distinct rate-limit class, Retry-After flooring, and class selection, #529),
  **`provider_limits`** (#529: per-provider/per-model `max_concurrent` +
  `min_interval_seconds`, one shared limiter per policy key; model > provider >
  default), `workflow_search_attribute`, `activity_retry`, observability (#451),
  and prompt registries — `inline` self-builds, and `registry.type:
  langfuse`/`langsmith` wire **out of the box** over the official SDKs from the
  standard env credentials (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`,
  `LANGSMITH_API_KEY`), no `registryTransport` needed; an injected
  `registry`/`registryTransport` always wins (that seam remains for custom
  backends). Unlike observability's degrade-to-untraced, missing registry
  credentials **throw** — a run cannot proceed without its prompts.
- **Providers** — `runtime.provider.type: openai`/`anthropic`/`gemini` wires
  **out of the box** over the official SDK (optional peers `openai`,
  `@anthropic-ai/sdk`, `@google/genai`, lazy-loaded per type — the TS mirror of
  Python's extras). The key resolves from `runtime.provider.api_key` (a literal
  or `value_from: {env|file}`, Python's secret rules) and otherwise from the
  vendor's standard variable — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY` (falling back to `GOOGLE_API_KEY`); `OPENAI_BASE_URL` /
  `ANTHROPIC_BASE_URL` are honored. An injected `provider`/`transports` always
  wins (custom providers and endpoints keep that seam), and a missing key
  **throws** like the registry's — a run cannot degrade past its provider.
- **Code-defined activities** — `assembleYamlRuntime`'s **`extraActivities`**
  merges injected activity descriptors into the map so YAML workflow steps can
  reference them by name (#496 — the TS replacement for Python's module loading;
  this is where hooks live). Both `defineActivity` (provider-backed) and
  `defineCodeActivity` (pure-code, non-LLM — #746, the analogue of Python's
  `activities.modules`) descriptors inject here, and a code step plans/composes/
  traces identically to an AI step.

## Two caches: session vs cross-run

An activity definition can declare **two independent caches** whose keys read alike
but do entirely different things. Getting them straight matters — the naming overlap
is the whole trap:

| Key | Name | What it caches | Scope | Needs |
| --- | --- | --- | --- | --- |
| `cache:` | session / prefix cache (#478) | provider-side CONTEXT (the stable prefix, `cache: reference` artifacts) | across the ITEMS of one `map` fan-out | nothing extra — prepped/released automatically |
| `cross_run_cache:` | cross-run output cache (#398/#753) | the activity's VALIDATED output | across separate RUNS | a `cacheStore` threaded into the runtime |

They live on different axes and are fully independent — a definition may declare
both, one, or neither.

The **cross-run** cache is what lets an identical input skip the provider call
entirely. It only ENGAGES when you thread a `cacheStore` into the runtime:

```yaml
activities:
  definitions:
    - name: summarize
      input: schemas:Doc
      output: schemas:Summary
      prompt: p/summarize
      cross_run_cache: { enabled: true }   # memoize validated output across RUNS
```

```ts
import { InMemoryCacheStore } from "@typeflux/temporal";

const runtime = await buildRuntime(spec, {
  schemas,
  cacheStore: new InMemoryCacheStore(), // (or a durable store) — required for cross-run caching to engage
});
```

With a store wired, a second run with the same input serves the cached output and
makes **zero** provider calls. Fields mirror the Python `CacheConfig` vocabulary:
`enabled` (default `true`; set `false` to opt out while keeping the block) and
`bypass_reads_env` (an env var whose presence forces regeneration — skips reads,
still writes). A cache HIT re-runs the activity's `outputCheck` (#745), so tightening
a check invalidates stale entries. Declaring the block with **no** store wired is an
inert no-op, never an error. Cross-run caching applies to provider-backed activities
only — pure-code activities (#746) reject it. A worker `bindings` module supplies the
store as `cacheStore` alongside `schemas`/`extraActivities`.

> Python parity note: Python's YAML surfaces only the session `cache:` key; its
> cross-run `CacheConfig` is code-only (`AIActivity.cache`). This TS `cross_run_cache:`
> key therefore has no Python-YAML counterpart yet — mirroring it in Python YAML is a
> follow-up.

## Workflow versioning

`workflow.version` is a frozen pointer to ONE graph. Every start stamps the plan
digest into the execution memo, and the control plane refuses to start an EDITED
graph under a frozen label (#530, #662). Enforcement is at **start time** —
because the TS runtime passes the plan as an argument, in-flight executions
already ran under the plan they were started with. See
[frozen-version enforcement](#permanent-divergences) below for the TS/Python ABI
note.

## Sub-workflows

A step may invoke another project workflow as a Temporal child workflow, either
once (`workflow: <manifest-id>`) or fanned over items (`map.workflow`). The
`workflow:` value is a project-manifest workflow id — pass a `subworkflows`
resolver (`projectSubworkflowResolver`) to `buildRuntime` / the control plane so
the derived plan embeds each child's resolved plan and identity; a standalone
spec that references a sub-workflow is rejected. In the TS edition the parent
plan carries the child's resolve-time identity (`{ childDigest, plan,
workflowName, project, versionLabel? }`) because `workflowIdentityMemo` and the
digest cannot run inside the workflow sandbox; the interpreter assembles the
child memo and calls `executeChild` from those fields alone.

Child ids are `{parent_workflow_id}.{step_id}` (`-{index}` under `map.workflow`),
started with `WorkflowIdReusePolicy.ALLOW_DUPLICATE` +
`ParentClosePolicy.TERMINATE`, inheriting the parent task queue, and stamped with
`typeflux_parent_workflow_id` plus their own identity memo and (when
`runtime.temporal.workflow_search_attribute` is configured) their own logical
name in that attribute. The worker built from the parent registers the child's
activities too (one generic workflow type serves parent and child); an activity
name declared by multiple project workflows is allowed only when the definitions
are identical — divergent definitions under one name reject at assembly, naming
both declaring workflows. A child-graph edit folds into the parent digest — the
frozen-label cascade documented in
[docs/yaml.md](../yaml.md#the-frozen-label-cascade-an-accepted-cost). Behavior is
at parity with Python; plans/digests stay edition-specific.

The bundle's `steps` array deliberately omits sub-workflow steps (they call no
activity of the parent's — the `topology` projection is the authoritative
full-step view, with a `workflow` node per sub-workflow step), and the
correlation card lists a run's direct children from the parent-link memo.

## Parallel, conditional, and multi-gate composition

The `parallel:` blocks, `when:` gating, and multiple `lifecycle.gates` primitives are
**schema-neutral** — the same YAML keys and semantics as the Python edition, which is
the fuller reference:

- [Parallel Steps](../yaml.md#parallel-steps) — heterogeneous branches merged into a
  typed `collect` (fields ARE branch ids), nesting capped at 3. The interpreter runs
  branches under one `CancellationScope` when lifecycle cancellation is enabled
  (`fanOutCancellable`); `collect.max_bytes` guards the merged payload.
- [Conditional Steps (`when`)](../yaml.md#conditional-steps-when) — a leaf predicate or
  one `all:`/`any:` level of pure literal data; a top-level false gate early-exits the
  sequence, a branch gate skips only that branch (`null` in its collect field).
- [Multiple review gates (`lifecycle.gates`)](../yaml.md#multiple-review-gates-lifecyclegates)
  — `review` XOR `gates`; drive a specific gate with
  `handle.signal("typeflux_submit_review", { user_decision, gate })`, and read the
  additive `waiting_gates` from the status query.

Plans and digests stay edition-specific; behavior (which steps run/skip, event
subsequences, wire shapes) is at parity. The Python
[`claims_review_composition`](../../packages/python/examples/claims_review_composition/)
example demonstrates the full surface end to end.

## Project manifests, profiles, and policy

A `typeflux.project.yaml` binds workflows to environments, component profiles,
and policies. The TS runtime composes all three:

- **Environment overrides + component profiles** compose into the resolved spec
  (`resolveEnvironmentWorkflow`, profile composition via `composeProfileOverrides`
  / `resolveSelectedProfiles`) — layering workflow YAML < profiles < environment
  overrides.
- **Policies** compose most-restrictively (`composeProjectPolicies`), with a
  fail-closed admission check (`validateProjectBundle` for the read side,
  admission at start for the operate tier) and a per-call `RuntimePolicyGuard`
  wired into the worker by `assembleYamlRuntime`. See the
  [policy-governed-review](#examples) example and the
  [control-plane README](../../packages/typescript/temporal-controlplane/README.md)
  for the operate-tier policy gate.
- **Transitive-closure admission** (#55 §9): admitting a workflow that references
  sub-workflows re-validates every referenced child against the parent's composed
  policy (`validateSubworkflowClosurePolicy`, surfaced as the
  `policy_subworkflow_closure` check when `validateWorkflowPolicyCompliance` is given a
  `resolveSubworkflowSpec`). A child that violates the policy fails the parent's
  admission — a composed program is governed as one unit. See
  [Composition governance and ceilings](../yaml.md#composition-governance-and-ceilings).
- **Composition ceilings** (#298): a `composition` block on a project policy bounds a
  workflow's graph shape as a pure tree-walk, surfaced as the
  `policy_composition_ceilings` check. Per-workflow knobs (`max_steps` /
  `max_parallel_width` / `max_parallel_nesting`) are evaluated against each closure
  member individually; tree-wide knobs (`max_total_steps`, the flattened sum over the
  whole closure, and `max_subworkflow_depth`) ride the closure walk, so decomposition
  cannot evade the budget. Composed policies take the **min** of each `max_*` and the
  **AND** of `allow_map_over_workflow` (tighten-only); every ceiling is `>= 1`. The full
  dimension + threat model is documented in
  [Composition governance and ceilings](../yaml.md#composition-governance-and-ceilings)
  and [Admission of agent-authored / externally submitted specs](../yaml.md#admission-of-agent-authored--externally-submitted-specs).
- **Risk tiers** (#300): a workflow declares `workflow.risk_tier` (`safe` < `policy_gated`
  < `human_gated` < `prohibited`) and a project policy's `risk_tiers` dimension defines the
  floor + what each tier requires; the effective tier `max(declared, floor)` expands
  fail-closed into the existing controls (review gate, moderation, redaction, provider+model
  allow-list) as the `policy_risk_tier` check and cascades up the sub-workflow closure
  (`evaluateWorkflowRiskTier` / `riskTierCascade`). The resolved bundle carries a typed
  `risk_tier` posture (`ApiBundleRiskTier`, byte-identical to Python). Full dimension +
  vocabulary in [Risk tiers](../yaml.md#risk-tiers). The `RiskTierContributor` evidence
  surface is Python-only until the TS metadata seam lands; enforcement is at full parity.
- **Compensation (sagas)** (#299): a step declares `compensate:` to undo its side effect and
  an activity declares `side_effecting: true`; on failure or cancellation the interpreter
  unwinds the compensations LIFO before re-raising, and a `risk_tiers` tier can demand the
  discipline with `require_compensation` (every `side_effecting` activity STEP must declare
  `compensate:`) — an OR-merged macro that rides the closure cascade at full parity with
  Python. `side_effecting` is governance metadata, never a digest input. Full patterns
  (idempotency, review-before-side-effect, terminate-and-resubmit) in [Compensation and
  rollback](../yaml.md#compensation-and-rollback-sagas). The `CompensationContributor`
  manifest evidence is Python-only until the TS metadata seam lands; TS records compensation
  identity through the plan node + status wire, and enforcement is at full parity.
- **Spec admission** (#298): `admitSpec` (`@typeflux/temporal-yaml`) is the seam for
  admitting an agent-authored or externally submitted spec under a project's governance —
  bounded parse + composed policy + ceilings + closure, returning a typed
  `AdmissionReport` carrying the exact evaluated `spec` (build the runtime FROM it so
  what runs is what was admitted). `origin: "external"` is the hostile-input posture
  (fail-closed with no governing policy); module-import surfaces — including schema-ref
  imports, which Python gates via `admission_schema_ref_roots` — are structurally absent
  in the TS injection model: `class:`/`modules:` never parse (#496) and schema refs
  resolve against injected schemas. There is no CLI convention in the TS packages, so the
  exported function is the seam.

## Examples

Runnable ports of the Python example suite live under
[`packages/typescript/temporal-yaml/examples`](../../packages/typescript/temporal-yaml/examples).
Each uses a **scripted provider** (fake model, no API key) but runs a real worker,
so the commands below need a local Temporal dev server (`temporal server
start-dev`) plus `pnpm -r build`. CI also exercises each example without a server
via a separate structural test.

| Example | Command | Covers |
|---|---|---|
| [`claims-review-composition`](../../packages/typescript/temporal-yaml/examples/claims-review-composition) | `pnpm --filter @typeflux/temporal-yaml example:composition` | The full #55 composition surface — `parallel:` if/else, `map.workflow` + `workflow:` sub-workflows, two `lifecycle.gates` — with both authoring modes (pure-YAML + `extraActivities`) |
| [`insurance-claim-review`](../../packages/typescript/temporal-yaml/examples/insurance-claim-review) | `pnpm --filter @typeflux/temporal-yaml example:insurance` | Map fan-out + `collect`, per-model `provider_limits` (#529), Zod schema resolution, env-interpolated config |
| [`lifecycle-review`](../../packages/typescript/temporal-yaml/examples/lifecycle-review) | `pnpm --filter @typeflux/temporal-yaml example:lifecycle` | The human-in-the-loop review gate, forward-only `user_decisions` routing, the client-side query→signal→result sequence, injected hooks |
| [`policy-governed-review`](../../packages/typescript/temporal-yaml/examples/policy-governed-review) | `pnpm --filter @typeflux/temporal-yaml example:policy` | Org+tenant policy composition, fail-closed admission on `buildRuntime`, runtime `RuntimePolicyGuard` — with pass / admission-reject / runtime-block YAML variants |
| [`session-cache-review`](../../packages/typescript/temporal-yaml/examples/session-cache-review) | `pnpm --filter @typeflux/temporal-yaml example:session-cache` | Provider session/prefix caching (#478) across a map fan-out, reference-style vs prefix-style, fail-soft degradation |

Each example's `README.md` includes an explicit Python↔TS mapping.

## Running the live workflow test

`test/live-yaml-workflow.test.ts` runs a YAML-defined map+activity workflow end to
end against a local dev server (skipped in CI):

```sh
temporal server start-dev
pnpm -r build   # so the workflow bundle resolves @typeflux/temporal/composition from dist
TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test
```

## Permanent divergences

These Python fields are **rejected** by the TS strict validator, each with a
message saying why — they are deliberate, not deferrals:

- **`activities.modules` + `runtime.imports`** — Python's importlib activity/module
  loading. TS injects descriptors instead (`extraActivities`): `defineActivity` for
  provider-backed activities, `defineCodeActivity` for pure-code ones (#746).
- **`provider.base_url` / `vertex` and `class` fields** — clients, registries, and
  moderators are injected in TS, so endpoint and implementation wiring belong to
  your adapter, not the spec.
- **`moderation.model`** — moderators are injected functions.

Beyond the spec, one runtime-level ABI divergence is **deliberate and permanent**:
the Python and TS Temporal bindings differ (Python registers a dynamic workflow
class with identity in the type; TS passes the plan as an argument and carries
identity in the execution memo). This is why a workflow started by one SDK is not
operated by the other edition's worker — the binding profiles
(`typeflux-binding` contract) are distinct by design.

No tracked deferrals remain — every accepted spec field is honored. Behavioral
parity, not byte-identical (Zod-validated spec vs Pydantic).

## See also

- [Concepts](concepts.md) — the ownership model and AI-activity contract
- [Observability](observability.md) — the trace/manifest surface for YAML runs
- [Provider Portability](provider-portability.md) — provider divergence rules
- [YAML Runtime (Python)](../yaml.md) — the fuller field-semantics reference
- [`@typeflux/temporal-yaml` README](../../packages/typescript/temporal-yaml/README.md)
