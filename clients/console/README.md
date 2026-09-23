# Typeflux Console

> **Repo boundary**: this directory is a planned extraction target with its own
> Apache-2.0 license — see [LICENSE](LICENSE) and the [extraction runbook](EXTRACTION.md).

Control-plane console for one or several Typeflux projects: **inspect,
understand, monitor, operate** workflows authored in code. Explicitly not a
graph editor or authoring tool — authoring stays in code, and the console
renders what the resolved contracts say.

## Product and visual principles

The console uses the Typeflux web v2.1 system as its source of truth: Inter,
near-black copy, neutral white/canvas surfaces, Typeflux indigo for focus and
selection, semantic status colors, generous panel radii, subtle shadows, and
short motion. Its compact navigation and working surfaces borrow the clarity
and information density of modern AI tools without changing Typeflux's core
promise: unknown, partial, unsupported, and planned states are always named;
an empty surface is never presented as a fabricated all-clear.

## Run it

```bash
# 1. Serve the control-plane API over your project manifest:
uv run --extra api python -m typeflux.controlplane serve \
  examples/typeflux.project.yaml          # http://127.0.0.1:8400

# …or serve several projects from a registry (see examples/typeflux.projects.yaml):
uv run --extra api python -m typeflux.controlplane serve \
  --registry examples/typeflux.projects.yaml

# 2. Start the console (proxies /api to 8400, no CORS needed):
cd clients/console
npm ci
npm run dev                               # http://127.0.0.1:5173
```

`@typeflux/control-plane-client` installs from the public npm registry —
no credentials or registry configuration needed.

A separately-hosted console must point at the API origin
(`localStorage.setItem("typeflux.apiBase", "https://…")`) and the server
must opt in with `--cors-origin`.

## What it shows

- **Overview** — every workflow in the selected environment: versioned
  workflow type, spec digest, policy hash, validation state, and a
  severity-ranked **insight feed** (unconfigured secrets, missing/failed
  policy, validation issues, mutable deployment images, warn-mode review
  gates). Every insight deep-links to the evidence. The project
  validation-issues table adds GitHub **source links** for file-class
  failures (#577 §10 — `missing_workflow_file` / `workflow_load_error` /
  `invalid_workflow_yaml`): the manifest entry and the expected workflow
  YAML at the served sha, shown only when the link is buildable.
