# ts-yaml-worker — reference TypeScript YAML worker image

The TypeScript twin of [`deploy/yaml-worker`](../yaml-worker). A minimal, non-root
container that composes `assembleYamlRuntime` + `createTypefluxWorker` (via
`buildRuntime`) for a Typeflux YAML spec and polls its Temporal task queue. It is the
image family the Kubernetes renderer (`typeflux-project deploy --output`) targets, and
the concrete artifact the promote runbook points at for TS-runtime projects.

## What it demonstrates

- The **preflight-marker probe convention**: the container command runs the worker
  entrypoint with `--preflight` (assemble + governance, no server), writes
  `/tmp/typeflux-preflight-ok` on success, then re-execs to poll. The startup /
  readiness / liveness probes all `test -f` that marker, so a container is never
  reported healthy until its own preflight has passed. `/tmp` is a writable `emptyDir`
  under the read-only root filesystem.
- A **non-root** security posture (`runAsNonRoot`, uid/gid 10001, dropped
  capabilities, `readOnlyRootFilesystem`, `seccompProfile: RuntimeDefault`) — the same
  posture the renderer stamps into every generated Deployment.

## The `typeflux-yaml-worker` entrypoint

The image installs `typeflux-yaml-worker` on `PATH` — the TS analog of Python's
`python -m typeflux.project run`. It has two modes:

- **Single-spec** (the default `CMD`): reads `TYPEFLUX_YAML_PATH` and serves one spec.
  Mirrors the Python reference image's `yaml.run`.
- **Project** (the rendered Kubernetes command): `typeflux-yaml-worker <manifest>
  --workflow W --environment E [--policy P]... --expect-policy-hash H` resolves the
  named workflow under its environment, composes + hash-verifies its policy, and serves
  it.

`--preflight` assembles and exits without connecting to a server. Preflight and the
live worker compose **one options assembly** (the OOTB env-keyed provider / registry /
observability auto-wiring included), so a preflight pass certifies the exact wiring the
worker polls with — a missing `OPENAI_API_KEY` fails preflight, before the readiness
marker.

The live dial resolves `runtime.temporal` fail-closed: `tls` (boolean or the structured
custom-CA/mTLS block) and `api_key` (`value_from: {env|file}`) map through the #685
machinery into `NativeConnection.connect`, so a Temporal Cloud/mTLS deployment connects
with the TLS + credential its manifest declares — a missing required secret is a loud
config error, never a silent plaintext downgrade.

### Code bindings (the TS ↔ Python divergence)

Python resolves a spec's `schemas:`/activity references via `importlib` at runtime; the
TS runtime needs **compiled code**. The entrypoint dynamic-imports an **optional**
bindings module named by `TYPEFLUX_WORKER_BINDINGS` that exports:

```js
export const schemas = { "schemas:Note": /* zod */, "schemas:Summary": /* zod */ };
// optional: provider, transports, hooks, moderators, extraActivities
```

Providers, prompt registries, and observability auto-wire from the spec + environment
(the same out-of-the-box story as `buildRuntime`), so bindings are only needed for what
must be code — the activity schema resolvers, plus hooks/moderators/extra activities and
any custom provider. With no bindings module, an activity's schema ref fails by name at
preflight, never silently.

This image ships an **offline scripted** reference under
`deploy-reference/` (`typeflux.yaml` + `bindings.mjs`) so it runs with no API key. Fork
the image and replace the spec + bindings with your project's — the bindings must live
where `import "zod"` resolves the same zod the SDK uses (bundle them alongside your own
`node_modules`).

## Build

```sh
# From the repo root (the build context is the repo root).
docker build -f deploy/ts-yaml-worker/Dockerfile -t ts-yaml-worker:dev .
```

The image is intentionally **not** built or pushed by CI (matching the Python reference
image); build and publish it from your own pipeline, pinning the base image by digest.

## Run locally

```sh
# Temporal dev server + the reference worker (offline scripted provider).
docker compose -f deploy/ts-yaml-worker/docker-compose.yml up --build
```

## Deploy

Prefer the **generated** artifacts for a project:

```sh
typeflux-project deploy typeflux.project.yaml \
  --environment prod --workflow review \
  --image ghcr.io/your-org/ts-yaml-worker@sha256:<digest> \
  --output ./out
kubectl apply -f ./out/kubernetes.yaml
# Provision Secrets from ./out/secret.scaffold.yaml via your secret manager.
```

`kubernetes.yaml` here is a hand-written single-spec reference; the renderer emits the
per-workflow ConfigMap + Deployment + Secret scaffold with the same probe / security /
naming rules. See [`docs/yaml-worker-deployment.md`](../../docs/yaml-worker-deployment.md)
for the full promote / drain runbook.
