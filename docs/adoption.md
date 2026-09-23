# Adopting Typeflux in an existing project

How a project — greenfield or already running Temporal — sets itself up to use Typeflux
effectively: which authoring mode to start in, where the files go, how the project manifest
grows, and how to pin the engine while the TypeScript SDK ships from a sibling checkout
(`engine.lock`). The agent-facing distillation of this page ships in the MCP server as
`typeflux://guide/project-layout` (#865).

## 1. Choose your entry point

Three authoring modes, one runtime. They compose — this is a dial, not a fork.

| Mode | What it is | Choose it when |
| --- | --- | --- |
| **Pure YAML** | `typeflux.yaml` declares prompts, activities, and the workflow graph; the YAML worker runs it. No project code in the workflow path. | Every activity is prompt+schemas. Lowest friction, fully governable, both editions. |
| **Code-first SDK** | `defineActivity` / `defineCodeActivity` (TS) or `@ai_activity.defn` (Python) descriptors, orchestrated by your own Temporal workflows. | You are already on Temporal, or activities need code (DB reads, side effects) and you want incremental adoption activity-by-activity. |
| **Hybrid** | A YAML workflow whose steps reference **injected** code activities (`extraActivities` in TS, `activities.modules` in Python) alongside declared AI activities. | You want the declarative graph + governance, but some activities can't be a prompt. This is the intended shape, not a compromise. |

Two constraints to know before choosing:

- **Governance follows declaration.** Policy checks inspect `activities.definitions`; an
  injected code activity is outside declarative bundle validation (a spec-level
  code-activity declaration is tracked upstream as #868). The model call is what policy
  most cares about — keep it declarable where you can.
- **Privacy can force fusion.** Anything crossing the workflow→activity boundary lands in
  Temporal event history. If a payload must never appear there (PII, document text), fuse
  the read and the model call inside one code activity rather than splitting them into two
  steps — see [privacy.md](privacy.md) for the codec/retention story.

## 2. Where the files go

```
your-repo/
├── typeflux.project.yaml        # the manifest — at the workspace root
├── workflows/
│   └── triage/
│       ├── typeflux.yaml        # one workflow spec per directory
│       └── schemas.py|.ts       # the spec's schema source, next to it
├── environments/
│   ├── local.yaml               # Temporal address/namespace per environment
│   └── staging.yaml
├── policies/
│   └── base.yaml                # org policy: providers, secrets, risk tiers
└── .mcp.json                    # MCP server wiring (gitignore if machine-specific)
```

The conventions that matter:

- **Manifest at the root.** MCP managed-local discovery walks the client's workspace roots
  for `typeflux.project.yaml` (root and one level down), so with a roots-advertising client
  (Claude Code among them) the live tier — `validate_project`, `get_bundle`, ID
  completions — turns on the moment the manifest exists. A client that does not advertise
  the optional MCP roots capability discovers nothing: set `TYPEFLUX_MANIFEST` to the
  manifest path explicitly in that case.
- **One directory per workflow** (`directory:` in the manifest), spec named
  `typeflux.yaml`, schemas next to the spec they serve. Single-file workflows can use
  `path:` instead — the shipped examples use both.
- **Environments and policies are top-level singletons**, referenced by id from the
  manifest. They are shared across workflows; don't nest them per-workflow.
- Secrets are **references** (`value_from: {env|file}`), never literals — in every file,
  from the first commit.

## 3. Growing the manifest

Start minimal — one workflow, one environment, one policy:

```yaml
version: "1"
name: my-project
workflows:
  - id: triage
    directory: workflows/triage
environments:
  local: environments/local.yaml
policies:
  base: policies/base.yaml
validation:
  targets:
    local:
      workflows: [triage]
      environment: local
      policies: [base]
```

As workflows multiply: keep ids stable (they're the CLI/API/console handle), and add each
new workflow to a validation target the moment it exists. Targets bind **policies**, not
visibility: `validate_project` still validates a workflow you left out of every target —
but without any target-derived policy, so it passes validation *ungoverned*. The failure
mode of a forgotten target entry is silence, not an error. Split targets when environments
or policy bindings genuinely diverge — overlapping targets compose their policies. Project-wide runtime defaults live
under `defaults:` rather than being repeated per spec.

## 4. Pinning the engine: `engine.lock`

The Python edition installs from a registry. The TypeScript edition currently ships from a
**sibling checkout** (`file:`/`link:` deps into `typeflux`), which makes "which
engine am I on?" a question your repo must answer explicitly. The convention — proven in a
real adopting project — is a committed `engine.lock` at the repo root:

```json
{
  "repository": "typeflux",
  "edition": "typescript",
  "commit": "<full SHA of the engine checkout>",
  "branch": "main",
  "verifiedAt": "YYYY-MM-DD",
  "localPath": "../typeflux",
  "packages": {
    "@typeflux/temporal": "packages/typescript/temporal",
    "@typeflux/temporal-worker": "packages/typescript/temporal-worker"
  },
  "peerVersions": { "@temporalio/worker": "1.18.1", "zod": "4.4.3" },
  "gatesRunAtThisCommit": {
    "build": "pnpm -r build",
    "typecheck": "pnpm -r typecheck (0 errors)",
    "test": "pnpm -r test (all passed)"
  }
}
```

The rules that make the pin worth having:

1. **No invented engine APIs.** Every Typeflux/Temporal symbol your project uses must
   exist at the pinned SHA. A missing capability is a gap to file upstream, never a thing
   to improvise or stub "to swap later" — placeholders reach production.
2. **Verify on re-pin, and record it.** When you move the pin: check out the new SHA in
   the sibling, run its build/typecheck/test gates, run *your* suite against it, then
   update `commit`, `verifiedAt`, and `gatesRunAtThisCommit` together. A CI step that
   asserts every engine symbol you import exists at the pinned SHA keeps the rule honest.
3. **Enforce the pin — the lock does not enforce itself.** `file:`/`link:` deps consume
   whatever the sibling checkout currently holds; if its HEAD advances, you are silently
   running a different engine while the lock still claims the old SHA. Add a build/CI
   guard that compares `git -C <localPath> rev-parse HEAD` against `commit` and fails on
   mismatch.
4. **Re-pin deliberately, not passively.** A stale pin silently excludes upstream
   behavior changes (governance semantics especially — e.g. risk-tier enforcement
   tightening). Treat "pin is N commits behind" as a maintenance item with an owner.
5. **Deferred work lives next to the pin.** If you're deliberately waiting on an engine
   capability, record it in the lock (`deferred: [{id, issue, resolvedWhen}]`) — the file
   gets read at exactly the moment the pin moves, which is when the deferral becomes
   actionable.

## 5. Wiring the MCP server

```json
{
  "mcpServers": {
    "typeflux": { "type": "stdio", "command": "npx", "args": ["-y", "typeflux-mcp"] }
  }
}
```

`.mcp.json` at the project root; approve on first session start. With no manifest you get
the static tier (schemas, docs, examples, scaffolds, `doctor`) — enough to author your
first workflow. With a manifest (discovered via roots, or `TYPEFLUX_MANIFEST` for clients
that don't advertise roots), the live validation tier activates. Managed-local is
shape-only for schema-referencing workflows — attach a schema-aware control plane
(`TYPEFLUX_CP_URL`) when `get_bundle`/catalog must fully resolve. Gitignore
the file when it carries machine-specific values (an absolute `TYPEFLUX_MANIFEST`, an HTTP
token); commit it when it's the portable `npx` form and the team should share it.

Then work the loop the server teaches: **schema + examples → scaffold → apply as a
reviewable diff → `validate_project` → `get_bundle` → only then run.**
