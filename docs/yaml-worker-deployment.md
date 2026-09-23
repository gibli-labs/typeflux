# YAML Worker Deployment

This guide covers the production shape for running a Typeflux YAML worker as a
long-running process. It focuses on worker operations: packaging, environment
configuration, scaling, retry/backpressure tuning, shutdown, and debugging.

## Worker Shape

The YAML worker listens on a Temporal task queue and executes workflows and
activities defined by a `typeflux.yaml` file:

```bash
uv run python -m typeflux.yaml.run path/to/typeflux.yaml
```

Run the command from `packages/python/` during development. In production, run it in
a container or process supervisor with the package installed and the YAML file
available on disk.

Workflow starters are separate from workers. Workers should stay up
continuously and poll the task queue. API servers, jobs, or CLIs should use the
Typeflux starter path when they need complete Typeflux observability:

```bash
uv run python -m typeflux.yaml.submit path/to/typeflux.yaml \
  --input input.json \
  --workflow-id workflow-123
```

Python services can use `TypefluxYamlRuntime.execute_workflow(...)` for the
same full root trace path. Raw Temporal starts remain valid, but they only
guarantee worker-owned activity/generation observations with correlation fields
such as workflow ID, run ID, task queue, workflow type, activity type, and
activity ID.

YAML authors do not choose whether an activity step is async. The worker
automatically uses a provider's `async_structured_call` capability when the
provider exposes one, and otherwise keeps synchronous provider calls isolated in
worker threads. Tune throughput with Temporal worker concurrency, YAML map
`concurrency`, and provider/provider-model limits rather than adding async
fields to workflow specs.

Langfuse trace egress is controlled by YAML, not ambient credentials. Production
workers only send traces when the deployed YAML explicitly contains:

```yaml
runtime:
  observability:
    type: langfuse
```

`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` may be present in the process
environment for prompt registry or future operator use, but they do not enable
YAML observability unless the YAML opts in. Keep this field reviewed the same
way you review provider and registry configuration in regulated deployments.

## Preflight

Before deploying a new worker image or YAML file, resolve and validate prompts:

```bash
uv run --directory packages/python python -m typeflux.yaml.run path/to/typeflux.yaml --preflight
```

Preflight catches YAML/import errors and prompt resolution failures before the
worker starts polling.

## Import Boundary

Treat `typeflux.yaml` as trusted operator code. Loading a YAML worker can import
Python activity modules, schema types, and provider classes, and import-time
Python code may run before the worker starts polling.

The safest production posture is to keep imports under the declared `project`
package:

```yaml
project: my_claim_app

activities:
  modules:
    - activities
```

Use explicit import policy for reviewed exceptions:

```yaml
runtime:
  imports:
    allow_absolute_activity_modules: true
    allow_provider_class: true
    allowed_module_roots:
      - shared_ai
```

Review any use of `activities.modules[].absolute: true` or
`runtime.provider.class` the same way you review application code. This guardrail
does not sandbox untrusted YAML. Hosted or multi-tenant systems need separate
isolation before accepting user-authored specs.

## Reference Container

The repo includes a reference image at `deploy/yaml-worker/Dockerfile`.

Build it from the repo root:

```bash
docker build -f deploy/yaml-worker/Dockerfile -t typeflux-yaml-worker:local .
```

Run it against local Temporal:

```bash
docker run --rm \
  -e TYPEFLUX_YAML_PATH=/app/examples/lifecycle_review/typeflux.yaml \
  -e TEMPORAL_ADDRESS=host.docker.internal:7233 \
  -e TEMPORAL_TLS=false \
  typeflux-yaml-worker:local
```

Use `host.docker.internal:7233` when the worker container connects to Temporal
running on the host. On Linux, you can instead use a Docker network, host
networking, or the host gateway pattern your platform supports.

The Dockerfile uses `uv.lock` and installs with:

```bash
uv sync --frozen --extra live --no-dev
```

At runtime, the image executes `/app/.venv/bin/python` directly instead of
`uv run` so worker startup does not re-sync dev tooling or mutate the locked
production environment.

The container command runs `--preflight` before it starts polling Temporal. If
preflight succeeds, it writes `/tmp/typeflux-preflight-ok` and then `exec`s the
long-running worker process. This makes preflight a real startup gate rather
than only a Kubernetes health observation.

The reference image also runs as a dedicated non-root `typeflux` user with
UID/GID `10001`. Keep that posture in downstream images unless your platform
requires a different non-root identity.

Pin production images by digest in your build pipeline. The reference Dockerfile
uses explicit image tags to stay readable, but production should prefer
immutable references such as:

```dockerfile
FROM python:3.12-slim-bookworm@sha256:<digest>
COPY --from=ghcr.io/astral-sh/uv:0.11.8@sha256:<digest> /uv /uvx /usr/local/bin/
```

The `.dockerignore` excludes local `.env` files, virtualenvs, caches, git
metadata, and build outputs from the Docker context.

## Docker Compose Smoke

The compose file at `deploy/yaml-worker/docker-compose.yml` starts local
Temporal and a YAML worker using the lifecycle review demo. It uses the fake
provider, so it does not require OpenAI or Langfuse credentials.

This compose file is a local development smoke test only. It is intentionally
not a production deployment template: it runs Temporal start-dev, uses local
build context, and omits production platform controls such as network policy,
secret manager integration, rollout policy, and centralized metrics.

Start it:

```bash
docker compose -f deploy/yaml-worker/docker-compose.yml up --build
```

If another local Temporal server already uses ports `7233` or `8233`, override
only the host bindings:

