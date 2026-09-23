# typeflux-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that makes a coding agent
(Claude Code, Cursor, Codex, ...) fluent in — and able to operate and author — a Typeflux project. It
exposes the Typeflux docs, examples, JSON-Schemas, and the live control-plane governance reads
(bundle, catalog, validate, topology, meta, ...) as MCP resources and tools, a capability-gated
operate tier, guided `/typeflux:*` recipes with live-project ID completions, and local authoring aids
that return reviewable-diff content.

The full **Phase 0-3** surface (#326 is complete): the read tier (Phase 0), the confirmation-hinted
operate tier (Phase 1), the guided recipes + completions (Phase 2), and the local authoring aids
(`scaffold_*`, `doctor`) + the Streamable-HTTP transport (Phase 3). It **never writes files** — the
scaffold tools _return content_ for the editor to apply as a reviewable diff, and this server
validates, explains, resolves, and operates what you wrote. Reads are free; the operate verbs are
permission-gated and require explicit client confirmation.

## Install & run

```bash
npx -y typeflux-mcp
```

`typeflux-mcp` publishes to public npmjs.org, so `npx` works with no auth. It speaks MCP over
**stdio** by default, or over **Streamable HTTP** for a shared/hosted server (see
[Transports](#transports)).

## Two modes

| Mode | Trigger | What it does |
| --- | --- | --- |
| **managed-local** (default) | `TYPEFLUX_CP_URL` unset | Discovers your `typeflux.project.yaml` (via the MCP client's roots, or `TYPEFLUX_MANIFEST`) and drives the TypeScript control plane **in-process** on loopback, torn down on exit. Pure Node — no Python needed. |
| **attach** | `TYPEFLUX_CP_URL` set | Talks to an already-running control plane (local or hosted, Python or TS) for full resolution and multi-project access. |

### Environment

| Variable | Meaning |
| --- | --- |
| `TYPEFLUX_CP_URL` | Attach to this control-plane base URL (selects attach mode). |
| `TYPEFLUX_CP_TOKEN` | Bearer token for a token-protected control plane. Never logged. |
| `TYPEFLUX_PROJECT` | Scope every read to this project id (multi-project attached CP). |
| `TYPEFLUX_MANIFEST` | managed-local: explicit `typeflux.project.yaml` path (bypasses roots discovery). |
| `TYPEFLUX_RUNTIME` | managed-local: `typescript` (default) or `python`. A `python` project stays inspectable (pure-YAML reads) and answers `UnsupportedRuntime` on resolution-dependent reads. |
| `TYPEFLUX_MCP_TRANSPORT` | `stdio` (default) or `http` (Streamable HTTP — see [Transports](#transports)). |
| `TYPEFLUX_MCP_HTTP_TOKEN` | **Required** in http mode: the bearer token every request must present. Never logged. The server refuses to start in http mode without it. |
| `TYPEFLUX_MCP_HTTP_HOST` / `TYPEFLUX_MCP_HTTP_PORT` | http mode bind host/port (default `127.0.0.1:8765`). |

## Editor configuration

**Claude Code** (`.mcp.json` / `claude mcp add`):

```json
{
  "mcpServers": {
    "typeflux": {
      "command": "npx",
      "args": ["-y", "typeflux-mcp"]
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`) and **Codex** use the same shape: command `npx`, args
`["-y", "typeflux-mcp"]`. To attach to a running control plane, add
`"env": { "TYPEFLUX_CP_URL": "http://127.0.0.1:8080" }`.

## Transports

- **stdio** (default) — one local, single-user editor session over the process's stdin/stdout. This
  is the `npx -y typeflux-mcp` path above.
- **Streamable HTTP** (design §8.2) — for a **shared/hosted** server in front of a team control plane
  (CI bots, a review-dashboard agent). Selected with `TYPEFLUX_MCP_TRANSPORT=http`. Its security
  posture is non-negotiable:
  - **Bearer auth is required.** Every request must carry `Authorization: Bearer <TYPEFLUX_MCP_HTTP_TOKEN>`;
    a missing/wrong token is rejected with `401` before any MCP handling (constant-time compare). The
    server **refuses to start** in http mode with no token set — a shared server can never come up
    unauthenticated.
  - **Sessions are isolated.** Each MCP session gets its **own** server instance and its **own**
    control-plane backend (keyed by the Streamable-HTTP session id); one caller's state —
    subscriptions, resolved backend, capability gating — never leaks into another's. A session's
    backend is prepared only after a successful `initialize`, and disposed when the client terminates
    it (DELETE) or the server closes.
  - **Sessions are bounded.** Concurrent sessions are capped (a new `initialize` past the cap is
    refused `503`), and an idle sweep disposes sessions unused beyond a timeout — so a client that
    disconnects without a DELETE, or one team token opening endless sessions, can't grow memory
    without bound. (The idle sweep is authoritative because a bare disconnect doesn't fire a close
    event, and the SSE stream legitimately reconnects, so it is never disposed on stream close.)

  ```bash
  TYPEFLUX_MCP_TRANSPORT=http \
  TYPEFLUX_MCP_HTTP_TOKEN=$TEAM_TOKEN \
  TYPEFLUX_MCP_HTTP_HOST=0.0.0.0 TYPEFLUX_MCP_HTTP_PORT=8765 \
  TYPEFLUX_CP_URL=http://control-plane:8080 TYPEFLUX_CP_TOKEN=$CP_TOKEN \
  npx -y typeflux-mcp
  # listens on http://0.0.0.0:8765/mcp — POST to initialize, then GET (SSE) / DELETE by session id.
  ```

  A client points at the endpoint and presents the same bearer token, e.g. Claude Code:

  ```json
  {
    "mcpServers": {
      "typeflux": {
        "url": "http://your-host:8765/mcp",
        "headers": { "Authorization": "Bearer <TEAM_TOKEN>" }
      }
    }
  }
  ```

## What it exposes

### Static resources (always available, even without a control plane)

- `typeflux://guide/authoring-checklist` — how to write a correct Typeflux AI activity.
- `typeflux://guide/project-layout` — project setup: authoring modes, file layout, manifest
  growth, and the `engine.lock` pinning convention (#865/#866; the full narrative is
  `docs/adoption.md`).
- `typeflux://schema/typeflux-yaml`, `typeflux://schema/project` — JSON Schema for the workflow YAML
  and the project manifest (generated from the authoritative zod specs; see below).
- `typeflux://docs/{slug}` — core docs: `concepts`, `yaml`, `code-defined-workflows`,
  `control-plane`, `control-plane-auth`, `provider-portability`, `observability`.
- `typeflux://docs/typescript/{slug}` — the TypeScript-edition doc mirror (#862): `tutorial`,
  `concepts`, `yaml`, `code-defined-workflows`, `observability`, `privacy`, `provider-portability`.
- `typeflux://examples/{name}` — distilled example projects (Python edition).
- `typeflux://examples/typescript/{name}` — distilled TS-edition examples (#863), including the
  governance ones with their `policies/*.yaml` inline.

### Live control-plane resources (`application/json`)

Every live URI carries a `{project}` dimension (design §3.3/§5.2): use `default` for the default
project (e.g. `typeflux://default/meta`), or a specific project id to scope.

Collections: `typeflux://{project}/{meta,workflows,environments,policies,profiles,deployments}` and
`typeflux://projects`. Parameterized: `.../environments/{id}`, `.../policies/{id}`,
`.../profiles/{kind}/{id}`, `.../validate`, and per-workflow
`.../workflows/{id}/{bundle,topology,catalog,connections,prompt-status,versions,workers,executions,status,correlation}`
(the workflow reads take `?environment_id=`; status/correlation additionally take `&execution_id=`).

Optional, richer scoping the contract supports (candidate `policy_id`s, a `deployment_image`, a
`task_queue`, a `limit`) lives on the **tools**, not the resource URIs — the MCP SDK's URI-template
matcher can't express optional query params, and the design puts "validate/resolve *with these
inputs*" on the callable tools anyway (§6.1).

### Read tools (`readOnlyHint`, `openWorldHint: false`)

`list_workflows`, `list_environments`, `list_policies`, `list_annotations`, `list_projects`,
`validate_project(environment_id?, workflow_id[]?, policy_id[]?)`,
`get_bundle(workflow_id, environment_id, policy_id[]?, deployment_image?)`, `get_catalog`,
`get_prompt_status`, `get_connections`, `get_workers(…, task_queue?)`,
`list_executions(…, limit?)`, `get_correlation`, `get_versions`, `list_deployments`,
`get_deployment`, `list_enforcement_events(environment_id, …)`, `get_github_provenance`.

Every tool returns structured content on success, and maps a control-plane failure to a
**structured** error carrying the HTTP status and the contract's code — never flattened prose:
`Unauthorized` (403), `NotFound` (404), `LifecycleBindingError` (409), `InvalidRequest` (422),
`UnsupportedRuntime` (501), `TemporalUnavailable` (503). (The structured error rides in the tool
result's `content` as JSON; `structuredContent` is reserved for the success shape.)

> Topology has no endpoint of its own — it is projected from `bundle.topology`.

### Operate tools (gated + confirmation-hinted)

`start_workflow` (PREVIEW-THEN-COMMIT: call without `expected_policy_hash` to preview the resolved
policy hash + validate input, then re-call with it to commit — input is elicited if the client
supports it, and a drifted policy is rejected before any Temporal connection), `get_status` (an
inspect read, `trace=false`; subscribe to the status resource to watch a run without hot-looping),
`submit_review`, `cancel_workflow` (`destructiveHint`), `repin_operations` / `refresh_project`
(`idempotentHint`). Each write carries `readOnlyHint: false` for client confirmation and is **removed**
when `/meta.capabilities` says the token can't perform it.

### Local authoring aids (`readOnlyHint`; workspace, not control plane)

These make an agent productive at the keyboard **without crossing the authoring boundary** — they
_return content_ for the editor to apply as a reviewable diff and **never write files** (design §6.4,
§11 decision 3). They stay available even with no control plane (a scaffold is pure-local; `doctor`
is most useful precisely when the live tier is down).

- `scaffold_ai_activity(name, input_fields, output_fields, prompt_ref?, style?, target_file?)` —
  returns generated Python (`@ai_activity.defn` or `AIActivity(...)`), TypeScript
  (`defineActivity` + zod strictObject schemas), or a hookless YAML
  `activities.definitions` block, **matched to the target file's existing style** (it READS
  `target_file` via MCP roots to match — never writes it) or to an explicit `style`.
- `scaffold_workflow_yaml(project, name, task_queue?, provider?, model?)` — returns a seeded
  `typeflux.yaml` with runtime/registry/provider stubs, `${ENV}` interpolation, and a
  secret-**reference** `api_key` (never a literal).
- `scaffold_project_entry(name, path?, environment_id?, policy_id?)` — returns a
  `typeflux.project.yaml` workflow entry (and optional environment/policy bindings) to merge into the
  manifest.
- `doctor(required_env?, workflow_id?, environment_id?, project?)` — the "why won't it run" first
  stop: a structured readiness checklist (required env keys present — **presence only, values are
  never shown**; control plane reachable; provider/registry keys set; and, with `workflow_id` +
  `environment_id`, Temporal/registry reachability). Reads only — writes nothing; degrades honestly.

Each scaffold returns `{ style, language, content, wrote_file: false, ... }` in `structuredContent`,
with the generated `content` fenced in the text so a caller-supplied token can't break out of the
block.

### Prompts — the `/typeflux:*` recipes

User-invokable slash commands that compose the resources + tools into a vetted path:

- `typeflux:scaffold-activity`, `typeflux:add-workflow` — authoring recipes that ground you in the
  checklist/schema/examples, **compose the `scaffold_*` tools** to emit starter content, and have
  **you** apply it as a reviewable diff, then `validate_project`. The scaffold tools return content —
  they never write files. `typeflux:port-to-ts-edition` drafts the TS-edition equivalent as a diff.
- `typeflux:diagnose-run` — pulls a run's live status / connections / workers / prompt-status and asks
  for a ranked cause list.
- `typeflux:review-gate` — surfaces an open review gate + its version-valid decisions and routes to a
  confirmed `submit_review`.
- `typeflux:prepare-deployment` — validates, checks plan drift, and shows the copyable promote
  command; it **never promotes**.

### Completions

The opaque IDs — `workflow_id`, `environment_id`, `policy_id`, `project`, deployment `plan_id` —
autocomplete from the live project's list endpoints, on **both** prompt arguments and resource-template
URI variables, scoped to the `project` you've already picked. A degraded backend completes nothing
(static discovery has no dynamic IDs).

> Trace search/list/inspect/diff/export have **no control-plane surface yet** and stay deferred: the
> OpenAPI contract exposes no trace/observability route, and the MCP server's only backing is that
> contract. `diagnose-run` composes the trace-less signals and notes the `trace_diff` gap.

### Capability gating

At startup the server reads `/meta.capabilities`. If the caller lacks `inspect` (a `403` on
`/meta` — the API has no `401`), the live tools/resources are withdrawn and only the static
discovery surface (docs / examples / schema / guide / recipes) remains — and completions return
nothing. Individual operate tools are also removed when their specific capability is absent.

## JSON Schemas — generation & freshness

`typeflux://schema/*` is **generated** from the authoritative zod specs in
`packages/typescript/temporal-yaml` via Zod 4's `z.toJSONSchema` (`npm run generate:schemas`) and
committed under `src/schema/`. This keeps the agent-facing schema faithful with no Python invocation
and no hand-maintained copy. **Freshness caveat:** the committed JSON is regenerated by the `prebuild`
hook and shipped as a static asset (so `npx` needs no monorepo checkout); a spec change that skips
regeneration is caught by `test/schema.test.ts`, which diffs a fresh generation against the committed
files.

## Packaging

The build (`npm run build` → `scripts/bundle.mjs`) uses **esbuild with code-splitting** to produce a
self-contained `dist/`:

- The unpublishable deps — `@gibli-labs/control-plane-client` (GitHub Packages) and
  `@typeflux/temporal-controlplane` (a `workspace:*` package) — are **inlined**, so the packed
  tarball carries no `file:`/`workspace:` deps and a public `npx typeflux-mcp` installs clean.
- Splitting is what keeps **attach mode** light: `managed-local.ts` reaches the in-process control
  plane only via a dynamic `import()`, so esbuild emits it as a separate chunk that the bin never
  statically references — attach mode (`TYPEFLUX_CP_URL`) never loads the Temporal machinery.
- The managed-local chunk transitively needs the Temporal worker (native `@swc/core`), so
  `@temporalio/worker` is the one runtime `dependency` (public npm, installed by `npx`). The
  operate-tier client and provider SDKs are dynamic and off the read path, so they stay external.

`npm pack` → the tarball's `dependencies` is just `@temporalio/worker`; `dist/index.js` is the bin.
(The Streamable-HTTP transport reuses the MCP SDK — already bundled — so it adds no new runtime
dependency; `yaml`, used only to validate scaffolds in tests, is a devDependency and is not shipped.)

## Status

**#326 is complete** — the phased plan (0: read tier → 1: operate tier → 2: recipes/completions → 3:
authoring aids + Streamable HTTP) has all landed, and this is the full server surface. The one
remaining design item, the observability `trace_*` tools (search/inspect/diff/export), stays deferred
as a **separate follow-up**: the OpenAPI contract exposes no trace/observability route, and the MCP
server's only backing is that contract — so unblocking it needs a control-plane trace surface first,
not more MCP work. `/typeflux:diagnose-run` composes the trace-less signals and notes the gap.

## Development

This package is **not** in the pnpm workspace (the other `clients/*` use npm). Locally it consumes
`@gibli-labs/control-plane-client` via a `file:` devDependency link to `clients/typescript`, and the
in-process control plane (`@typeflux/temporal-controlplane`) via an esbuild/vitest alias to the
monorepo build.

```bash
# from the repo root: build the TS packages the MCP server consumes
pnpm -r --filter "./packages/typescript/**" build
(cd clients/typescript && npm install && npm run build)

# then, in clients/mcp
npm install
npm run build        # regenerate schemas + bundle docs/examples, typecheck, then esbuild-bundle
npm test             # unit + conformance + a real in-process end-to-end
npm run smoke:attach # spawn the built bin in attach mode against a stub control plane
```
