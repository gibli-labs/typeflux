# Control-plane interface contract

[`openapi.v1.json`](openapi.v1.json) is the **normative** OpenAPI 3.1 document for
the Typeflux control-plane HTTP API (epic
[#616](https://github.com/gibli-labs/typeflux-temporal/issues/616)). Servers
*conform to it*; clients are *generated from it*. It is hand-governed: the
document is the interface, not a build artifact.

This is an **interface contract** — the second contract class next to the
golden-reproduction [areas](../README.md): a surface document rather than a
fixture any SDK reproduces. It is registered under `interfaces` in
[`conformance.json`](../conformance.json), and the suite-integrity gates on both
SDKs assert it exists, parses, and stays indexed.

## Change process

A change to this document is an **interface change** and is reviewed as one —
never as regen noise. A contract PR must:

1. Edit `openapi.v1.json` deliberately (the diff is the review surface).
2. Land the conforming server change in the same PR. The conformance check
   (`python -m typeflux.controlplane conformance`, a dedicated CI
   step plus the pytest gate) asserts the FastAPI-emitted schema matches this
   document and fails with a structured path-by-path divergence report — a
   contract change Python does not implement, or a server surface the
   contract does not describe, is named line by line. The document must also
   stay in canonical rendering (`json.dumps` with `indent=2, sort_keys=True`)
   so contract diffs stay reviewable.
3. Regenerate the TypeScript client: `npm run generate` in
   [`clients/typescript`](../../clients/typescript) (the Web CI job fails on
   drift between this document and `src/schema.ts`), and bump the client
   `version` in the same PR.
4. After merge, release the client (the **Release control-plane client**
   workflow), then open the console bump PR updating the pinned
   `@typeflux/control-plane-client` version in
   [`clients/console`](../../clients/console). The console consumes releases
   only — it never sees in-PR client changes.

Versioning: the `v1` in the filename and the document's `info.version` are the
API major version. Breaking the surface means a new `openapi.v2.json`, not an
in-place rewrite — and a matching major bump of
`@typeflux/control-plane-client`, whose major is locked to the contract's
(`scripts/assert_client_contract_lockstep.mjs`, enforced in the Web CI job and
again at release time). The document also rides the contracts bundle
(`CONTRACT_VERSION`) like every other contract here.

## Conformers

| Conformer | How | Status |
|---|---|---|
| Python control-plane server (`typeflux.controlplane`) | emitted schema must match this document (`conformance` CLI + CI step + pytest gate, structured diff on divergence); passes the HTTP conformance suite (black-box CI matrix job + in-process pytest gate) | enforced |
| TypeScript client (`@typeflux/control-plane-client`) | `src/schema.ts` generated from this document, drift-gated in Web CI | enforced |
| TypeScript control-plane server | conformance suite entry | pending [#563](https://github.com/gibli-labs/typeflux-temporal/issues/563) / [#620](https://github.com/gibli-labs/typeflux-temporal/issues/620) |

The HTTP-level executable conformance suite (golden request/response fixtures
any control plane must pass, epic
[#617](https://github.com/gibli-labs/typeflux-temporal/issues/617)) lives in
[`conformance/`](conformance/): a stdlib-only black-box runner, the golden
cases under `conformance/fixtures/`, and the canonical fixture project each
server edition provides a binding for.

The **error taxonomy is contract-normative**: the `ApiError` schema
description **in the document is the single normative source** for the
`error` discriminants and their status codes; messages are prose and must
never embed server-implementation source locations. Each taxonomy case is
pinned by a conformance fixture (the 409 lives in the operate-tier follow-up
lane).