```bash
TEMPORAL_HOST_PORT=17233 TEMPORAL_UI_HOST_PORT=18233 \
docker compose -f deploy/yaml-worker/docker-compose.yml up --build
```

In another shell, start a workflow through the host environment:

```bash
TEMPORAL_ADDRESS=localhost:7233 \
uv run --directory packages/python python -m examples.lifecycle_review.main start --workflow-id lifecycle-compose-demo
```

Use the overridden host port in `TEMPORAL_ADDRESS` when you changed
`TEMPORAL_HOST_PORT`.

Query status:

```bash
TEMPORAL_ADDRESS=localhost:7233 \
uv run --directory packages/python python -m examples.lifecycle_review.main status lifecycle-compose-demo
```

Submit a review decision to release the review gate. The decision must be one
of the `user_decisions` keys in the example's `typeflux.yaml`:

```bash
TEMPORAL_ADDRESS=localhost:7233 \
uv run --directory packages/python python -m examples.lifecycle_review.main review lifecycle-compose-demo send_email \
  --reviewer operator \
  --notes "Routed to email during compose smoke"
```

Stop the deployment:

```bash
docker compose -f deploy/yaml-worker/docker-compose.yml down
```

## Kubernetes Shape

The example manifest at `deploy/yaml-worker/kubernetes.yaml` shows the intended
shape:

- a `Deployment` for long-running worker replicas
- a `ConfigMap` for non-secret runtime configuration
- a `Secret` reference for API keys
- a termination grace period for graceful worker shutdown
- CPU and memory requests/limits
- non-root pod/container security contexts
- a read-only root filesystem with explicit writable `/tmp`
- startup, readiness, and liveness probes

Treat it as a starting point. In production, pin your worker image to an
immutable digest or reviewed release tag rather than `latest`.

The reference ConfigMap keeps Temporal address, namespace, task queue, and TLS
mode as non-secret runtime settings. API keys, including `TEMPORAL_API_KEY`, are
Secret material. The reference manifest defaults to `TEMPORAL_TLS: "true"`
against a Temporal Cloud-style endpoint; the plaintext in-cluster values stay
commented out and are for local development only.

Project-generated Kubernetes Deployments also inject trace-only runtime
placement metadata. Namespace, pod name, pod UID, node name, and service
account come from Kubernetes Downward API environment variables. Platform,
generated Deployment/worker name, and container image are renderer-injected
static environment variables. Typeflux records these under
`typeflux.runtime_placement` on the Langfuse trace, not in the execution
manifest, so pod restarts and reschedules do not churn manifest hashes. Labels,
annotations, mounted service account tokens, and arbitrary environment dumps are
not recorded.

For production YAML, prefer typed secret references over interpolating secret
values into the resolved spec:

```yaml
runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS}
    namespace: ${TEMPORAL_NAMESPACE}
    tls: true
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
  provider:
    type: openai
    api_key:
      value_from:
        env: OPENAI_API_KEY
```

Use Kubernetes `secretKeyRef`, External Secrets, sealed secrets, Docker
secrets, CI secret injection, or your platform sidecar/operator to materialize
those values as environment variables or mounted files. Typeflux does not fetch
from Vault, AWS Secrets Manager, GCP Secret Manager, or Azure Key Vault directly
inside the worker process; credential acquisition stays in the deployment
control plane.

Security defaults in the reference manifest:

- `runAsNonRoot: true` with UID/GID `10001`
- `allowPrivilegeEscalation: false`
- `capabilities.drop: ["ALL"]`
- `seccompProfile.type: RuntimeDefault`
- `readOnlyRootFilesystem: true`

If your YAML worker or provider SDK needs writable scratch space, mount it
explicitly. The reference manifest mounts an `emptyDir` at `/tmp`.

Health behavior:

- The container command runs YAML `--preflight` before starting the long-running
  worker. This catches YAML, import, environment interpolation, and prompt
  resolution failures before the worker polls Temporal.
- Project-generated workers pass `--expect-policy-hash` and set
  `TYPEFLUX_EXPECTED_POLICY_HASH` from the composed policy hash admitted during
  deployment generation. `project run` compares that value to the selected
  runtime policy before preflight or Temporal polling, and `project submit`
  performs the same check before starting a workflow, so policy drift fails
  closed at startup and at submission.
- `python -m typeflux.yaml.run` is the local-dev single-spec worker
  path and does not enforce project policy. It fails fast when
  `TYPEFLUX_EXPECTED_POLICY_HASH` is set so a policy-expecting deployment
  cannot silently run an unenforced worker; production workers run through
  `typeflux-project run`.
- `startupProbe` checks the `/tmp/typeflux-preflight-ok` marker written by the
  container command after preflight succeeds. This avoids running a second
  preflight in parallel and makes startup health reflect the real gate.
- `readinessProbe` and `livenessProbe` are lightweight container/config checks.
  In the reference manifest they confirm that the configured YAML file is
  readable inside the container.
- Project-generated Deployments use the same gate: the rendered worker command
  runs `project run … --preflight`, writes `/tmp/typeflux-preflight-ok`, then
  `exec`s the long-running worker, and the rendered container carries
  startup/readiness/liveness probes that check the marker. Policy-hash
  verification runs inside both the preflight pass and the worker start.
- Temporal task queue pollers, worker metrics, and workflow history are the
  source of truth for whether the worker is actually polling and completing
  work. Use these signals for alerts and rollout verification.

