# Control-plane HTTP conformance suite

The executable definition of control-plane parity (epic
[#617](https://github.com/gibli-labs/typeflux-temporal/issues/617)): golden
request/response cases any control-plane server must pass while serving the
**canonical conformance fixture project**. The Python CP is the baseline that
records the goldens; every other edition (the TS server,
[#563](https://github.com/gibli-labs/typeflux-temporal/issues/563)/[#620](https://github.com/gibli-labs/typeflux-temporal/issues/620))
must reproduce them.

## Running

[`runner.py`](runner.py) is **stdlib-only** (any Python 3.11+, no installs) so
every server edition shares one harness:

```bash
# Against a running server (one auth profile at a time; default: open):
python runner.py --base-url http://127.0.0.1:8400 [--profile token]

# Spawn the server under test — {profile} and {port} are substituted per
# profile group (each profile gets its own port: a draining server can hold
# its socket past child exit, and a same-port respawn would lose the bind
# race to it); serve-python.sh is the Python edition's launcher (other
# editions provide their own, mapping the same profile names):
python runner.py --port 8411 --server-cmd './serve-python.sh {profile} {port}'

# Re-record goldens from the baseline server (deliberate contract changes only):
python runner.py --record --server-cmd './serve-python.sh {profile} {port}' --port 8411

# The TypeScript edition's lane (applies the cases' editions.ts-cp variants/skips):
python runner.py --server-cmd './serve-typescript.sh {profile} {port}' --port 8411 --edition ts-cp
```

## Editions

The shared goldens are the Python control plane's recordings. Where an edition
diverges **by design** — the mirror fixture registry (identity-flavored fields
like `/runtime` and foreign project ids invert on the TS side), capability
honesty while an operation tier is absent, dialect artifacts (#496) — the case
carries an `editions.<name>` **variant**: a mandatory `note` naming why, plus
the `request`/`response` that edition is pinned to. Where an edition genuinely
cannot pass yet, it carries a **skip** whose reason must cite a live issue.
`--record --edition <name>` records responses only into cases that already
opt in with an `editions.<name>` entry — a variant is a reviewed, per-case
decision, never an accidental fork. The suite-integrity gates on both SDKs
enforce the shape (registered edition names, why-notes, issue-pointed skips,
and that a variant never replaces the base golden).

## Auth profiles

Cases are tagged with the auth profile their server must run under
(`_suite.json` → `profiles`; untagged = `open`): `open` (all permissions),
`token` (bearer tokens with the canonical conformance grants — the
`NAME:PERMS:TOKEN` syntax is itself under test), `proxy` (trust-proxy-auth,
actor + permissions from `X-Typeflux-Actor`/`X-Typeflux-Permissions`). The
auth cases pin the normative semantics: 403-uniform failures (unauthenticated
and unknown-token answer byte-identically — validity is never disclosed),
operate-grants imply `inspect`, tolerant proxy permission parsing, and
`/meta.capabilities` reflecting exactly what the actor can do.

Exit 0 = every case passes; failures print a JSON-Pointer-addressed divergence
report (same taxonomy as the OpenAPI conformance gate). Two CI layers run the
suite: the **Control-plane conformance** job is the true black-box path (this
runner spawning a real server per auth profile; new server editions join as
matrix entries pointing at their own launcher), and the in-process pytest gate
(`packages/python/tests/test_controlplane_http_conformance.py`) replays the
same cases over `TestClient` in the Tests matrix, importing this runner's
`normalize`/`diff` so there is exactly one comparison implementation.

## Fixtures

[`fixtures/_suite.json`](fixtures/_suite.json) indexes the cases (the
suite-integrity gates on both SDKs assert the index and the files on disk
match) and holds the **normalization rules**:

- `normalize.mask_keys` — field names whose values are machine/run-specific
  everywhere they appear (absolute paths, timestamps); compared as
  `"<normalized>"`.
- per-case `mask` — JSON Pointers (with `*` wildcards) for structural cases:
  bundle git provenance (`/code`), absolute file paths inside arrays, the
  deployment-preview command string.

`spec_digest`, `plan_hash`, `policy_hash`, and `generator_version` are
deliberately **not** masked: they are stable given the pinned fixture project,
and a generator bump must fail conformance until the goldens are deliberately
re-recorded.

## The canonical fixture project

`project/python/` is the Python binding of the canonical project (workflow ids
`workflow` + `broken`, environment `local`, policy `base`, provider profile
`anthropic-prod`). The project surface — ids, shapes, policy content — is part
of the suite; each server edition provides its language's binding (#620 adds
`project/typescript/`), and language-specific values (module refs, file
paths) fall under the normalization rules above.

## Scope

Read tier + error taxonomy: meta, workflows, environments, policies, profiles,
validate, bundle, catalog, prompt-status, deployments, projects, plus one
fixture per error-taxonomy case (the `ApiError` schema description in
[`../openapi.v1.json`](../openapi.v1.json) is the normative discriminant
source). Determinism notes: the 503 case uses the fixture project's
`unreachable` environment (a connection-refused Temporal address, stable even
beside a running local dev server) and masks the `message` prose — its
wording varies by failure path; every other error message is pinned verbatim.

Deliberately excluded: `/connections` (does live connectivity probes — needs
a probe-stub decision), `repin`/`refresh` (mutating), and the Temporal-backed
operate tier — including the black-box 409 `LifecycleBindingError` case,
which requires a live foreign execution and belongs to the epic's
dev-server-backed follow-up lane (the discriminant and semantics are
contract-normative via the `ApiError` schema, and in-process tests cover the
mapping).
