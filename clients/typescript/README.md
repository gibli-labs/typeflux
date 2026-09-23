# @typeflux/control-plane-client

Typed TypeScript client for the Typeflux control-plane HTTP API.

Every contract type in [`src/schema.ts`](src/schema.ts) is **generated** from
the normative OpenAPI contract
([`contracts/controlplane/openapi.v1.json`](../../contracts/controlplane/openapi.v1.json),
#616). Servers conform to that document (a Python gate asserts the emitted
schema matches it), and CI regenerates the client and fails on any diff, so the
TS types cannot drift from the contract. Never hand-edit `src/schema.ts`;
[`src/index.ts`](src/index.ts) is glue only and declares no contract types.

## Usage

```ts
import { createControlPlaneClient } from "@typeflux/control-plane-client";

const client = createControlPlaneClient({ baseUrl: "http://127.0.0.1:8400" });

// Read tier — bundle (with the nodes+edges topology projection), catalog,
// validation, discovery:
const { data: bundle } = await client.GET("/api/v1/workflows/{workflow_id}/bundle", {
  params: { path: { workflow_id: "workflow" }, query: { environment_id: "local" } },
});
bundle?.topology?.nodes; // render the workflow DAG read-only

// Catalog JSON Schemas are plain JSON Schema objects — usable with ajv for
// form rendering/validation:
const { data: catalog } = await client.GET("/api/v1/workflows/{workflow_id}/catalog", {
  params: { path: { workflow_id: "workflow" }, query: { environment_id: "local" } },
});
catalog?.activities[0]?.input_schema.json_schema;

// Operations tier — versions/drain, start, status, review, cancel:
const { data: drain } = await client.GET("/api/v1/workflows/{workflow_id}/versions", {
  params: { path: { workflow_id: "workflow" }, query: { environment_id: "local" } },
});
drain?.running; // running executions per versioned workflow type
drain?.drained; // safe-to-decommission flag

const { data: status } = await client.GET("/api/v1/workflows/{workflow_id}/status", {
  params: {
    path: { workflow_id: "workflow" },
    query: { environment_id: "local", execution_id: "case-1" },
  },
});
status?.valid_user_decisions; // per-execution-version review decisions
```

Versioned workflow types, spec digests, per-version drain counts, and
per-execution valid review decisions ride through verbatim — the client adds
no version logic of its own.

## Development

```bash
npm ci
npm run generate   # regenerate src/schema.ts from the normative contract
npm run build      # type-check and emit dist/
```

After a contract change (`contracts/controlplane/openapi.v1.json` and the
conforming Python change land together — see
[`contracts/controlplane/README.md`](../../contracts/controlplane/README.md)),
run `npm run generate` here, bump `version` in `package.json`, and commit the
regenerated client with it.

## Releases (#616)

The package publishes to **public npmjs.org** as
`@typeflux/control-plane-client` (#893 — no credentials needed to install). The **major version is locked to the
contract's API major** (`info.version` in `openapi.v1.json`) — CI enforces the
lockstep on every PR and again at release time. Breaking the API surface means
a new `openapi.v2.json` and a `2.0.0` client, never an in-place rewrite.

To release: bump `version` here in the same PR as the contract change, merge,
then run the **Release control-plane client** workflow (dispatch with the
version, or push a `client-v<version>` tag). The workflow regenerates from the
contract, fails closed on drift or version mismatch, publishes, and pushes the
tag.

To install outside the monorepo:

```bash
npm install @typeflux/control-plane-client
```

(Historical `@gibli-labs/control-plane-client` versions remain on GitHub
Packages as a frozen lineage; new releases publish only to npmjs.org.)