Avoid putting provider credentials or Langfuse keys in ConfigMaps. Use
Kubernetes Secrets, External Secrets, your cloud secret manager, or another
audited secret source.

## Project Deployment Kubernetes Smoke

Use this smoke when you want to test the project deployment renderer end to end:
render project artifacts, start a Kubernetes worker, submit a Temporal Cloud
workflow, and verify Langfuse observability.

Prerequisites:

- Docker Desktop, `minikube`, and `kubectl`
- `.env.temporal-cloud` populated with Temporal Cloud, OpenAI, and Langfuse
  credentials
- the contract prompt bootstrapped into Langfuse, if the selected environment
  uses the Langfuse prompt registry

Bootstrap the contract prompt when needed:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run --directory packages/python python -m examples.contract_risk_review.main --bootstrap-langfuse
```

Start a local Kubernetes cluster and namespace:

```bash
minikube start --driver=docker
kubectl create namespace typeflux-smoke --dry-run=client -o yaml | kubectl apply -f -
```

Build and load the worker image into minikube:

```bash
docker build -f deploy/yaml-worker/Dockerfile -t typeflux-worker:k8s-smoke .
minikube image load typeflux-worker:k8s-smoke
```

Render Kubernetes artifacts for the contract review workflow:

```bash
uv run --directory packages/python typeflux-project deploy examples/typeflux.project.yaml \
  --environment temporal_cloud_dev \
  --workflow contract_risk_review \
  --policy base \
  --image typeflux-worker:k8s-smoke \
  --allow-mutable-image \
  --output /tmp/typeflux-k8s-live-contract \
  --config-env TYPEFLUX_RUN_LIVE
```

The output directory contains:

- `deployment-plan.json`: secret-free deployment plan
- `kubernetes.yaml`: apply-safe ConfigMap and Deployment manifests
- `secret.scaffold.yaml`: blank Secret template for operators or secret-manager
  tooling
- `secrets.env.example`: checklist of required Secret keys

The Python `deploy` command carries the same #757 refinements as the TypeScript
edition (see the TypeScript Edition section for the full description):

- **Portable artifacts.** `project_manifest_path` (render annotations +
  `deployment-plan.json`) defaults to the manifest path RELATIVE to its own
  directory, so committed artifacts are machine-independent. `--project-manifest-path`
  records an explicit value; `--absolute-manifest-path` restores the resolved absolute
  path. Provenance only — never part of the plan hash.
- **Placeholder-image gate.** The all-zeros digest (`...@sha256:0000…0000`) is
  rejected at plan-write AND promote/verify time unless `--allow-placeholder-image`
  (API `allow_placeholder_image`) is passed.
- **Observability credentials.** A spec declaring `runtime.observability.type:
  langfuse|langsmith` scaffolds the backend's credential NAMES
  (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, `LANGSMITH_API_KEY`) into the Secret
  surface — names only, required when a composed policy sets `observability.required`.

Do not apply `secret.scaffold.yaml` over a populated Secret. Its values are
blank by design. Provision the generated Secret name from your secret manager,
or create a local smoke Secret with all keys listed in `secrets.env.example`.
For simple unquoted dotenv files, this shape is enough:

```bash
SECRET_NAME="$(python -c "import json; p=json.load(open('/tmp/typeflux-k8s-live-contract/deployment-plan.json')); print(p['workers'][0]['secret_name'])")"
kubectl -n typeflux-smoke create secret generic "$SECRET_NAME" \
  --from-env-file=packages/python/.env.temporal-cloud \
  --dry-run=client -o yaml | kubectl apply -f -
```

If your dotenv parser uses quotes or comments that `kubectl --from-env-file`
does not understand, either create the Secret from your platform secret manager
or generate a temporary kubectl-only env file with python-dotenv semantics:

```bash
uv run --directory packages/python python -c "from dotenv import dotenv_values; from pathlib import Path; src=dotenv_values('.env.temporal-cloud'); keys=[line.split('=', 1)[0] for line in Path('/tmp/typeflux-k8s-live-contract/secrets.env.example').read_text().splitlines() if line and not line.startswith('#') and '=' in line]; print('\n'.join(f'{key}={src[key]}' for key in keys if src.get(key) is not None))" \
  > /tmp/typeflux-k8s-live-contract/secrets.kubectl.env
kubectl -n typeflux-smoke create secret generic "$SECRET_NAME" \
  --from-env-file=/tmp/typeflux-k8s-live-contract/secrets.kubectl.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

The temporary `secrets.kubectl.env` file contains real secrets; keep it out of
git and delete it when the smoke test is done. The important invariant is that
every key referenced by `secrets.env.example` exists in the Kubernetes Secret
before the Deployment rolls out.

Apply the worker manifests and wait for rollout:

```bash
kubectl -n typeflux-smoke apply -f /tmp/typeflux-k8s-live-contract/kubernetes.yaml
DEPLOYMENT_NAME="$(kubectl -n typeflux-smoke get deploy \
  -l typeflux.io/workflow=contract-risk-review \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n typeflux-smoke rollout status "deployment/${DEPLOYMENT_NAME}" --timeout=180s
kubectl -n typeflux-smoke get pods -l typeflux.io/workflow=contract-risk-review
```

Submit the workflow from the host. The PDF path in the input must be visible to
the worker container, not just the submitter process:

