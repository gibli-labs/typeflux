# Offline TypeScript quickstart

This example describes current source/candidate packages. Copy this directory into
a fresh Node application (Node 24.15+). Install matching candidate core and YAML
tarballs built from the checkout, plus Zod:

```sh
npm init -y
npm install /absolute/path/to/typeflux-temporal-VERSION.tgz /absolute/path/to/typeflux-yaml-VERSION.tgz zod
npx --no-install typeflux-local workflow.yaml --bindings bindings.mjs --input input.json
npx --no-install typeflux-local --project typeflux.project.yaml --workflow echo --environment offline --bindings bindings.mjs --input input.json
cat input.json | npx --no-install typeflux-local workflow.yaml --bindings bindings.mjs --input -
node local-sdk.mjs
```

Use the actual artifact filenames. Once a release containing this feature exists,
`npm install @typeflux/yaml @typeflux/temporal zod` is the registry alternative.
The historical `@typeflux/temporal` core name does not add a Temporal SDK dependency.
Each invocation prints `{"text":"local result"}`. No model credentials, vendor SDK,
observability backend, network service, Temporal worker or server is required.

`bindings.mjs` provides Zod schemas and the existing `FakeProvider`. Bindings paths
resolve from the working directory; `TYPEFLUX_WORKER_BINDINGS` is the CLI fallback.
Named exports and a default object both work. Compile TypeScript bindings to
JavaScript first. `local-sdk.mjs` loads the same YAML and awaits `buildLocalRuntime`
and `executeWorkflow`. From a checkout after [development setup](../../../../../docs/development.md),
run `node packages/typescript/yaml/examples/local-runtime/local-sdk.mjs` at its root.

## Adopt Temporal

Keep `workflow.yaml`, schemas, bindings and prompts. The manifest's included
`temporal.yaml` environment adds only engine/connection/queue placement. Install
matching Temporal integration packages and the starter's explicit dependencies:

```sh
npm install @typeflux/temporal-yaml @temporalio/client @temporalio/worker
# For an unreleased candidate use the matching integration/worker/core tarballs,
# rather than mixing candidate source with older registry packages.
temporal server start-dev
# In a second terminal in this application:
node temporal-sdk.mjs
```

The Temporal CLI is a separate prerequisite only for this optional adoption example;
a reachable configured service can be used instead. The script resolves the
`temporal` environment, serves a worker and starts a fresh run, prints the same JSON,
then closes connections. It never resumes or migrates a local invocation. Maintained
verification runs this script from a Temporal consumer with its integration/client/
worker dependencies installed, against the existing isolated server.
See the [engine comparison and migration guide](../../../../../docs/local-runtime.md#switch-an-application-to-temporal).

A new local command always starts from the beginning. Process loss can prevent
cleanup and a rerun may repeat completed effects. The packed-candidate probe executes
these local CLI/SDK commands outside the workspace, with no `@temporalio/*` dependency
and with network connections blocked.