- **Workflow detail** — identity (versioned type + digest), the read-only
  **topology** DAG (#55): a ranked graph of declared steps. Sequential steps
  run along the spine; a `parallel` block (stacked cards, like `map`) fans out
  to each branch (branches stack in rows and `collect` back where the merged
  value materializes), and a `when`-gated `branch`/`conditional` edge carries a
  `when` pill that reveals its predicate in an instant hover panel. Sub-workflow
  nodes (`SUB`) link to the child workflow's page, and a dedicated
  **Sub-workflows** section after Activities lists each child with its calling
  steps and source path. Each review decision gets its own labeled swim-lane
  below the deepest row — decisions are *entry points* into the downstream
  chain, so the checkpoint has no unconditional outgoing arrow (its decision
  lanes are the flow; warn-mode fall-through renders dotted when no decision
  covers the next step), and the chain continues from wherever a decision
  enters. Plus activities
  with JSON Schemas, lifecycle review routes, policy identity, component
  provenance, secret references (names and configured state only — never
  values), runtime config, validation, and the deployment preview with the
  copyable deploy command.
- **Drift** (#583) — one project-level answer to "what is drifting right
  now": **GitHub-vs-served** drift (#727 — the served checkout compared
  against the tracked branch HEAD on GitHub, read server-side at request
  time, with a commits-behind count and a GitHub compare-view link),
  approved-plan drift (and coverage gaps), critical cross-environment
  bundle drift (version/governance only), on-demand prompt-registry
  drift per workflow, and the two Temporal-tier classes (#577 §1):
  **version drain** (runs still executing on old versioned types, per
  workflow, "drain before decommission") and **runtime-pin skew** (the
  provable graph-changed case, per workflow, reusing the run inspector's
  #592 derivation — a matching digest is *not* a freshness verdict). The
  two Temporal-tier classes are on-demand checks like prompt drift (a
  routable-but-dead cluster answers each read only at its bound, so the
  fan-out never fires on page load) and degrade LOUDLY when Temporal is
  unreachable — an explicit "status unknown", never an empty all-clear.
  Every row names why it matters and what to do next, and deep-links to
  the surface that owns the detail.
- **Runs** (#589) — every workflow's recent executions in one
  failure-first table (failed/terminated/timed-out first, then running,
  unknown statuses above completed); rows open the addressable
  `?run=` view. The run inspector also surfaces **runtime-pin skew**
  (mutating ops bound to an older resolution than the current YAML) with
  a capability-gated repin action.
- **Governance** (#587) — where policy applies: the coverage matrix
  (workflows × environments, the *applied* policy ids + composed hash from
  each resolved bundle), a gaps feed (unprotected / composition-failed,
  with remediation links), and every policy rendered structurally with its
  `extends` chain and section-level provenance. Effective rule values stay
  the backend's job — the console never re-implements the merge.
- **Policy violations & enforcement** (#723 / #577 §2) — the queryable feed
  of where policy actually *fired*: admission verdicts (workflows that would
  be rejected now, normalized from the same validation surface each workflow
  page renders) and runtime blocks (moderation, read from Langfuse traces in
  a bounded window). Read at request time — nothing is persisted server-side.
  Capability-gated on `enforcement_events`: a control plane that doesn't
  advertise it renders an explicit "not supported" panel, never an error or a
  fake empty. Degradation is loud — an unreachable/absent Langfuse observer
  banners that runtime events may be missing rather than silently omitting
  them. Filterable by workflow (scoped to workflows that resolve in the env),
  verdict, and window, with cursor "load more". One shared component, mounted
  on both this Governance surface and the Governance *persona* view.
- **Environment diff** — the resolved bundle of the same workflow across two
  environments, with every differing path classified: **critical** =
  version/governance drift (spec digest, policy hash, secret-configured
  mismatches), **warning** = behavior may differ (provider/model, component
  hashes), **info** = expected divergence (addresses, env files).
  "Identical" is a first-class result.
- **Versions & drain** — running executions per versioned workflow type and
  the fail-safe `drained` flag for the drain-then-decommission flow.
- **Runs & operations** (#243) — start a resolved workflow version (the
  panel shows the versioned type and spec digest it will register under;
  input is validated client-side for shape and server-side against the
  workflow's input model), get the start receipt with a copyable
  `TraceListQuery` lookup, inspect by execution id (untraced status, live
  topology highlight, recommended-cadence auto-poll; the inspected run is
  URL state — `?run=<execution_id>` — so a run view is shareable and
  restored on cold load, #580), submit a review —
  only the decisions valid for *that execution's version* are offered — and
  request cooperative cancellation. Reviewer identity, notes, and reasons
  stay out of `typeflux.*` metadata, but are sent as Temporal signal payloads
  (persist in workflow history; the cancel reason is returned to `inspect`
  callers) — not client-side (#325). When the control plane vouches for a
  caller identity (`/meta.caller_identity`, trusted proxy auth, #577), review
  decisions are attributed to that principal and the free-text reviewer field
  becomes a read-only attribution; token/open auth keeps free text.
- **Deployments** (#253) — the in-repo deployment plans per workflow:
  ready-to-promote / drifted / preflight / digest-pin badges, policy and
  code-commit provenance, per-field drift mismatches, the **Approving PR**
  field (#727 — each plan's plan-file commit linked to the merged/approving
  PR from one project-level provenance read; capability-gated, and honest
  when a lookup is degraded, capped, or still in flight rather than a
  fabricated "none"), and the copyable promote command. Promotion stays a
  CLI step approved by merging the plan file's PR; the console never mutates
  a plan or emits artifacts.

## Multiple projects

When the API serves a registry (`serve --registry`, #256) the sidebar shows
a **project switcher** and the overview a cross-project **Projects** panel —
each project's source (local manifest, or a Git `url @ ref`), an active/switch
control, and a **refresh** action for Git-sourced projects (re-fetches the
repository on the server). Selecting a project persists the choice and
reloads; a fetch middleware rewrites every request to that project's scoped
routes (`/api/v1/projects/{id}/...`), so a single-project server (the default)
needs no switcher and behaves exactly as before.

## Architecture notes

- All contract types come from `@typeflux/control-plane-client`
  (generated from the normative contract, consumed as a **pinned published
  release** — no workspace link); the console declares no contract shapes by
  hand. Picking up a contract change is a three-step loop: contract PR
  (conformance-gated) → client release (`Release control-plane client`
  workflow) → console bump PR updating the pin here.
- The substance lives in pure, unit-tested engine modules: `insights.ts`
  (per-workflow severity rules), `diff.ts` (drift classification),
  `topology.ts` (DAG layout), `driftFeed.ts` (project-level drift rows),
  `githubDrift.ts` (#727 — the GitHub-vs-served drift feed, the plan→PR
  hand-off, and all their notice/tooltip/remediation copy, so every surface
  reads one settled, partial-aware verdict). Components render their output;
  new derivations get their own sibling engine module when they aggregate
  above a single workflow.
- The shell (#578): TanStack Router over **hash history** (`router.tsx` —
  every `#/…` URL is a typed route with validated search params) and TanStack
  Query (`queries.ts` — every read is a cached query keyed
  `[resource, ...params]`). The layout lives in `shell/Shell.tsx`; pages
  stay presentational and prop-driven.
- Panels (#580): page sections live in `panels/workflow.tsx` and
  `panels/runs.tsx` as independent components that pages compose (dependency
  direction: pages → panels → components/queries; panels never import
  pages). New surfaces should recompose existing panels before adding code.
- Dependencies are deliberately minimal: react, react-dom, the generated
  client, @tanstack/react-router + @tanstack/react-query; no UI framework.
- **Repo migration:** the package is self-contained — the client dependency
  is already a pinned published version (zero `file:`/`workspace:` links into
  the monorepo), so moving the console to its own repository is copying this
  directory.

```bash
npm run typecheck   # strict tsc
npm run test        # vitest (insight/diff/layout engines)
npm run build       # production bundle
```

### End-to-end smoke (#293)

`npm run test:e2e` runs a Playwright smoke that brings up a real
control-plane API (over the examples project, registered under two ids in
`e2e/typeflux.projects.yaml`) and the console dev server, then verifies the
key read-only pages render and that switching project rescopes requests to
`/api/v1/projects/{id}/...`. It is **non-live** — read endpoints need no
Temporal. One-time setup: `npx playwright install chromium` (and `uv sync
--extra api` so the API can serve). CI runs this as the `console-e2e` job.