```bash
cat > /tmp/contract-k8s-input.json <<'JSON'
{
  "engagement_id": "contract-risk-review-k8s-smoke",
  "business_context": "Kubernetes smoke for project deployment generation.",
  "review_objective": "Extract contract entities, clauses, risks, and recommended next steps.",
  "contract_files": [
    "/app/examples/contract_risk_review/fixtures/sample-contract.pdf"
  ]
}
JSON

uv run --directory packages/python typeflux-project submit examples/typeflux.project.yaml \
  --environment temporal_cloud_dev \
  --workflow contract_risk_review \
  --policy base \
  --input /tmp/contract-k8s-input.json \
  --workflow-id contract-risk-review-k8s-smoke-001 \
  --tag k8s-smoke
```

Verify the Langfuse trace:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run --directory packages/python typeflux-trace trace list \
  --workflow-id contract-risk-review-k8s-smoke-001 \
  --limit 1 \
  --json
```

Inspect the returned trace ID:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run --directory packages/python typeflux-trace trace inspect TRACE_ID --json
```

Success criteria:

- the Kubernetes pod is running with zero restarts
- the workflow completes in Temporal Cloud on the generated task queue
- Langfuse shows the workflow root, Temporal observations, provider/generation
  observations, execution manifest hashes, policy status, Temporal connection
  metadata, Kubernetes runtime placement metadata, and artifact provenance
- raw PDF contents, local host paths, API keys, and TLS material are absent from
  `typeflux.*` metadata
- reapplying `kubernetes.yaml` does not overwrite the populated Secret

Cleanup:

```bash
kubectl delete namespace typeflux-smoke
minikube stop
```

This minikube flow uses `--allow-mutable-image` because the image is loaded
locally. Production render jobs should use digest-pinned images and omit that
flag.

## TypeScript Edition

