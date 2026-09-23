# Typeflux authoring checklist — writing a correct AI activity

A distilled, actionable how-to for an agent authoring or editing a Typeflux workflow YAML
(`typeflux.yaml`). It complements `typeflux://schema/typeflux-yaml` (the machine-checkable schema)
and `typeflux://docs/yaml` (the full reference). Author YAML as a reviewable diff in the repo, then
validate it with the `validate_project` tool / `typeflux://{project}/validate` resource **before**
proposing it — do not start workflows to check them.

## The mental model

- A **workflow** is a deterministic, replayable Temporal graph of **steps**. No model calls or side
  effects happen in the workflow body itself — every call is an **activity**.
- An **activity** is one typed unit of work: `input` schema in, a `prompt`, `output` schema out.
  The runtime validates the model's output against `output` and retries on a validation miss
  (`validation_retries`).
- Schemas are referenced by name (e.g. `schemas:ClaimItem`). The activity boundary is strict: the
  input/output types must line up with what the step feeds it.

## Non-negotiables (the schema will reject these loudly)

1. **Strict keys.** Every block forbids unknown keys. A typo like `maximum_atempts` fails at load —
   it does not silently take the default. Check field names against the schema.
2. **Bounded retries.** Activity retries are always bounded (`retry.maximum_attempts`, default 5).
   `maximum_attempts: 0` is Temporal's explicit *unlimited* sentinel — only use it deliberately.
3. **Explicit provider params.** A param the selected provider does not support (e.g. `top_k` on
   `openai`) is rejected, not ignored. Keep `provider_params` to the provider's supported set.
4. **Secret references, never literals.** Put credentials behind `value_from: { env | file }`
   (exactly one source), never inline secret strings in the YAML.
5. **One representation per gate.** A lifecycle review is either the single `review:` gate OR a
   `gates:` list — never both. Review routes are forward-only.

## Writing an activity — the checklist

- [ ] `name`, `input`, `output`, and `prompt` are all set. `prompt` is an inline prompt or a
      registry `{ name, version | label }` (version and label are mutually exclusive).
- [ ] The `input`/`output` schema refs exist and match how the step wires the activity.
- [ ] `validation_retries` is set intentionally (default 1) — how many times to re-ask the model
      when its output fails the `output` schema.
- [ ] Timeouts (`start_to_close_timeout_seconds`, `heartbeat_timeout_seconds`) are set for
      long-running calls.
- [ ] If the activity performs an **external side effect** (a downstream write, a notification, a
      charge), declare `side_effecting: true`. This is a governance assertion — a
      `risk_tiers.require_compensation` policy will then require the *step* to declare
      `compensate:` (a saga undo activity).
- [ ] `moderation:` is set when output must be screened (`on_violation: block | flag`).
- [ ] For session-cached activities (`cache:`), do not attach a non-`reference` artifact with
      `attach.role: system` — it is dropped on a reference-style cache hit.

## Writing the workflow

- [ ] `steps` is non-empty; each step has a unique `id` and exactly one of `activity`, `map`,
      `parallel`, or `workflow` (a child workflow by manifest id).
- [ ] `when:` gates use the literal predicate DSL (`{path, eq/neq/lt/lte/gt/gte/in/exists}`), at
      most one `all:`/`any:` level — no nested boolean trees, no named predicates.
- [ ] `map` fans over `over` with bounded `concurrency` and a `collect` with a `max_bytes` guard
      (default 1.5MB; `0` disables). Exactly one of `map.activity` / `map.workflow`.
- [ ] `risk_tier` (`safe` | `policy_gated` | `human_gated` | `prohibited`) reflects the workflow's
      real blast radius. Unset reads as `safe`. The project policy's `risk_tiers` dimension defines
      what each tier requires (e.g. a human gate, compensation).
- [ ] `lifecycle.review` (or `gates`) is present when a human must approve before side effects
      run. `after_step` names the step it waits behind; `user_decisions` map decisions to routes.
- [ ] `version:` is a frozen pointer to one graph — set/bump it deliberately.

## Before you propose the change

1. Resolve the bundle: `get_bundle` (or `typeflux://{project}/workflows/{id}/bundle`) shows the
   fully-resolved graph, effective runtime, policy, secret references, and risk tier.
2. Validate the project: `validate_project` returns the structured issue list per environment.
3. Inspect the activity catalog: `get_catalog` confirms every referenced activity resolves with
   the expected IO schemas.
4. If a read returns `UnsupportedRuntime` (501), the serving control plane cannot resolve this
   project's runtime — pure-YAML reads still work; attach to a control plane that can resolve it
   (set `TYPEFLUX_CP_URL`) for full resolution.
