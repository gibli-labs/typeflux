# Control-plane HTTP API

> The API contract, permission model, and operator concepts on this page are
> **language-neutral** — the same generated client and console work against
> either edition's server. For serving a TypeScript project (the
> `typeflux-controlplane` bin, the `ts-plan-argument` binding, honest
> degradation), see [Control Plane (TypeScript)](typescript/control-plane.md).

The control-plane API (#241) is a thin, versioned HTTP adapter over the
existing `typeflux.project` functions — routing, serialization, and
error mapping only. It exists so a browser console (#212) or any non-Python
client (#242) can reach the resolved contracts; it adds no project/YAML
resolution logic of its own, and it is **read-only inspection, not an
authoring surface**: workflows stay authored in code and YAML.

## Install and run

The API ships behind the `api` extra:

```bash
pip install 'typeflux[api]'

typeflux-controlplane serve path/to/typeflux.project.yaml \
  --host 127.0.0.1 --port 8400
```

The TypeScript control plane ships a `typeflux-controlplane` bin (#807):
`typeflux-controlplane serve --registry typeflux.projects.yaml --port 8400`
(from `@typeflux/temporal-controlplane`). It has no `openapi` subcommand — the
Python control plane is the contract's normative generator. Each edition's CP
resolves ITS edition's projects natively and operates the other edition through
the language-neutral wire surface only (the recorded #812 decision — see
`project/binding_ts.py`); re-open triggers are recorded there.

The manifest is re-read per request, so YAML edits show up without a
restart. Serving a single manifest this way is the degenerate case of the
project registry (#256): a registry of one whose sole entry is the default
alias, served at the unprefixed `/api/v1/...` routes. To serve several
projects, see [Projects & multi-project serving](#projects--multi-project-serving)
below.

## Endpoints (read tier)

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/meta` | `api_version`, `bundle_version`, `catalog_version`, project identity |
| `GET /api/v1/workflows` | declared workflows (id, path/directory, profile selections) |
| `GET /api/v1/environments` | declared environments (id, profile path) |
| `GET /api/v1/validate` | `ProjectValidationReport`; optional `environment_id`, repeatable `workflow_id` / `policy_id` |
| `GET /api/v1/workflows/{id}/bundle` | `ResolvedWorkflowBundle`; requires `environment_id`, optional repeatable `policy_id`, optional `deployment_image` |
| `GET /api/v1/workflows/{id}/catalog` | `ActivityCatalog`; requires `environment_id` |
| `GET /api/v1/workflows/{id}/connections` | resolved registry/observer connection status (#258); requires `environment_id` |
| `GET /api/v1/workflows/{id}/prompt-status` | prompt-registry drift vs the resolved label/version (#254); requires `environment_id` |
| `GET /api/v1/policies` · `GET /api/v1/policies/{id}` | declared policies + one policy definition |
| `GET /api/v1/profiles` · `GET /api/v1/profiles/{kind}/{id}` | declared component profiles + one profile definition |
| `GET /api/v1/environments/{id}` | one environment definition (variable names only — never values) |
| `GET /api/v1/deployments` · `GET /api/v1/deployments/{plan_id}` | in-repo deployment plans with live drift verification + the promote command (#253) |
| `GET /api/v1/projects` | registered projects for the switcher (#256) — see [Projects](#projects--multi-project-serving) |
| `GET /api/v1/annotations` | the parsed `.typeflux/annotations.yaml` insight-acknowledgement projection (#733); pure-YAML project read — always servable, an absent file is an empty projection |
| `GET /api/v1/enforcement-events` | policy/admission enforcement feed (#723) over the resolved observability backend; requires `environment_id`, optional repeatable `workflow_id`/`policy_id`/`verdict`, `since`/`until`, `limit` |
| `GET /api/v1/github-provenance` | HEAD-vs-served drift + plan→approving-PR mapping (#727) via the injected GitHub transport; 501 on projects without the `github_provenance` capability |

Responses carry the same secret-free contract JSON as the project CLI
(`typeflux-project bundle|catalog|validate`): bundle and
validate payloads omit `null` fields, the catalog keeps them explicit. The
one deliberate divergence: `/validate` always includes `resolved_workflows`
(possibly empty) where the CLI omits the empty list — a stable shape is
kinder to generated clients.

Secret *values* never appear in any response; secret *references* (the
`value_from` env var names and their configured state) stay inspectable in
`bundle.secret_references`.

## Endpoints (Temporal-connected tier)

These wrap `WorkflowOperations` and `workflow_drain_status`; they connect to
the Temporal cluster configured in the resolved runtime.

| Endpoint | Does |
| --- | --- |
| `POST /api/v1/workflows/{id}/start` | start one execution; body `{environment_id, execution_id, input, task_queue?, policy_ids?, expected_policy_hash?}` → `WorkflowStartReceipt` |
| `GET /api/v1/workflows/{id}/status` | lifecycle snapshot + the review decisions valid for *that execution's version* + recommended poll cadence + `runtime_pin` (the `spec_digest` and pin time of the runtime mutating ops are bound to — identity for visibility, not a freshness verdict); `trace=false` by default so poll loops stay out of the audit trail (`trace=true` records a deliberate, audited check and requires an operate-class permission — see [auth](control-plane-auth.md)) |
| `POST /api/v1/workflows/{id}/review` | submit a review decision; body carries a `ReviewCommand` (`user_decision`, optional `reviewer`/`notes` — kept out of `typeflux.*` metadata, but sent as the Temporal review-signal payload so they persist in workflow history; not client-side) → 204 |
| `POST /api/v1/workflows/{id}/cancel` | request cooperative cancellation with an optional reason → 204 |
| `POST /api/v1/workflows/{id}/repin` | drop the pinned operations runtime for `{environment_id}` so the next mutating call re-resolves the current YAML — refresh the pin without a `serve` restart (#324); requires `project.refresh` → `{repinned, dropped}` |
| `GET /api/v1/workflows/{id}/versions` | cross-version drain view: running executions per versioned workflow type and the fail-safe `drained` flag, for the drain-then-decommission flow |
| `GET /api/v1/workflows/{id}/executions` | recent executions for the workflow (#251); requires `environment_id`, optional `limit` |
| `GET /api/v1/workflows/{id}/correlation` | observer-aware run↔trace correlation for one `execution_id` (#251); requires `environment_id` |
| `GET /api/v1/workflows/{id}/workers` | resolved task queue + live poller count via `describe_task_queue` (#278), degrading to `reachable: false` when Temporal is unreachable; requires `environment_id`, optional `task_queue` |
| `POST /api/v1/workflows/{id}/migrate` | long-drain migrate (#204): terminate the targeted execution and resubmit it against the current version; body `{environment_id, execution_id, run_id?, abandon_gates?, reason?, policy_ids?, expected_policy_hash?, dry_run?}` → `WorkflowMigrateResult` (`dry_run: true` runs every preflight — binding, same-version, pollers, gates, input decode — and returns the preview with `new_run_id: null` without terminating, #791); requires `start` **and** `cancel` (see [auth](control-plane-auth.md)) |

The start body's `input` object is validated against the workflow's input
model before submission; invalid input is a 422.

**Bounded Temporal tier (#581):** every Temporal-connected call (the
operations build/pin, start, status, review, cancel, executions, workers,
versions) is cancelled after a bound — default **10s**, configurable via
`serve --temporal-timeout`, `TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS`,
or the `create_app(..., temporal_tier_timeout_seconds=…)` parameter — and a
refused or timed-out connect answers **503** (`TemporalUnavailable`). The
cancellation unwinds the process-wide env-resolution lock, so the *awaited
Temporal portion* of one request against an unreachable/blackholed cluster
can no longer hold the read tier hostage (the build's synchronous client
init still runs on the loop — #585 tracks that). Note the `workers`
endpoint's soft degradation (`reachable: false` with a 200) still applies to
fast failures; a probe that exceeds the bound is a hard 503.

**Execution binding (status / review / cancel):** before any query or signal,
the operation `describe`s the addressed execution and verifies it is the
workflow this route serves — its registered `workflow_type` must match the
routed type (workflow identity *and* graph version) and its `typeflux_project`
memo must match the routed project. A mismatch (an id collision, a typo, or a
cross-project attempt) fails closed with **409** before the op reaches the
execution. There is no legacy fallback: an execution started outside the
Typeflux runtime carries no identity memo and is refused.

**Freshness split (deliberate):** read endpoints re-read the manifest per
request, so YAML edits show up immediately. Operations pin the resolved
runtime — including the verified policy hash — at first use per
`(workflow, environment, policy selection)` and reuse the Temporal
connection across requests, so status polling at the recommended cadence
does not re-resolve YAML or reconnect per tick. The skew this creates is now
visible and recoverable (#324): `status` reports `runtime_pin` (the pinned
`spec_digest` and pin time, so an operator can see which version mutating ops
are bound to), and `POST .../repin` drops the pin so the next mutating call
re-resolves — refreshing without a `serve` restart. `runtime_pin` is identity,
not a freshness verdict: the digest covers the workflow graph, not runtime
config (task queue, Temporal profile, policy), so repin rather than digest
comparison is the way to refresh. Like every project entry point, operations
fail closed on policy drift: a mismatched `expected_policy_hash` is rejected
before any Temporal connection.

## Authorization

The server is **open by default** (it binds `127.0.0.1` and trusts its caller),
but every operation is gated by a permission — `inspect` for reads, and
`start` / `review` / `cancel` / `project.refresh` for the mutating endpoints.
Configure built-in bearer tokens (`--auth-token NAME:PERMS:TOKEN`) or trust an
authenticating reverse proxy (`--trust-proxy-auth`) before exposing the API
beyond a trusted operator network. `GET /meta` reports the caller's
`capabilities` so the console disables actions it cannot perform. See
[Control-Plane Auth](control-plane-auth.md) for the full matrix and deployment
patterns.

## Projects & multi-project serving

One server can serve several projects (#256). A registry file lists them;
the single-manifest `serve` above is the registry of one.

```bash
typeflux-controlplane serve \
  --registry path/to/typeflux.projects.yaml \
  --clone-cache path/to/.typeflux-clones      # optional; default next to the registry
```

```yaml
# typeflux.projects.yaml  (see packages/python/examples/typeflux.projects.yaml)
version: "1"
default: examples            # optional; first entry otherwise
projects:
  - id: examples
    manifest: typeflux.project.yaml        # local checkout (path relative to this file)
  - id: upstream
    repo:                                  # Git-sourced (see below)
      url: https://github.com/org/repo
      ref: main
      manifest: typeflux.project.yaml      # path within the repo
  - id: ts-edition
    manifest: ts/typeflux.project.yaml
    runtime: typescript                    # declared language runtime (#619)
```

Each entry sets **exactly one** of `manifest` (local) or `repo` (Git-sourced).
`runtime` (default `python`) declares which language runtime can resolve the
project's modules. A project whose runtime this server has no resolver for
stays **inspectable** (pure-YAML reads keep working) but resolution-dependent
operations — bundle, catalog, validate, prompt-status, deployments, and the
whole operate tier — answer `501` with the `UnsupportedRuntime` error
discriminant, fail closed. `/meta.capabilities` and the projects listing
(`runtime`, `resolvable`) report the distinction honestly.

**Routes.** Every read/operation endpoint is served twice from one handler
set: unprefixed (`/api/v1/...`, serving the **default** project — so the
single-project dev loop is unchanged) and project-scoped
(`/api/v1/projects/{project}/...`, serving any registered project). The
manifest is still re-read per request; the operations cache key carries a
project dimension so a workflow id shared across projects never shares a
pinned connection. `GET /api/v1/projects` lists the registry for the
console switcher.

### Git-sourced projects

A `repo` entry is cloned into the clone cache (`--clone-cache`, or
`.typeflux-clones` next to the registry) on first resolve and re-read per
request thereafter. The registry file is **operator-authored, trusted
config** — there is no user-supplied URL path — so cloning its URLs is in
scope; clones use subprocess git only. A `manifest` path that escapes the
clone directory (`..`) is rejected.

Serving a project (Git-sourced or a local `manifest` resolved from another
working directory) puts its **root on `sys.path`** — the manifest directory —
so the workflow's Python package (`project: <pkg>`) imports. This expects a
**flat layout**: the project's top-level package sits at the manifest
directory. Because imports are process-global, two served projects must not
expose the **same top-level package name**; give each a unique or namespaced
package. Private repos: the clone uses the host's ambient git auth (SSH key or
credential helper) — there is no credential field in the registry yet.

| Endpoint | Does |
| --- | --- |
| `POST /api/v1/projects/{id}/refresh` | re-fetch a Git-sourced clone (`git fetch` + checkout + hard-reset to the ref) and invalidate that project's pinned operations so the next call re-resolves the new sha; a no-op for local checkouts → `ProjectRefreshResult` |

Read endpoints are already per-request fresh, so they reflect the new
checkout immediately after a refresh.

## Workflow topology

`ResolvedWorkflowBundle.topology` projects the workflow structure as a
nodes+edges DAG so a console can render it and highlight live progress —
a display/monitoring affordance, never authoring:

```json
{
  "nodes": [
    {"id": "assess_items", "kind": "map", "activity": "assess_item"},
    {"id": "summarize", "kind": "activity", "activity": "summarize"},
    {"id": "decide", "kind": "activity", "activity": "decide"}
  ],
  "edges": [
    {"source": "assess_items", "target": "summarize", "kind": "sequential"},
    {"source": "summarize", "target": "decide", "kind": "sequential"},
    {"source": "assess_items", "target": "summarize", "kind": "review", "condition": "approve"},
    {"source": "assess_items", "target": "decide", "kind": "review", "condition": "fast_track"}
  ]
}
```

Today's vocabulary, derived from structures the bundle already records:

- **Nodes** are steps: `kind: "activity"` or `kind: "map"` (a map step is a
  single fan-out/collect node; its shape stays on `steps[].map`).
- **Sequential edges** follow declared step order — also the
  `invalid_user_decision: warn` fall-through path.
- **Review edges** fan out from the lifecycle review checkpoint to each
  routed decision target, with `condition` carrying the decision name. Route
  targets are type-checked at build time: every routed step must consume the
  review checkpoint's output type.

Graph-shaped orchestration (#55) extends these node/edge kinds; it does not
replace the contract. The topology is a pure projection of graph shape
already covered by the spec digest (#191) — it changes no control flow.

## Error mapping

Every non-2xx response uses one envelope: `{"error": <name>, "message": <safe text>}`.

| Condition | Status | `error` |
| --- | --- | --- |
| Unknown `workflow_id` / `environment_id` | 404 | `NotFound` |
| Config/validation failure (`TypefluxError` + `ValueError`, e.g. `ProjectProfileError`, `ProjectEnvironmentError`, `ProjectPolicyError`) | 422 | exception class name |
| Malformed request (missing/invalid parameters) | 422 | `RequestValidationError` |
| Lifecycle op addressed an execution that is not the routed workflow/project | 409 | `LifecycleBindingError` |
| Unexpected Typeflux runtime failure | 500 | exception class name |

## OpenAPI contract

The API is defined by the normative OpenAPI contract at
[contracts/controlplane/openapi.v1.json](../contracts/controlplane/openapi.v1.json)
(#616). The server conforms to it: the conformance check asserts the
FastAPI-emitted schema matches the document and fails with a structured
path-by-path divergence report, so a contract change the server does not
implement (or a server change the contract does not describe) fails CI with
the exact JSON-Pointer paths that diverge. Contract changes are reviewed as
interface changes — see
[contracts/controlplane/README.md](../contracts/controlplane/README.md) for the
change process.

```bash
# Check conformance (exit 1 + structured divergence report on drift). The
# default contract path resolves inside the monorepo checkout; a
# pip-installed package must point at the document explicitly:
typeflux-controlplane conformance [--contract PATH]

# Export the emitted schema for inspection:
typeflux-controlplane openapi --out -
```

## Console

[`clients/console`](../clients/console/) is the control-plane console
(#212/#243): overview with a severity-ranked insight feed, workflow detail
with the topology DAG, the cross-workflow **Runs** surface (#589 — every workflow's recent executions, failure-first, linking to shareable run views; the run inspector adds runtime-pin-skew detection with a permission-gated repin), the **Governance** page (#587 — the policy coverage matrix from resolved bundles, a gaps feed, structured policy rules with the extends chain), the project-level **Drift** page (#583 — plan drift
and coverage gaps, critical cross-environment drift, on-demand prompt drift,
every row deep-linked with its remediation), the **environment diff** (every
differing bundle path classified critical/warning/info — version and
governance drift vs expected divergence), the cross-version drain view, and
**runs & operations** — start (showing the versioned type it will register under),
untraced status inspection with live topology highlight (the inspected run
is URL state — `?run=<execution_id>` — so a run view is shareable and
restored on cold load, #580), version-valid review submission, and
cooperative cancellation. Project policies apply
fail-closed to console starts exactly as to CLI submits. The **Deployments**
page (#253) lists in-repo plans with ready/drifted/preflight/digest-pin
badges and the copyable promote command. When the server serves several
projects (#256), the sidebar gains a **project switcher** and the overview a
cross-project **Projects** panel — local and Git-sourced, with a **refresh**
action for Git-sourced projects; a fetch middleware transparently rescopes
every request to the active project. See its README for the dev workflow
(`serve` + Vite proxy) and the repo-migration note.

`serve` accepts repeatable `--cors-origin` flags for a console hosted on a
different origin; by default no CORS headers are emitted.

External deep-links (#250): set `TEMPORAL_UI_URL` (Temporal Web base) and
`LANGFUSE_PROJECT_URL` (the project-scoped Langfuse base, e.g.
`https://cloud.langfuse.com/project/<id>`) in an environment's profile and
the bundle exposes them under `links` — the console then renders
"Open in Temporal ↗" on runs and "Langfuse ↗" on prompt refs. URLs only,
allowlisted variables only; affordances stay hidden when unset.

### Acknowledging insights — `.typeflux/annotations.yaml`

Console insights can be acknowledged or suppressed with an in-repo,
PR-reviewed file (#733): `.typeflux/annotations.yaml` beside
`typeflux.project.yaml`. Git is the single source of truth — there is no
console-side or control-plane-side mutable ack state; the console's
"acknowledge" affordance generates the YAML entry for you to commit.

```yaml
annotations:
  - insight_id_pattern: "policy.drift.*"   # exact insight id, or a glob (* only)
    reason: "tracked upstream; not actionable until the provider ships the fix"
    tracked_in: "https://github.com/acme/infra/issues/412"   # optional http(s) issue URL
    expires: 2026-12-31                                       # optional ISO date
```

Field rules (`extra: forbid` — an unknown or typo'd key is an authoring
error, never silently ignored):

- `insight_id_pattern` (required): the console's stable insight id, exact or
  a glob whose only wildcard is `*`.
- `reason` (required): an ack without a reason is not an ack.
- `tracked_in` (optional): shape-validated http(s) URL; rendered as a link,
  never fetched.
- `expires` (optional): ISO date. A **non-expired** entry collapses its
  matches into the acknowledged disclosure; once the date passes, the entry
  **un-collapses** — its matches return to the active feed and a loud
  `annotation:stale:` insight names the rotted ack (stale-ack insights are
  themselves never acknowledgeable). An expired entry is still served with
  its expiry; deriving staleness is the console's job.

Parsing is fail-closed: an absent file is an empty projection (the common
case), and a malformed file yields an empty projection plus a project
**validation issue** — never a half-parsed subset. The projection is served
at `GET /api/v1/annotations`, consumed by the console's insight feed, and
exposed to agents as the MCP `list_annotations` tool.

## TypeScript client

[`clients/typescript`](../clients/typescript/) packages
`@typeflux/control-plane-client`: contract types generated from the
normative OpenAPI contract via `openapi-typescript`, wrapped by a typed
`openapi-fetch` client, published to the GitHub Packages npm registry with
the major version locked to the contract's API major. Nothing is
hand-written — the conformance chain is
`contracts/controlplane/openapi.v1.json` (normative) → Python server
(structured-diff conformance gate) and → `src/schema.ts` (the Web CI job
regenerates and fails on any diff, then builds the package). The console
consumes a **pinned published release**, so a contract change reaches it in
three steps: contract PR (conforming Python change + regenerated client +
version bump together) → client release workflow → console bump PR.

## MCP server (agent integration)

[`clients/mcp`](../clients/mcp/) packages `typeflux-mcp`, a Model Context
Protocol server that makes a coding agent fluent in — and able to operate and
author — a Typeflux project. **#326 is complete** (Phases 0-3):

- **Read tier (Phase 0).** Every control-plane READ (bundle, catalog, validate,
  topology, meta, ...) as MCP resources and read tools over
  `@typeflux/control-plane-client`, plus static resources for the docs,
  examples, generated JSON-Schemas, and an authoring checklist.
- **Operate tier (Phase 1).** `start` (preview-then-commit with
  `expected_policy_hash` + input elicitation), subscribable `status`, `review`,
  `cancel`, `repin`, `refresh` — each capability-gated from `/meta` and
  confirmation-hinted; fail-closed governance is preserved end-to-end.
- **Recipes + completions (Phase 2).** The `/typeflux:*` guided recipes and
  live-project ID completions.
- **Authoring aids + transports (Phase 3).** Local `scaffold_ai_activity` /
  `scaffold_workflow_yaml` / `scaffold_project_entry` / `doctor` tools that
  **return content** for the editor to apply as a reviewable diff (they never
  write files — the authoring boundary holds), and a **Streamable-HTTP**
  transport (bearer-authed, per-session isolation) for a shared/hosted server
  alongside the stdio default.

By default it drives the TypeScript control plane in-process against a manifest
discovered via MCP roots (managed-local); `TYPEFLUX_CP_URL` attaches to a
running control plane instead. The one deferred item is the observability
`trace_*` tools — blocked on a control-plane trace surface (a separate
follow-up), not on more MCP work. See
[`clients/mcp/README.md`](../clients/mcp/README.md).