The TypeScript SDK ships the same deployment tier (#687): the `typeflux-project`
CLI, immutable content-hashed plan files, the Kubernetes renderer, and a reference
worker image at `deploy/ts-yaml-worker`. Behavior mirrors the Python sections above
(behavioral parity — the manifests are structurally identical, not byte-identical).

### CLI

`@typeflux/temporal-yaml` installs a `typeflux-project` bin with the same `deploy`
flag surface:

```bash
# Write an immutable, content-hashed plan file (approval = the PR that merges it).
typeflux-project deploy typeflux.project.yaml \
  --environment prod --workflow review \
  --image ghcr.io/your-org/ts-yaml-worker@sha256:<digest> \
  --plan-out deployments

# Render the Kubernetes artifacts for the plan.
typeflux-project deploy typeflux.project.yaml \
  --environment prod --workflow review \
  --image ghcr.io/your-org/ts-yaml-worker@sha256:<digest> \
  --output ./out

# Promote: verify an approved plan against the current resolution, fail closed on drift.
typeflux-project deploy typeflux.project.yaml \
  --apply deployments/review.prod.<hash>.yaml --require-merged-plan --output ./out
# --require-merged-plan (#790, or TYPEFLUX_REQUIRE_MERGED_PLAN=1): refuse unless this
# exact plan file is merged on the remote default branch — the git-native proof that
# the reviewed, PR-approved artifact is what is being promoted. Recommended in CI.
```

`--output` writes the same four artifacts as Python: `deployment-plan.json`,
`kubernetes.yaml` (apply-safe ConfigMap + Deployment, no Secret manifests),
`secret.scaffold.yaml`, and `secrets.env.example`. The rendered Deployment carries
the identical non-root security context, the `/tmp/typeflux-preflight-ok`
startup/readiness/liveness probes, the Downward-API placement env, and
RFC-1123-sanitized names/labels. `--project-path-in-image` overrides the in-image
manifest path the rendered worker command targets. `--allow-mutable-image`,
`--allow-shared-task-queue`, `--config-env`, and `--policy` behave as in Python.

**Portable artifacts (#757).** By default the recorded `project_manifest_path`
(surfaced in the render annotations `typeflux.io/project-manifest-path` and in
`deployment-plan.json`) is normalized RELATIVE to the manifest's own directory, so
committed artifacts are machine-independent. `--project-manifest-path <path>` records
an explicit (repo-relative) value; `--absolute-manifest-path` restores the operator's
resolved absolute path. This path is provenance only — it is never part of the plan
hash or drift detection, so the relative default is a safe behavior change.

**Placeholder-image promote gate (#757).** The well-known all-zeros digest
(`...@sha256:0000…0000`) is FORMAT-valid, so the digest-pin check passes — but it
resolves to no published image and only fails at pod scheduling (ImagePullBackOff),
after the plan/PR/promote chain has approved it. It is now rejected at plan-write AND
promote/verify time unless `--allow-placeholder-image` (API `allowPlaceholderImage`)
is passed; the error names the digest and the flag. At verify time the refusal is its
own synthetic check entry (`deployment.image_placeholder`): the literal image rides
`plan_value` and the explanation rides `current_value`, so literal-diff renderers over
the sibling mismatch paths stay coherent.

**Closure observability consistency at plan build (#757 review).** A composed worker
builds ONE observer — the parent's — so a closure whose child declares a different
real backend (parent `langfuse` + child `langsmith`) now fails plan BUILD and
plan-write with the same `ObservabilityCompositionError` the worker boot raises (#756),
instead of minting an approvable plan that crash-loops at pod boot. A child declaring
`none` (or nothing) inherits the parent's observer; the credential scaffolding above
keys off that verified single effective backend. Both editions.

**Observability credentials in the render (#757).** When the resolved spec declares a
tracing backend (`runtime.observability.type: langfuse` or `langsmith`), the render's
Secret surface now includes the backend's credential NAMES —
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` for langfuse, `LANGSMITH_API_KEY` for
langsmith — as names-only `secretKeyRef` entries, scaffold slots, and
`secrets.env.example` lines (never values). They are marked required when a composed
policy requires observability (`observability.required: true`, the #756 runtime gate);
otherwise optional. Without this a required-observability worker would come up healthy
and run silently untraced.

**Reusing the CLI resolver (#757).** Consumers driving `buildProjectDeploymentPlan` /
`writeDeploymentPlan` directly should import `resolveProjectWorkflow` /
`planResolverFor` from `@typeflux/temporal-yaml` rather than re-implementing resolution
from lower-level exports — the exported resolver composes component profiles in the
same order the CLI does, so a plan built from it reproduces CLI-identical digests
(a hand-rolled twin that skips profile composition silently diverges once any profile
is declared).

**Hermetic interpolation for committed renders (#760).** Both `resolveProjectWorkflow`
and `planResolverFor` take an optional final `env` argument — the base environment
`${VAR}` spec references interpolate against. It defaults to `process.env` (zero
behavior change when omitted), so a normal deploy uses the operator's shell. A consumer
that commits machine-independent artifacts passes a fixed map so the operator's shell
never leaks into the resolved bytes:

```ts
const resolve = planResolverFor(project, sources, { DEPLOY_REGION: "eu-west-1" });
```

When `env` is supplied the interpolation is **completely hermetic**: the injected map is
the only source (a `${VAR}` whose name is absent errors exactly as an unset shell
variable would — no silent `process.env` fallback; an injected `""` is *set*, not
missing), and the environment's `variables:` overlay still layers over it (overlay
wins). `planResolverFor` threads the same `env` through the sub-workflow closure, so a
parent's plan digest is computed hermetically across its whole transitive closure —
hand that resolver to `writeDeploymentPlan` / `verifyDeploymentPlan` for hermetic plan
authoring and promotion. This replaces the fragile swap-`process.env`-around-each-call
wrapper (`withHermeticResolutionEnv`) a consumer would otherwise maintain — no global
mutation, no sync-only assumption.

Python parity: `resolve_project_workflow(..., base_env=<mapping>)` retains the base on
the resolved artifact (sub-workflow and closure-admission walks reuse it), and the plan
writer is wired end-to-end — `write_deployment_plan(..., base_env=...)` /
`verify_deployment_plan(..., base_env=...)` / `build_project_deployment_plan(...,
base_env=...)` compute and reproduce spec digests against the injected mapping. No CLI
flag exposes the seam yet in either edition; when a CLI consumer materializes it would
attach to the plan/deploy verbs (e.g. `--base-env-file`).

### Reference image + entrypoint

`deploy/ts-yaml-worker` is the TS twin of `deploy/yaml-worker`. It installs a
`typeflux-yaml-worker` bin — the TS analog of `typeflux-project
run`, and the entrypoint the rendered Deployment's container command invokes:

```bash
docker build -f deploy/ts-yaml-worker/Dockerfile -t ts-yaml-worker:local .
docker compose -f deploy/ts-yaml-worker/docker-compose.yml up --build   # local smoke
```

Like the Python image, the container command runs `--preflight`, writes
`/tmp/typeflux-preflight-ok`, then `exec`s the worker, so the probes gate on the
same preflight. It runs as a non-root `typeflux` user (uid/gid `10001`). It is
intentionally **not** built or pushed by CI (matching the Python reference image).

The worker's preflight and its live build compose one options assembly — the
out-of-the-box env-keyed provider/registry/observability wiring included — so a
passed preflight certifies the exact wiring the worker polls with (a missing
`OPENAI_API_KEY` fails preflight, before the readiness marker). The live dial
resolves `runtime.temporal` fail-closed: `tls` (boolean or the structured
custom-CA/mTLS block) and `api_key` secret references map through the same #685
machinery the control plane uses, so a Temporal Cloud/mTLS deployment connects
with the posture its manifest declares — never a silent plaintext downgrade.

**Code bindings (the TS ↔ Python divergence).** Python resolves a spec's
`schemas:`/activity references via importlib at runtime; the TS runtime needs
COMPILED code. `typeflux-yaml-worker` dynamic-imports an OPTIONAL bindings module
named by `TYPEFLUX_WORKER_BINDINGS` that exports `{ schemas, provider?,
transports?, hooks?, moderators?, extraActivities? }` — required only for what
must be code (schema resolvers, hooks, custom providers); vendor providers and
registries auto-wire from the environment. The reference image ships an offline
scripted example under `deploy-reference/`; a production image forks it with the
project's compiled bindings. See `deploy/ts-yaml-worker/README.md`.

### Edition-honest plan identity

The version-promotion and drain runbook below is edition-neutral, with one identity
substitution: the TS plan's `workflow_type` is the CONSTANT generic type
(`typefluxYamlWorkflow`), so drift detection and the drain gate ride the
`spec_digest` (`workflowPlanDigest`) + composed policy hash rather than a
version-suffixed workflow type (D687-1). `typeflux-project deploy --apply` verifies
those and fails closed on drift — edit the spec and the same promote command names
the `identity.spec_digest` mismatch.

## Changing Workflow YAML Safely

YAML workflow graphs are immutable versioned artifacts: executions start under
a versioned Temporal workflow type derived from the spec digest (or an
explicit frozen `workflow.version` label), so a changed graph never replays an
in-flight execution's history. Rolling out a YAML change is a version
promotion, not an in-place mutation:

1. **Promote.** Build and deploy workers for the new spec version. Old and
   new workers can share a task queue safely because their workflow types
   differ; the planner's one-queue-per-workflow default still applies.
2. **Observe.** Each run records its spec digest, generator version, and
   registered workflow type in the execution manifest, trace metadata, and a
   `typeflux_spec_digest` start memo, so you can verify which graph new
   executions are using.
3. **Drain.** Keep the previous worker deployment running until the old
   versioned type has no open executions:

   ```
   WorkflowType = 'SupportTriageYamlWorkflow.v6' AND ExecutionStatus = 'Running'
   ```

   With `runtime.temporal.workflow_search_attribute` configured (and the
   attribute registered in the namespace), one query covers every version of
   the logical workflow:

   ```
   TypefluxWorkflow = 'SupportTriageYamlWorkflow' AND ExecutionStatus = 'Running'
   ```

   The drain-status command always uses the `WorkflowType STARTS_WITH`
   prefix query for gating — executions started before a search attribute
   was enabled, or via raw `client.start_workflow`, carry no attribute and
   must still count. It reports running executions per versioned type as
   JSON and exits non-zero until every version other than the
   currently-loaded spec's type is drained, so rollout scripts can gate
   decommission on it:

   ```bash
   uv run --directory packages/python typeflux-project drain-status typeflux.project.yaml \
     --workflow support_triage --environment production
   ```

4. **Decommission.** Remove the old worker deployment once drained. For
   stragglers that cannot be waited out, decide deliberately between waiting,
   deciding an open gate, **migrating** (terminate-and-resubmit onto the new
   version — see "Migrating long-tail executions" below), or terminating
   without a resubmit, per your business policy, before decommissioning.

Roll back by redeploying the workers for the previously approved version —
its workflow type and graph are unchanged, so this is always safe. Never edit
a deployed YAML graph under a reused `workflow.version` label; worker and
starter startup rejects a label whose digest no longer matches the executions
already started under it.

Prompt content and versions, provider models and params, retries, timeouts,
observability, and policy settings are not part of the graph identity — they
roll out with a normal worker restart and do not create a new workflow
version.

## Migrating long-tail executions

Draining assumes old-version executions finish on their own. When one cannot be
waited out — a long-running case, or one parked at a review gate nobody will
decide — the supported primitive is **terminate-and-resubmit with input
carry-over**: `migrate`. It reads the running execution's original input from
its start event, terminates the old run with the reason
`typeflux migrate to <new version key>`, and starts a **new run under the same
workflow id** against the currently-resolved version, carrying the original
input. The new run's memo records `typeflux_migrated_from` (the old run id) and
`typeflux_migrated_from_version`, so the audit trail links the pair and the
control plane's correlation view can surface the provenance.

Continue-as-new handoff across graph versions is **not** offered: mapping
in-flight interpreter state (completed steps, open gates, fan-out progress) onto
an edited graph is the graph-identity problem versioning exists to prevent. A
migration is honest that the new run **starts from step zero**.

### Decision tree for a straggler

1. **Will it finish soon on its own?** → **wait it out.** Migration re-runs
   every activity; if the execution is nearly done, waiting is cheaper and
   loses no work.
2. **Is it parked at a review gate?** → **decide the gate first, then migrate**
   (or let it complete). `migrate` refuses by default when the execution has an
   open gate (a `422` naming the gate ids), because terminate-and-resubmit
   discards the pending human decision. Only pass `--abandon-gates` when you
   have accepted that loss deliberately.
3. **Does it need to run under the new graph, and is re-running its activities
   acceptable?** → **migrate.** Requires that the current resolved version
   differs from the execution's (migrating onto the same version is refused as a
   no-op) and that workers are serving the target task queue (refused
   fail-closed otherwise, so you never trade a running execution for a
   permanently pending one).
4. **Should it simply stop?** → **terminate without a resubmit**, per your
   business policy.

### Idempotency stance

Migration is **not** idempotent and does not attempt to be: the resubmitted run
re-executes **all** activities from the start. This is safe when the workflow's
activities are idempotent (or re-execution is otherwise acceptable) — for
example, activities keyed on a stable case id, or read-then-write steps guarded
by their own dedupe. If re-running an activity would double an external effect
(a payment, an email, an irreversible state change), do **not** migrate blindly;
either make the activity idempotent first, or drain/terminate instead. The
decision is deliberately the operator's — the tool does not guess.

### Review-state loss

A migrated run loses any in-progress review state: an open gate's pending
decision, and the position within a multi-gate sequence, are not carried over
(only the original workflow **input** is). This is why `migrate` refuses an
execution with an open gate unless `--abandon-gates` is given. If the human
decision matters, decide the gate on the old run first, or let it complete.

### Composition-cascade interaction

The frozen-label cascade (see "The frozen-label cascade" in `yaml.md`) makes the
long tail worse for composed workflows: a child-only graph edit moves the
parent's digest and every ancestor's, pushing each ancestor through
promote → drain → decommission. When you migrate a **parent** whose digest moved
only because a child changed, the parent restarts from step zero and re-runs its
children under their new versions too — the whole composed program re-executes.
Prefer waiting out composed executions where you can, keep composition shallow,
and reserve migration for cases where re-running the entire tree is acceptable.

### Running a migration

Control plane (both editions) — addressed exactly like cancel/review
(`execution_id` plus an optional `run_id`; omitted, the current run is
targeted):

```
POST /api/v1/workflows/{workflow_id}/migrate
     { "environment_id": "...", "execution_id": "...",
       "run_id": "... (optional)",
       "abandon_gates": false, "reason": "..." }
```

CLI (Python edition):

```bash
uv run --directory packages/python typeflux-project migrate typeflux.project.yaml \
  --workflow support_triage --environment production \
  --execution-id <workflow-id> [--run-id <run-id>] [--abandon-gates] [--reason "..."]
```

The command prints the old/new run ids and version keys and exits non-zero on a
refusal (same-version, no serving workers, an open gate without
`--abandon-gates`, or a carried-over input that no longer validates against the
current version's input model). Every such precondition — including the
frozen-`workflow.version` check and the input validation — runs BEFORE the old
run is terminated, so a refusal always leaves the execution running. After the
terminate, only two failures remain: a concurrent close of the old run (a 409
`MigrateExecutionClosedError` conflict — nothing was terminated or started) and
a failed replacement start (a distinguished `MigratePartialError` naming the
already-terminated run; the carried input is intact in its history — resubmit
via a normal start).

Build-ids are not used for any of this — see "Build-id / worker-versioning
alignment: evaluated, not adopted" in `yaml.md` for why, and the re-open
triggers.

## Deployment Hardening Checklist

- Pin base, uv, Temporal, and worker images to reviewed release tags or digests.
- Run the worker as a non-root user and keep `allowPrivilegeEscalation` disabled.
- Drop Linux capabilities and use `RuntimeDefault` seccomp.
- Keep the root filesystem read-only and mount only the scratch paths the worker
  needs.
- Run `--preflight` before accepting a rollout.
- Keep secrets out of images and ConfigMaps.
- Alert on Temporal task queue pollers, activity failure rates, retry rates,
  workflow backlog, and worker process restarts.
- Use separate task queues for tenant, compliance, environment, or provider
  budget isolation.

## Environment Variables

The YAML runtime only consumes environment variables that the YAML file
interpolates. The reference examples include `runtime.temporal.address`,
`runtime.temporal.namespace`, `runtime.temporal.tls`, `runtime.temporal.api_key`,
and `task_queue` interpolation.

Temporal:

- `TEMPORAL_ADDRESS`: Temporal frontend address, for example `localhost:7233`
  or `temporal-frontend.default.svc.cluster.local:7233`
- `TEMPORAL_NAMESPACE`: Temporal namespace, default `default`
- `TEMPORAL_TLS`: set to `true`, `1`, or `yes` for TLS
- `TEMPORAL_API_KEY`: API key for Temporal Cloud or compatible frontends
- `TEMPORAL_TASK_QUEUE`: optional task queue override when the YAML uses
  interpolation

Default local profile:

```text
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TLS=false
TEMPORAL_API_KEY=
TEMPORAL_TASK_QUEUE=typeflux-local
TYPEFLUX_ENVIRONMENT=local
TYPEFLUX_DEPLOYMENT_ID=local-dev
TYPEFLUX_TEMPORAL_REGION=local
```

Temporal Cloud live-smoke profile, saved locally as ignored
`packages/python/.env.temporal-cloud`:

```text
TEMPORAL_ADDRESS=<namespace-id>.tmprl.cloud:7233
TEMPORAL_NAMESPACE=<namespace-id>
TEMPORAL_TLS=true
TEMPORAL_API_KEY=...
TEMPORAL_TASK_QUEUE=typeflux-cloud-smoke
TYPEFLUX_ENVIRONMENT=temporal-cloud
TYPEFLUX_DEPLOYMENT_ID=quickstart-cloud-smoke
TYPEFLUX_TEMPORAL_REGION=us-east
```

Run live Cloud checks with:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run --directory packages/python python -m examples.lifecycle_review.main run
```

For project manifests, prefer checked-in environment profiles plus ignored env
files for secrets:

```bash
cd packages/python
uv run typeflux-project resolve examples/typeflux.project.yaml \
  --workflow lifecycle_review \
  --environment temporal_cloud_dev \
  --json

uv run typeflux-project run examples/typeflux.project.yaml \
  --workflow lifecycle_review \
  --environment temporal_cloud_dev
```

The selected environment profile applies listed env files and variables only for
that project command. It also suppresses implicit cwd `.env` loading so the
runtime target comes from the selected profile rather than the shell location.

For self-hosted TLS with custom certs or mTLS, use structured YAML instead of
only `TEMPORAL_TLS`:

```yaml
runtime:
  temporal:
    address: temporal.example.com:7233
    namespace: default
    tls:
      server_root_ca_cert:
        value_from:
          file: /etc/typeflux/temporal/ca.pem
      domain: temporal.example.com
      client_cert:
        value_from:
          file: /etc/typeflux/temporal/client.pem
      client_private_key:
        value_from:
          file: /etc/typeflux/temporal/client.key
```

Docker Compose and Kubernetes projected secrets work with the same
`value_from.file` shape. For example, mount a Docker secret at
`/run/secrets/openai_api_key` and configure:

```yaml
runtime:
  provider:
    type: openai
    api_key:
      value_from:
        file: /run/secrets/openai_api_key
```

Secret references are required by default. Use `required: false` only for
profiles where the same YAML intentionally supports running without that
credential.

Provider:

- `OPENAI_API_KEY`: required for `runtime.provider.type: openai`
- `ANTHROPIC_API_KEY`: required for `runtime.provider.type: anthropic`
- `TYPEFLUX_OPENAI_MODEL`: model name used by examples and YAML interpolation
- `TYPEFLUX_ANTHROPIC_MODEL`: Anthropic model name used by YAML interpolation

Prefer `runtime.provider.api_key.value_from.env: OPENAI_API_KEY`,
`runtime.provider.api_key.value_from.env: ANTHROPIC_API_KEY`, or
`value_from.file` for production workers. Literal strings and env interpolation
still work for compatibility, but they put the resolved secret value directly on
the in-memory spec.

Provider variants of the same example (for instance
`typeflux.anthropic.yaml` next to `typeflux.yaml`) must run on separate task
queues and register distinct workflow type names. A worker polls everything on
its queue, so a fake or OpenAI worker sharing a queue with an Anthropic worker
can pick up executions meant for the other provider. The shipped variants use
isolated default queues (`…-anthropic-typeflux`); be aware that exporting
`TEMPORAL_TASK_QUEUE` overrides the default for every spec that interpolates
it, which re-introduces the collision when two variant workers run with the
same environment. Deployment plan generation also refuses to put two workflows
on one queue unless `--allow-shared-task-queue` is passed.

Langfuse:

- `LANGFUSE_HOST`: Langfuse host, for example
  `https://us.cloud.langfuse.com`
- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_PROMPT_LABEL`: prompt label, default often `production`

Provenance and environment:

- Set deployment/environment identifiers through your orchestrator and CI where
  possible. Typeflux records git and deployment metadata when available through
  runtime provenance.
- Keep high-cardinality IDs in metadata rather than tags.

Secrets should come from your runtime secret manager, Kubernetes Secrets,
GitHub Actions secrets, or another secret store. Do not bake secrets into
images, commit `.env` files, or put real keys in ConfigMaps.

Observability records typed secret references only as source provenance:
runtime path, source kind (`env` or `file`), source name/path, and whether the
source was configured. API key values, certificate contents, and token material
are never recorded in `typeflux.*` metadata or emitted as search tags.

## Scaling

Scale YAML workers horizontally by running more worker replicas on the same
Temporal task queue. Temporal distributes activity and workflow tasks across
pollers.

Use separate task queues when you need isolation by environment, tenant,
workflow family, provider budget, or compliance boundary.

Tune concurrency at three layers:

- Map step `concurrency` controls how many activity tasks one workflow map step
  schedules at a time.
- Worker/provider rate-limit policies control provider calls inside each worker
  process.
- Replica count controls total available pollers and total provider-call
  capacity.

When using provider/model limits, account for total replicas. A per-worker
`max_concurrent: 4` with three replicas can create up to twelve concurrent
provider calls unless a shared external limiter is added.

## Retry And Backpressure

Typeflux has two retry layers:

- Local provider retry handles short provider/transient errors inside an
  activity attempt.
- Temporal activity retry handles durable recovery after worker crashes, long
  outages, and exhausted local retries.

Configure local provider retry in YAML when brief provider throttling should be
absorbed before Temporal retries the whole activity:

```yaml
runtime:
  provider_retry:
    max_attempts: 3
    initial_backoff_seconds: 0.5
    max_backoff_seconds: 5.0
    backoff_multiplier: 2.0
    retry_rate_limits: true
    retry_transient_errors: true
```

Structured-output validation repair is owned by Typeflux by default. YAML
`validation_retries` controls how many corrected provider calls Typeflux makes
inside one activity attempt. OpenAI/Instructor retries are disabled by default
to avoid hidden nested repair loops; Python-only callers can opt into
`OpenAIProvider(instructor_max_retries=N)` when they intentionally want
Instructor to do additional repair inside each Typeflux attempt.

Keep local provider retries small so worker capacity is not tied up for long
periods. Omit `runtime.provider_retry` to keep the default no-local-retry
behavior (`max_attempts: 1`). Use Temporal retry policy for durable recovery.

Backpressure and retry metadata appear in `typeflux.provider_controls`, including
queued/throttled/retry fields and selected provider/model policy metadata.

## Shutdown

Workers are long-running processes. Let the orchestrator send SIGTERM and allow
a grace period before SIGKILL. The reference Kubernetes manifest uses
`terminationGracePeriodSeconds: 45`, and the compose file uses
`stop_grace_period: 45s`.

If a worker exits while activities are running, Temporal will eventually retry
or time out work according to workflow/activity retry and timeout settings.

## Debugging Checklist

Worker cannot start:

- Run `--preflight`.
- For project-generated workers, confirm the selected policy files still compose
  to the generated `TYPEFLUX_EXPECTED_POLICY_HASH` value.
- Check YAML interpolation and missing env vars.
- Confirm Python import paths for activity modules, schemas, providers, and
  hooks.

Worker is idle:

- Confirm starter and worker use the same `TEMPORAL_ADDRESS`,
  `TEMPORAL_NAMESPACE`, and task queue.
- Check Temporal UI task queue pollers.
- Verify the worker container can reach Temporal from its network.

Activity fails:

- Check prompt registry credentials and prompt labels.
- Check provider credentials and selected model.
- Inspect schema mismatch and validation repair logs.

Slow or throttled execution:

- Inspect `typeflux.provider_controls`.
- Compare map `concurrency`, worker replicas, and provider/model limits.
- Check provider account limits and retry counts.

Trace missing:

- Verify Langfuse env vars.
- Allow for Langfuse search index lag.
- Search by exact workflow ID or inspect a known trace ID.
- Confirm observability is configured in YAML.
- Confirm the workflow was started through `typeflux.yaml.submit` or
  `TypefluxYamlRuntime.execute_workflow(...)` if you expect a root Typeflux
  workflow trace. Raw Temporal starts should be debugged through their
  correlated activity/generation observations.

Temporal UI is the source of truth for workflow state. Use workflow history,
activity attempts, task queue pollers, and pending activities before debugging
application-level symptoms.
