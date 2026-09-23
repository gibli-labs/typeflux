# Control-Plane Authentication & Authorization

The control-plane API and console go beyond read-only inspection: they can start
workflows, submit review decisions, request cancellation, and refresh
Git-sourced projects. This document is the authorization boundary for those
operations — what Typeflux ships, how to configure it, and the recommended
pattern per deployment.

## Decision: a pluggable boundary, open by default

Typeflux ships **a built-in authorization layer plus a reverse-proxy integration
point**, not an identity provider. The boundary is **open by default**: the
single-binary server binds `127.0.0.1` and trusts its caller, so nothing changes
for local development. Authorization is enforced only once you configure it.

> Exposing the control-plane beyond a trusted operator network **requires**
> configuring auth (token or proxy). An unconfigured server grants every
> operation to every caller.

The resolved caller — the **actor** — is used only for authorization and for the
console capability report. It is **never** written to `typeflux.*` execution
metadata or the lifecycle audit. Reviewer identity, notes, and cancellation
reasons are kept out of `typeflux.*` execution metadata, but they are **not**
client-side: they are sent as Temporal signal payloads and persist in workflow
history, and the cancel reason is returned to any `inspect` caller via status —
so avoid sensitive free text there (#325).

## Permissions

Operations are gated by a small set of permission categories, not by endpoint:

| Permission | Grants |
| --- | --- |
| `inspect` | Every read endpoint (meta, workflows, bundles, catalog, status, executions, deployments, drain, workers, project listing, promotion-command visibility) |
| `start` | `POST /workflows/{id}/start` |
| `review` | `POST /workflows/{id}/review` |
| `cancel` | `POST /workflows/{id}/cancel` |
| `project.refresh` | `POST /projects/{id}/refresh` |

`*` (or `all`) grants every permission. A caller with no permissions is denied
everything, including reads (fail closed). Holding any *operate* permission
(`start` / `review` / `cancel` / `project.refresh`) **implies `inspect`** — you
cannot operate on what you cannot see — so an operator token need not list
`inspect` explicitly.

Plan **generation** is a console-only assembly step with no API call, so it has
no permission; the generated `deploy` command's visibility rides on `inspect`.

### Traced status is not a read

`GET /workflows/{id}/status` is an `inspect` read for polling (`trace=false`,
the default). Passing **`trace=true`** records a first-class, auditable lifecycle
operation in the trace — that is a write, so it requires an *operate-class*
permission (`start` / `review` / `cancel` / `project.refresh`). An `inspect`-only
caller that requests `trace=true` is refused with `403` and a message naming the
requirement; the API never silently downgrades the request to `trace=false`
("read-only means read-only" — an inspect token must not author audit events).

## Authorization matrix

| Method & path | Required permission |
| --- | --- |
| `GET /api/v1/meta` | `inspect` |
| `GET /api/v1/workflows`, `/environments`, `/policies`, `/profiles`, `/validate` | `inspect` |
| `GET /api/v1/workflows/{id}/…` (bundle, catalog, status, executions, correlation, connections, prompt-status, workers, versions) | `inspect` |
| `GET /api/v1/workflows/{id}/status?trace=true` | any operate-class (`start`/`review`/`cancel`/`project.refresh`) — records an audited lifecycle op |
| `GET /api/v1/deployments`, `/deployments/{id}` | `inspect` |
| `GET /api/v1/projects` | `inspect` |
| `POST /api/v1/workflows/{id}/start` | `start` |
| `POST /api/v1/workflows/{id}/review` | `review` |
| `POST /api/v1/workflows/{id}/cancel` | `cancel` |
| `POST /api/v1/projects/{id}/refresh` | `project.refresh` |
| `POST /api/v1/workflows/{id}/repin` | `project.refresh` (operator runtime maintenance — drops the pinned operations runtime) |
| `POST /api/v1/workflows/{id}/migrate` | `start` **and** `cancel` together — the API's only compound requirement (migrate terminates the running execution and resubmits it, so it is exactly a cancel plus a start) |

Every route also carries the router-level `inspect` baseline; unlisted `GET`
routes (annotations, enforcement events, GitHub provenance) require `inspect`
only.

A denied request returns `403` with the standard error envelope
`{"error": "Forbidden", "message": "operation requires the '<perm>' permission"}`.

## Console behavior

`GET /api/v1/meta` returns a `capabilities` object derived from the actor:

```json
{ "can_start": true, "can_review": true, "can_cancel": true, "can_refresh_project": false }
```

The console reads it on load and disables the start, review, cancel, and refresh
controls the actor cannot use, with a short "your access does not permit …"
hint. The API still fail-closes independently, so a hidden control is defense in
depth, not the boundary.

## Deployment patterns

### Local development — open (default)

```bash
typeflux-controlplane serve typeflux.project.yaml
```

Binds `127.0.0.1`, trusts the caller, grants everything. The console's actions
all work. Do not bind this to a non-loopback interface.

### Internal operator console — built-in tokens

Grant bearer tokens a permission set. A read-only token for analysts, an
operator token for the on-call:

```bash
typeflux-controlplane serve typeflux.project.yaml \
  --auth-token "analyst:inspect:$ANALYST_TOKEN" \
  --auth-token "oncall:*:$ONCALL_TOKEN"
```

`--auth-token` is `NAME:PERMS:TOKEN` (repeatable); `--auth-token-file` reads the
same lines from a file. Callers send `Authorization: Bearer <token>`. Token
*values* are never logged — only the names identify a caller.

### Multi-tenant hosted — reverse-proxy SSO

Put an authenticating proxy (SSO/OIDC) in front and have it set the actor and
granted permissions as headers:

```bash
typeflux-controlplane serve --registry typeflux.projects.yaml \
  --trust-proxy-auth
```

The server reads `X-Typeflux-Actor` and `X-Typeflux-Permissions` (a
comma-separated permission list, or `*`). **Only enable this behind a proxy that
strips those headers from inbound client requests and sets them itself** —
otherwise a client could forge them.

## Validation

```bash
uv run --directory packages/python pytest tests/test_controlplane_auth.py tests/test_controlplane_api.py
```

The suite covers the permission parser, each authorizer, capability reporting,
and allow/deny behavior end to end (a read-only token inspects but is denied
`start`/`refresh`; an operator token passes the gate). No Temporal connection is
needed — authorization is resolved before any operation runs.

## Out of scope

User and credential storage, sessions, and SSO protocol handling live behind the
proxy integration point. Typeflux ships the boundary and the token option, not
an identity provider.

## Related

- [Control Plane](control-plane.md) — the API surface and the console.
- [Compliance Readiness](compliance-readiness.md) — the broader posture, including
  the redaction model that keeps actor identity out of execution metadata.
