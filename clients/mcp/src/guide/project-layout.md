# Typeflux project layout & adoption guide

How to set up a project to use Typeflux effectively — the questions the authoring checklist
doesn't cover: which mode to start in, where files go, how the manifest grows, how the
engine is pinned. Full narrative: `docs/adoption.md` in the engine repo.

## Pick the authoring mode (a dial, not a fork)

- **Pure YAML** — every activity is prompt+schemas, declared in `typeflux.yaml`. Lowest
  friction, fully governable. Default for greenfield.
- **Code-first SDK** — `defineActivity`/`defineCodeActivity` (TS) or `@ai_activity.defn`
  (Python) in your own Temporal workflows. Right when the project already runs Temporal, or
  when adoption should proceed one activity at a time.
- **Hybrid** — a YAML workflow whose steps call injected code activities (`extraActivities`
  in TS, `activities.modules` in Python) alongside declared AI ones. The intended shape when
  some activities can't be a prompt; not a compromise.

Two constraints: governance inspects `activities.definitions`, so an injected activity sits
outside declarative validation (keep the MODEL CALL declarable where you can); and anything
crossing the workflow→activity boundary lands in Temporal history — fuse a read with its
model call in one code activity when the payload must never appear there.

## Layout

```
your-repo/
├── typeflux.project.yaml        # manifest at the WORKSPACE ROOT (MCP discovery finds it)
├── workflows/<name>/typeflux.yaml   # one directory per workflow, spec named typeflux.yaml
├── workflows/<name>/schemas.py|.ts  # schema source next to the spec it serves
├── environments/local.yaml      # top-level singletons, referenced by id
├── policies/base.yaml
└── .mcp.json                    # gitignore if it carries machine-specific values
```

Secrets are references (`value_from: {env|file}`) from the first commit — never literals.

## Manifest growth

Start with one workflow / one environment / one policy and a single `validation.targets`
entry. Then: keep workflow ids stable (they are the CLI/API handle); add every new workflow
to a validation target immediately — targets bind POLICIES, not visibility, so a workflow
left out of every target still validates but *ungoverned* (the failure mode is silence);
split targets only when environments or policy bindings genuinely diverge (overlapping
targets compose policies); put project-wide runtime defaults under `defaults:`.

## Pin the engine (`engine.lock`)

When the SDK ships from a sibling checkout (the TS edition today), commit an `engine.lock`
at the repo root recording: `edition`, the engine `commit` SHA, `localPath`, the package
paths, `peerVersions`, `verifiedAt`, and `gatesRunAtThisCommit` (which engine gates passed).
The rules that make it worth having:

1. **No invented engine APIs** — every symbol used must exist at the pinned SHA; a missing
   capability is a gap to file upstream, never a stub "to swap later".
2. **Verify on re-pin**: run the engine's gates AND your suite at the new SHA, then update
   `commit` + `verifiedAt` together. CI should assert imported symbols exist at the pin.
3. **Enforce the pin** — file:/link: deps consume whatever the sibling currently holds, so
   CI must compare `git -C <localPath> rev-parse HEAD` against `commit` and fail on drift.
4. **Re-pin deliberately** — a stale pin silently excludes upstream governance changes.
5. **Record deferrals next to the pin** (`deferred: [{id, issue, resolvedWhen}]`) — the
   lock is read at exactly the moment a deferral becomes actionable.

## First-session loop

Wire `.mcp.json` (`npx -y typeflux-mcp`), then: schema + examples → scaffold → apply as a
reviewable diff → `validate_project` → `get_bundle` → only then run. With no manifest the
static tier is available; creating `typeflux.project.yaml` activates the live tier for a
roots-advertising client (otherwise set `TYPEFLUX_MANIFEST` explicitly). Managed-local is
SHAPE-ONLY for schema-referencing workflows: reads that need resolved schemas answer 422
(TS, no injected resolver) or 501 (Python runtime) — attach a schema-aware control plane
(`TYPEFLUX_CP_URL`) for full `get_bundle`/catalog resolution.
