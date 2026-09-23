# Changelog

All notable Typeflux Temporal changes should be recorded here before a release.

This project uses SemVer-style versioning for public package releases. Keep
unreleased changes under `Unreleased`, then move them under a dated version
heading as part of the release PR or tag preparation.

## Unreleased

(no unreleased changes)

## client-1.15.1 - 2026-09-19

- `@typeflux/control-plane-client` **migrated to public npmjs.org** under the
  `@typeflux` scope (#893) — installation needs no credentials. Patch-compatible
  with 1.15.0: identical generated API from the same contract revision; only
  the package name, registry, and metadata changed. Historical
  `@gibli-labs/control-plane-client` versions remain on GitHub Packages as a
  frozen lineage. Consumers change the dependency name and import specifier.

## 0.1.0 - 2026-09-19

- Initial public release of the SDK surface: the Python SDK
  (`typeflux` on PyPI) and the four-package TypeScript train
  (`@typeflux/temporal`, `@typeflux/temporal-yaml`,
  `@typeflux/temporal-worker`, `@typeflux/temporal-controlplane` on npm).
  Typed AI activities over Temporal, the declarative YAML runtime,
  control-plane servers in both editions, governance/policy enforcement,
  execution manifests, redaction, and provider portability
  (OpenAI/Anthropic/Gemini + a credential-free fake provider). 0.x beta:
  breaking changes may land in any minor and are always called out here.

### Added

- Risk tiers (#300): a workflow declares `workflow.risk_tier` (`safe` <
  `policy_gated` < `human_gated` < `prohibited`) and a project policy's
  `risk_tiers` dimension defines what each tier requires and a `min_tier`
  floor. The effective tier is `max(declared, floor)` and cascades up the
  sub-workflow closure (a parent embedding a higher-tier child inherits at
  least that tier); it fail-closed expands into the existing controls (review
  gate, moderation, redaction, provider+model allow-list) at admission — no
  parallel enforcement engine. Slice 2 surfaces the posture on the resolved
  bundle as `risk_tier` (`BundleRiskTier`: declared/effective/floor/
  floor_source, per-requirement satisfaction, and the closure `cascade` when a
  child lifts it) — additive contract change, both editions byte-identical,
  omitted when no policy constrains tiers. The console derives a risk-tier
  insight (tier + unsatisfied requirements, no prompt/secret content) and shows
  the tier in the workflow Policy panel. Control-plane client bumps to 1.8.0.

- Project refresh provenance (#296): `GET /api/v1/projects` now reports each
  Git-sourced project's current clone `repo_sha` and its latest
  `last_refresh` result (recorded per process), and `ProjectRefreshResult`
  carries `refreshed_at`. A failed refresh returns a structured
  `refreshed: false` result (no 500) and leaves the previous good clone
  resolvable — reads are not poisoned. The console Projects panel shows the
  current sha and an ok/failed last-refresh badge, and refreshing updates the
  row in place via a re-fetch instead of a full page reload.

- Multi-project — GitHub-sourced clones + refresh (#256, PR 3 of 3,
  completing the issue): a registry entry may set `repo: {url, ref,
  manifest}` instead of a local `manifest`; the server clones it into a
  managed cache (`.typeflux-clones`, or `serve --clone-cache <dir>`) on
  first use and re-reads it per request. `POST /api/v1/projects/{id}/refresh`
  re-fetches the clone (a no-op for local checkouts) and invalidates that
  project's pinned operations so the next call re-resolves the new sha; the
  console's Projects panel shows each project's source (repo `url @ ref`)
  and a **refresh** action for Git-sourced ones. Manifest paths that escape
  the clone are rejected; the registry file is operator-authored, so cloning
  its URLs is in scope.

- Multi-project console — project switcher + cross-project overview (#256,
  PR 2 of 3): the sidebar gains a project switcher (shown when the server
  serves more than one project), and the Overview lists every registered
  project with its manifest and an active/switch affordance. A fetch
  middleware rewrites every request to the active project's scoped routes
  (`/api/v1/projects/{id}/...`) — the typed call sites are unchanged, and
  the unprefixed routes still serve the default project. The selection is
  persisted; switching reloads so every view rescopes cleanly.

- Multi-project control plane — registry + project-scoped routes (#256, PR
  1 of 3): the server can serve several Typeflux projects. A
  `typeflux.projects.yaml` registry lists `{id, manifest}` entries (phase-1
  local checkout paths); `create_app_from_registry` / `serve --registry`
  build the multi-project app, and the existing single-manifest `create_app`
  is the degenerate **registry of one** (default alias). Every handler is
  defined once and mounted twice — unprefixed for the default project and
  under `/api/v1/projects/{project}/...` for the full set — so the
  single-project dev loop is byte-for-byte unchanged. `GET /api/v1/projects`
  lists the registered projects for the console switcher (PR 2). The
  operations cache gains a project dimension. **Isolation**: applying an
  environment profile mutates `os.environ` process-wide, so every env-context
  block is serialized under a process-wide resolution lock with a
  per-coroutine (ContextVar) reentrancy guard. Async reads that hold the lock
  across a Temporal `await` (drain, executions, workers, the operations build)
  take it via `async_project_environment_context`, which acquires in a worker
  thread so the event loop is never blocked — concurrent async requests
  serialize instead of deadlocking.

- Deployment plans + GitHub-PR approval gate (#253): an in-repo,
  immutable, content-hashed plan file
  (`deployments/<workflow>.<env>.<plan_hash12>.yaml`) pins the identity a
  promotion emits artifacts for — versioned workflow type + spec digest,
  code sha (#252), target environment, policy hash, digest-pinned image,
  and preflight — composed from one `resolve_workflow_bundle` call so the
  plan is identity-locked to the resolution that produced it. Approval is
  the GitHub PR review that merges the file to main; promotion
  (`project deploy --plan <file>`) is **plan-authoritative** (environment,
  workflow, image, and policies come from the file, not the CLI flags) and
  **fails closed** on drift with the concrete mismatch. `deploy --plan-out
  <dir>` writes the plan; `--environment`/`--image` are required only when
  not promoting a plan. `GET /deployments` lists plans with live
  verification and a server-built promote command. The console gains a
  read-only **Deployments** page: per-workflow plan cards with
  ready-to-promote / drifted / preflight / digest-pin badges, policy and
  code-commit provenance, per-field drift mismatches, and the copyable
  promote command. Plan files carry identities and hashes only — no secret
  values, prompt text, or rendered config.

- Precise Langfuse trace deep-links (#270 follow-up): the bundle exposes the
  exact trace title the writer records runs under
  (`workflow.observability_trace_name` = `TypefluxWorkflow:<name>`, from one
  shared `workflow_trace_name` helper), and the console's "this workflow's
  traces" link searches by that exact title instead of the bare workflow
  name — so a console-started run's traces are found precisely. The trace
  name is also shown in the workflow Identity panel.

- Task-queue worker presence (#278): `GET /workflows/{id}/workers` reports
  the resolved task queue and the live poller count (via Temporal's
  describe_task_queue), degrading to `reachable: false` when Temporal is
  unreachable. The console's Start panel shows the queue with a
  "Check workers" affordance and the receipt warns prominently when a run
  was submitted to a queue with **no workers polling** — so a silently
  pending run is no longer a mystery.

- Regulated showcase example (#268): `regulated_disclosure_review` — an
  OpenAI-backed, review-gated workflow with Langfuse observability +
  redaction, a fail-closed review gate, and artifact constraints — exercises
  the `regulated` project policy end to end. A `temporal_cloud_regulated`
  validation target admits it under `regulated` in `temporal_cloud_dev`, so
  the policy is finally referenced (the explorer's `used_by` shows it); it
  resolves under `base` in `local` like the other examples.

- Schema-driven typed input form for starting workflows (#269): the bundle's
  `workflow.input_schema` now carries the full JSON Schema, and the console's
  Start panel renders typed fields from it (text/number/checkbox/enum select;
  nested/array shapes fall back to a per-field JSON editor) with required
  markers and descriptions. Input is **validated against the schema with ajv
  before the start call** — wrong types and missing required fields are caught
  client-side; the server-side model validation remains the backstop. An
  "edit as raw JSON" toggle preserves the freeform escape hatch.

- Configurable project runtime defaults (#265): `defaults.runtime` in the
  project manifest is a precedence layer applied beneath every workflow
  YAML — engine defaults < project defaults < workflow YAML < profiles <
  environment overrides — through the same deep merge and override
  allowlist as every other layer (out-of-allowlist keys fail loudly).
  Spec-digest-safe. The bundle's `runtime_effective` materializes the
  curated knobs (provider retry/model, registry label, history limit)
  with a per-key source — `engine_default` / `project_default` /
  `configured` — so the console's Runtime section shows what unset knobs
  resolve to, not an omission. The examples manifest ships a
  `defaults.runtime` showcase.

### Changed

- Dependency maintenance: update the console to Vitest 4.1.11 and the MCP
  server to Vitest 5, plus current compatible `fast-uri`, `hono`, `qs`, and
  `baseline-browser-mapping` releases, and remediate the new `browserslist`
  advisories. The MCP package advances to 0.5.1 so the published server
  identity stays aligned with its package contents.

- Local environment traces to Langfuse by default: the common dev loop is
  local Temporal with traces in your own Langfuse project, so the examples'
  `local` environment reads `observability.type` from
  `${TYPEFLUX_LOCAL_OBSERVABILITY:-langfuse}` — traced by default, degrading
  cleanly when Langfuse keys are absent. Set `TYPEFLUX_LOCAL_OBSERVABILITY=none`
  for pure-offline.

- Environment-derived Temporal Web link (#276): `links.temporal_ui` now
  derives from the resolved environment's Temporal address when
  `TEMPORAL_UI_URL` is unset — `tmprl.cloud` addresses link to
  `cloud.temporal.io`, localhost links to `:8233` — so a cloud environment
  no longer shows a localhost "Open in Temporal" link. An explicit
  `TEMPORAL_UI_URL` still wins (self-hosted UIs); the execution path is
  identical for both.

- Workflow-scoped Langfuse trace links (#270): the Connections observer
  "traces" link now filters by the workflow name instead of dumping you
  into the whole project's trace list; per-run links keep their
  execution-id scope.

- Console topology rendering (#212 follow-up): review decisions now render
  as labeled **swim-lanes** below the sequential spine — one lane per routed
  decision, orthogonal checkpoint→target routing, shorter routes nearest the
  spine — replacing the stacked arcs. Decisions read as *entry points* into
  the downstream chain: a review checkpoint (tagged ⏸ REVIEW) has no
  unconditional outgoing spine arrow — its decision lanes are the flow, the
  warn-mode fall-through renders dotted only when no decision covers the
  next step, and the spine visibly continues from each entry point. Extends
  naturally to graph-shaped orchestration (#55).

### Added

- Deep-link fidelity (#264): the Langfuse project URL now derives from the
  YAML-dictated registry itself (host from `runtime.registry.host`, project
  id via the authenticated client; a mismatched `LANGFUSE_PROJECT_URL`
  loses with a warning) so prompt/trace links cannot point at the wrong
  host. Inline prompts no longer render registry links — they get a
  template preview from the user's own YAML (registry-managed prompt text
  still never serializes). Executions rows gain per-run Temporal/trace
  links and the Connections observer line gains browse traces.

- Prompt-registry drift (#254): `GET /workflows/{id}/prompt-status` compares
  each label-pinned prompt's current registry version against the version
  recorded in the latest execution manifest — drift reports "ran vX, label
  now vY"; pinned versions are trivially in-sync; any unknown side reports
  `unknown`, never a false in-sync; registry/observer failures degrade
  per-ref. Console: operator-triggered Prompt registry panel with status
  badges and Langfuse prompt links.

- Backend connections (#258): `GET /workflows/{id}/connections` reports the
  YAML-dictated registry and observer — type, host (never keys),
  reachability via a single request-scoped probe that degrades to
  `reachable: false`, plus execution-manifest and redaction state. The
  console's workflow page gains an operator-triggered Connections panel;
  `observability: none` reads as "runs are not traced".

- Definitions explorer (#257): `GET /environments/{id}`, `GET /policies[/{id}]`,
  and `GET /profiles[/{kind}/{id}]` render the declarative objects read-only —
  environment env-file refs and variable *names* (values never serialize),
  full policy rules with the composed hash, profile owned subtrees with
  content hashes — each with a `used_by` reverse index over declared
  references. The console sidebar gains Environments / Policies / Profiles
  sections with detail pages linking back into workflows.

- Git provenance (#252): `ResolvedWorkflowBundle.code` records the
  resolving checkout's sha, branch, dirty flag, normalized origin URL, and
  repo-relative source paths (absent cleanly outside git; never contents).
  The console's definition page gains a "defined in code · branch @ sha"
  banner with a commit link, a dirty-checkout badge, and a sha-pinned
  "View source" GitHub link.

- Runs list and manifest correlation (#251): `GET /workflows/{id}/executions`
  lists executions via the fail-safe type-prefix query (all statuses, with a
  per-run current-version flag), and `GET /workflows/{id}/correlation`
  retrieves the reproducibility record through the workflow's YAML-dictated
  observer — Langfuse yields the safe trace summary (trace id, manifest
  hash, git sha, prompt refs, models) with a direct trace deep-link in the
  console; `observability: none` is reported as exactly that; backend
  failures degrade, never error. Console: Executions table (click to
  inspect, old-version badges) and the Manifest correlation card.

- External deep-links (#250): `ResolvedWorkflowBundle.links` resolves
  `TEMPORAL_UI_URL` and `LANGFUSE_PROJECT_URL` per environment (explicit
  allowlist, http(s)-validated, never secrets); the console renders
  "Open in Temporal" on run receipts/status and "Langfuse" on prompt refs
  with a distinct link-out affordance — hidden entirely when unconfigured.
  The binding layer correlates and links out; it does not redraw what
  Temporal Web and Langfuse own.

- Version-aware workflow operations in the console (#243): the run page
  becomes **Runs & operations** — start a resolved workflow (showing the
  versioned type + spec digest it will register under, with client-side
  input-shape validation and the server's model validation surfaced as the
  422 envelope), a start receipt with a copyable `TraceListQuery` lookup,
  version-valid review submission (buttons render exclusively from the
  execution's `valid_user_decisions`, so invalid-for-version decisions
  cannot be selected), and cooperative cancellation behind a confirm.
  Status polling stays untraced at the recommended cadence; reviewer
  identity/notes/reasons stay out of `typeflux.*` metadata (but are sent as
  Temporal signal payloads and persist in workflow history; see #325).
  Completes the control-plane MVP arc (#213).

- Read-only control-plane console (#212): `@typeflux/console` under
  `clients/console` — JetBrains-dark Vite/React app over the generated
  client with a pure, unit-tested insights engine (unconfigured secrets,
  policy gaps/failures, version drift, mutable images, warn-mode review
  gates — all severity-ranked and deep-linked), a classified environment
  diff (critical = version/governance drift, warning = behavior divergence,
  info = expected), the read-only topology DAG with review arcs, the
  cross-version drain view, and untraced run inspection with live topology
  highlight. Operations render disabled pending #243; authoring stays in
  code. `controlplane serve` gains opt-in `--cors-origin` for
  separately-hosted consoles (dev uses the Vite proxy). A `Console` CI job
  type-checks, tests, and builds the app.

- TypeScript control-plane client (#242): `@typeflux/control-plane-client`
  under `clients/typescript` — contract types generated from the checked-in
  OpenAPI spec (`openapi-typescript`) plus a typed `openapi-fetch` wrapper,
  with no hand-written types. A new `TypeScript client` CI job regenerates
  the client and fails on drift, completing the chain Pydantic →
  `controlplane.v1.json` → `schema.ts`; versioned workflow types, spec
  digests, drain counts, and per-execution valid decisions ride through
  verbatim, and catalog JSON Schemas are directly consumable with ajv.

- Control-plane HTTP API, Temporal-connected tier (#241):
  start/status/review/cancel endpoints over `WorkflowOperations` and a
  `GET /workflows/{id}/versions` cross-version drain view over
  `workflow_drain_status`. Status polling defaults untraced with per-version
  `valid_user_decisions` and the recommended poll cadence; review/cancel stay
  always-traced with reviewer identity/notes/reasons kept out of `typeflux.*`
  metadata; start input is validated against the workflow's input model at
  the API edge. Operations pin the policy-hash-verified runtime per
  (workflow, environment, policy selection) and reuse the Temporal
  connection across requests (read endpoints stay per-request fresh).

- Control-plane HTTP API, read tier (#241): `typeflux.controlplane`
  (behind the new `api` extra) serves one project manifest over FastAPI —
  `meta`, workflow/environment discovery, `validate`, `bundle`, and `catalog`
  endpoints return the existing secret-free contract JSON unchanged, with
  `TypefluxError`-rooted config errors mapped to 422, unknown ids to 404, and
  runtime failures to 500 in one error envelope. The OpenAPI spec is exported
  deterministically (`python -m typeflux.controlplane openapi`),
  checked in at `docs/openapi/controlplane.v1.json`, and pinned by a drift
  test for downstream TypeScript codegen (#242).

- Workflow topology projection (#241, graph-ready for #55):
  `ResolvedWorkflowBundle.topology` exposes workflow structure as a
  nodes+edges DAG — activity/map nodes, sequential edges in declared step
  order, and review edges from the lifecycle checkpoint to each routed
  decision target (`condition` = decision name) — so a console can render
  and monitor structure without assuming a flat step list. A pure display
  projection: no digest or control-flow impact.

- Project activity catalog (#217): `resolve_activity_catalog` (and
  `project catalog` CLI) returns the discovered activities for a resolved
  workflow/environment in a UI-friendly, secret-free shape — AI vs plain
  Temporal kinds distinguished, schema identities plus JSON Schemas,
  secret-safe prompt/provider metadata for AI activities only, `used_by_steps`
  mapping, and `compatible_next` static type-compatibility edges for
  composition.

- Project-local component profiles (#214) with safe provenance (#215):
  `profiles:` in the project manifest declares file-referenced `provider`,
  `registry`, and `runtime` profiles, each owning a disjoint runtime subtree;
  workflows select at most one per kind and environments can swap a selection
  per workflow. Resolution applies one deterministic pipeline (workflow YAML
  < profiles < environment overrides) using the existing override-wins deep
  merge and override allowlist. Unknown references, kind mismatches, and
  out-of-subtree keys fail loudly in `project validate` and at resolution.
  Each applied profile is recorded as safe provenance — kind, id, name,
  content hash, source path, override paths — on the resolved workflow, in
  the bundle's `components` field, and (ids/hashes only) under
  `typeflux.components` in workflow metadata and execution-manifest
  contributions.

- `TypefluxError` exception root (exported from `typeflux`): every
  Typeflux domain error — provider, prompt-resolution, project policy /
  environment / deployment / policy-enforcement, preflight, metadata
  conflict, and activity output validation — now derives from it, so one
  handler can catch any Typeflux failure while still narrowing to domain
  roots. Errors that historically subclassed `ValueError`/`RuntimeError`
  keep those bases, so existing handlers continue to catch them. The
  `ModelProvider`/`AsyncModelProvider` protocols and extension docs now
  spell out the full optional capability contract (`provider_params`,
  `observation_context`, `usage_sink` kwargs and the `getattr`-probed
  provider attributes) that the executor detects by signature.

### Changed

- Internal consolidation (no behavior change): the OpenAI and Anthropic
  providers share error-classification and artifact-lookup helpers from
  `providers/_shared.py`, and activity-rollup construction is a single
  `build_activity_rollup` instead of duplicated copies in `yaml/runtime.py`
  and `execution/worker.py`.
- Project policy composition merges conflicting monotonic numeric scalars to
  the most-restrictive value instead of hard-failing: `artifacts.max_bytes`
  and `max_concurrent` take the minimum, `min_interval_seconds` takes the
  maximum (at any nesting depth). Retry/backoff fields, which have no clean
  "stricter" direction, still hard-fail so operators resolve them explicitly.

### Added

- `examples/regulated_claims_adjudication`: an end-to-end regulated-insurer
  example that exercises the Anthropic provider, normal Temporal activities
  for a deterministic compliance rules engine, a lifecycle review gate, and
  the control-plane `WorkflowOperations` API in one workflow. Includes offline
  discovery/graph tests and a live runner that verifies the run in Langfuse
  (generation usage, literal special-character rendering, curated lifecycle
  operations, plain activities, and absence of reviewer PII). Registered in
  the examples project manifest.
- Control-plane workflow operations API:
  `typeflux.project.WorkflowOperations` wraps the runtime lifecycle
  helpers in UI-safe DTOs — non-blocking `start` (returns
  `WorkflowStartReceipt` with workflow/run ids, versioned workflow type, spec
  digest, task queue, and a trace lookup hint), `status` (lifecycle snapshot
  plus the valid review decisions for the resolved version; untraced by
  default for UI polling with `trace=True` for auditable checks), and
  always-traced `submit_review`/`request_cancel`. Construction mirrors
  `project submit` (environment profile, policy guard, expected-policy-hash
  verification before any Temporal connection). The runtime gains
  `start_workflow`, a non-blocking start that applies the same identity memo
  and search attributes as `execute_workflow` and returns the raw Temporal
  handle. Operations never enter workflow execution manifests.

- Normal Temporal activities in YAML workflows: module discovery accepts
  plain `@temporalio.activity.defn` callables (exports and module globals)
  alongside `AIActivity` objects. They join the workflow graph with full
  input/output chain validation (the definition must declare one typed
  Pydantic input and a typed Pydantic return), are scheduled by name like any
  step, and are registered with the Temporal worker as-is — no AI wrapper, no
  registry/provider coupling, no AI preflight, no repair loop. Execution
  manifests and the resolved workflow bundle record them as planned activity
  names (`kind: temporal`) without prompt/provider metadata. Spec digests and
  versioned workflow types are unchanged. New
  `TemporalActivityDescriptor`/`YamlWorkflowActivity` contracts.

### Fixed

- Trace lookup by `workflow_id` now finds control-plane-started runs whose
  workflow id is present only on observation-level lifecycle or Temporal
  metadata, so `WorkflowStartReceipt.trace_query_hint` round-trips through the
  trace reader. Langfuse trace reads also request usage fields, and provider
  usage updates set the generation model so Anthropic token usage can produce
  derived Langfuse cost when the model is priced.

- `AnthropicProvider` no longer imports the SDK, loads env, or builds a real
  sync client when only `async_anthropic_client` is injected; sync
  `structured_call` with async-only injection now fails with a clear
  `ProviderConfigError` (mirroring the existing inverse case). The `anthropic`
  extra floor rises to `>=0.77` — verified as the first SDK version with
  `messages.parse`/`output_format` and `create(output_config=…)`; on 0.75/0.76
  both structured-output paths raise `TypeError`.

### Added

- Anthropic token-usage observability parity: providers can report usage
  through an optional `usage_sink` callback (capability-detected like
  `provider_params`/`artifacts`), the executor forwards the last reported
  usage to the Typeflux generation observation, and the Langfuse writer sets
  it as `usage_details` so Langfuse derives cost from model + tokens.
  `AnthropicProvider` reports `input_tokens`/`output_tokens` per call; OpenAI
  keeps its Langfuse drop-in instrumentation path. New `ProviderUsage`
  contract in `typeflux.providers`.

- Prompt rendering no longer HTML-escapes substituted values: `{{var}}`
  placeholders substitute field values literally (`&`, `<`, `>`, and quotes
  round-trip byte-for-byte). The chevron dependency is removed — the
  validated placeholder grammar is plain dot-path identifiers, and mustache
  sections were never documented or supported.

### Changed

- YAML environment interpolation no longer applies to prompt text: inline
  prompt bodies, message `content`, content-part `text`, and artifact
  `attach.text` keep a literal `${NAME}` verbatim instead of injecting worker
  environment values into prompts, providers, and traces. Prompt config keys
  (`model`, `provider_params`, …) still interpolate. Configuration strings
  gain an escape syntax: `$${NAME}` renders a literal `${NAME}` with no
  environment lookup.

- `PromptRef` semantics are now explicit: `version: int | None` pins an
  immutable Langfuse registry version (passed as `version=` to the SDK), and
  `label: str | None` selects a mutable label (passed as `label=`); they are
  mutually exclusive. The dead `labels` tuple and the `version_explicit` flag
  are removed. With neither selector set, resolution uses
  `runtime.registry.label` when configured, otherwise the `production` label —
  matching previous default behavior. YAML `prompt.version` now requires an
  integer (a string fails loading with a pointer to `label:`); existing specs
  using `version: production` migrate to `label: production`. The
  observability `PromptRefView` reader still accepts manifests written with
  the old string-version shape.

### Added

- `TypefluxYamlRuntime.wait_for_lifecycle_state(workflow_id, state, …)`:
  client-side wait helper that polls the lifecycle status query untraced
  (default one-second interval) until the requested state is reached, raising
  `TimeoutError` otherwise. `query_lifecycle_status` gains `trace=False` so
  internal polling never records curated lifecycle operation observations;
  explicit calls stay traced by default. The lifecycle review demo waits via
  the helper instead of a 0.25s raw polling loop, so traced demo runs show
  one root trace plus a small number of meaningful lifecycle operations, and
  docs now spell out curated lifecycle traces vs raw Temporal OTel spans and
  the 1–5s (or user-triggered) polling guidance.

### Fixed

- Deployment plan ConfigMaps emit the model under the provider-specific env
  key (`TYPEFLUX_ANTHROPIC_MODEL` for Anthropic, `TYPEFLUX_OPENAI_MODEL` for
  OpenAI; the fake provider emits none) instead of always using the OpenAI
  key, and `TYPEFLUX_ANTHROPIC_MODEL` joins the safe config allowlist.
- `examples/contract_risk_review/typeflux.anthropic.yaml` now defaults to its
  own task queue (`contract-risk-review-anthropic-typeflux`) and distinct
  workflow type so provider variants never share a worker queue; the support
  triage variant's workflow type is likewise disambiguated.
- README/status drift: shipped YAML capabilities (workflow compilation, map
  steps, review gates, lifecycle signals/queries) are no longer described as
  out of scope, and the README/docs teaching snippets use `value_from` secret
  references for the Temporal API key.

### Changed

- Project-generated Kubernetes Deployments gate on preflight: the rendered
  worker command clears any stale marker, runs `project run … --preflight`,
  writes `/tmp/typeflux-preflight-ok`, then execs the worker, and the
  container carries startup/readiness/liveness probes on that marker. The
  reference Dockerfile clears the marker the same way so a container
  restarted inside a pod never inherits the previous container's preflight.
- The reference `deploy/yaml-worker/kubernetes.yaml` is TLS-forward: it
  defaults to a Temporal Cloud-style endpoint with `TEMPORAL_TLS: "true"` and
  keeps the plaintext in-cluster variant as a commented local-dev option.
- Anthropic example variants are registered in `examples/typeflux.project.yaml`
  (`support_triage_langfuse_anthropic`, `contract_risk_review_anthropic`) and
  covered by the local validation target.

### Added

- `project drain-status` CLI (and `workflow_drain_status` API): running
  executions for a logical workflow grouped by versioned workflow type, with
  a drained exit code for gating decommission in rollout scripts. Gating
  always uses the `WorkflowType STARTS_WITH` prefix query so executions
  without the optional search attribute still count.
- Recorded-history replay harness: checked-in Temporal histories (plain and
  lifecycle-review runs) replayed with `temporalio.worker.Replayer` in normal
  CI — a clean replay of the unchanged fixture spec is the regression net for
  generated-control-flow changes, and a graph-edited spec is asserted to be
  structurally unable to replay the recorded history (different versioned
  workflow type).

- `runtime.temporal.workflow_search_attribute`: opt-in Keyword search
  attribute carrying the stable logical workflow name on every
  Typeflux-started execution, so one visibility query spans all versioned
  workflow types (cross-version runs lists and drain checks). The attribute
  must be registered in the namespace before enabling; caller-supplied search
  attributes are merged with the Typeflux key authoritative.
- Secret-reference enforcement for literal credentials: project policies gain
  `secrets.require_secret_references` (fails admission on literal
  temporal/provider API keys), the YAML loader warns on literal credential
  values (hardcoded or env-interpolated) pointing to `value_from`, literal
  values surface in secret-reference metadata as `source_kind: literal`
  (never the value), and the Temporal API key TLS guard is load-time for
  literals but connect-time for `value_from` references so local profiles can
  declare optional references with `tls: false`. Example YAMLs switch
  `${TEMPORAL_API_KEY:-}` interpolation to typed optional references.
- `ResolvedWorkflowBundle`: a stable, secret-safe control-plane contract that
  materializes one project/environment/workflow selection — identity (spec
  digest, versioned workflow type), resolved runtime settings, policy
  identity, activity descriptors, effective step retry/timeouts, lifecycle
  review decisions, secret references, validation results, and an optional
  deployment preview — via `typeflux.project.resolve_workflow_bundle`
  and the `project bundle` CLI subcommand. Composes existing resolution and
  validation surfaces; deterministic JSON for identical inputs.

- Apache-2.0 license file and package license metadata.
- Public `typeflux.__version__` package version surface.
- Narrow optional extras for OpenAI, Anthropic, and Langfuse installs.
- Release validation documentation and packaging metadata regression checks.
- Trace search results report incomplete scans (`TracePage.complete` and
  `TracePage.warnings`) instead of silently truncating at the scan-page bound,
  and search passes Typeflux tags to the backend when the API supports
  server-side tag filtering.

### Changed

- Hardened robustness and cost-control edges: provider retry delays honor
  `Retry-After` hints (the hint floors configured backoff) and add
  proportional jitter (`jitter_ratio`, default 0.1) so synchronized workers
  fan out; map steps gain `collect.max_bytes` (default 1.5MB, 0 disables)
  failing with an actionable error before Temporal's per-payload limit, with
  the guard participating in the spec digest; YAML documents are bounded at
  1 MiB and 1000 alias references with hosted/multi-tenant guidance
  documented.
- Duplicate mapping keys are now load errors across all Typeflux YAML
  (workflow specs, project manifests, environment profiles, policies)
  instead of silently resolving last-wins; YAML merge keys (`<<`) keep their
  normal override semantics. Prevents a file from displaying one value while
  the runtime and control-plane bundle resolve another.
- YAML-generated activity calls always carry a bounded Temporal retry policy
  (default `maximum_attempts: 5`, 1s initial interval, 2.0 coefficient, 60s
  cap) instead of inheriting Temporal's unlimited default. Configure via the
  per-activity `retry:` block or the `runtime.activity_retry:` default;
  `maximum_attempts: 0` opts back into unlimited. Exhausted output-validation
  repair (`AIActivityOutputValidationError`) now converts to a non-retryable
  Temporal failure, so blind activity retries cannot multiply provider spend
  on top of the local validation-repair loop. Activity heartbeating and
  cancellation cooperation are tracked in #208.
- Closed YAML/project policy gaps for worker enforcement, endpoints, and
  region assurance: `project submit` honors `--expect-policy-hash` /
  `TYPEFLUX_EXPECTED_POLICY_HASH`; `yaml.run` fails fast when an expected
  policy hash is set (it is the local-dev path and does not enforce policy);
  policies can constrain provider `base_urls` and prompt-registry
  `allowed_hosts`; `runtime.temporal.address_regions` maps addresses to
  regions so residency checks no longer rely solely on the self-attested
  `TYPEFLUX_TEMPORAL_REGION`; and `runtime.provider.class` is rejected for
  non-`fake` provider types instead of being silently ignored.
- YAML workflow graphs are now immutable versioned deployment artifacts.
  Executions register and start under a versioned Temporal workflow type
  derived from a canonical spec digest (`<workflow.name>.<digest12>`), or an
  explicit frozen `workflow.version` label. The spec digest, generator
  version, and registered workflow type are recorded in workflow execution
  manifests and `typeflux.yaml` trace metadata, and every start carries a
  `typeflux_spec_digest` memo. Changing the replay-relevant graph shape
  produces a new workflow type, so Temporal never replays an old history
  against a changed graph; see "Changing Workflow YAML Safely" in the
  deployment docs.
- YAML/provider configuration is now authoritative over Langfuse prompt config
  `model`/`provider_model` by default. Prompt config model fields stay visible
  in `langfuse.prompt_config` metadata but no longer steer execution unless
  `runtime.provider.allow_prompt_model_override: true` is set (the same flag
  exists on `LangfusePromptRegistry`). Activity execution manifests and
  rollups record the effective model origin under `provider_model_source`,
  and a mismatched `typeflux.provider_hint` in prompt config is surfaced as a
  sanitized warning.
- Unresolved activity rollup entries no longer duplicate prompt-resolution
  details under flattened `typeflux.prompt_resolution.*` keys; the nested
  `prompt_resolution` object is the manifest contract.
- Provider invocation metadata no longer emits the flat
  `typeflux.activity_name`, `typeflux.manifest_hash`, and
  `typeflux.activity_execution_manifest_hash` join keys. The structured
  `typeflux.activity_name` and `typeflux.join.*` fields are canonical; readers
  still accept the flat keys from older traces as legacy fallbacks.
- YAML lifecycle redaction exclusions enumerate known safe operational fields
  instead of preserving arbitrary `typeflux.lifecycle.*` values.

### Fixed

- Exact trace-id lookups (`get_trace`, `trace inspect`/`export`/`diff`) no
  longer lose traces older than the default 24-hour lookback window; bounds
  apply only when callers pass `since`/`until` explicitly.
- Root trace metadata refreshes (such as the post-start run-id refresh) no
  longer clobber executed activity manifest details with planned rollup
  entries.
- Closed observability and Temporal privacy leaks in error and status paths:
  user-supplied Langfuse clients now get call-site redaction of all
  observation inputs, outputs, and metadata (equivalent to the SDK mask
  installed on Typeflux-built clients); observation error status messages are
  sanitized (exception type names, validation summaries, or
  sanitized-by-construction provider/prompt errors only); exhausted output
  validation raises `AIActivityOutputValidationError` with a safe summary
  instead of the raw Pydantic error (which carried model output previews into
  Temporal history); and Anthropic provider errors use the same sanitized
  reason shape as OpenAI instead of raw exception text.
- `export_workflow_lifecycle_audit` accepts an optional `redactor` to mask
  reviewer identity, cancellation reasons, and failure messages; the
  lifecycle example's `audit` command gains `--redact`. Defaults still
  preserve those fields verbatim for compliance use, now documented.

