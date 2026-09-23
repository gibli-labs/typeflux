# Lifecycle Review Example

This demo shows the YAML lifecycle surface for production-style workflows:

- progress through a generated Temporal query
- cooperative cancellation through a generated Temporal signal
- a human review gate after a YAML step
- routed review decision signals
- optional Langfuse trace metadata for safe lifecycle status

## Run With Local Temporal

Start Temporal:

```bash
docker run --rm -d --name typeflux-dev \
  -p 7233:7233 -p 8233:8233 \
  temporalio/temporal:latest server start-dev --ip 0.0.0.0
```

Run the demo commands from the repo root with `uv run` so they use the locked
environment.

The repo also includes Docker, Docker Compose, and Kubernetes worker templates
in [YAML Worker Deployment](../../docs/yaml-worker-deployment.md).
For Temporal Cloud, create an ignored `.env.temporal-cloud` profile with
`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TLS=true`,
`TEMPORAL_API_KEY`, and `TEMPORAL_TASK_QUEUE`, then run commands with
`TYPEFLUX_ENV_FILE=.env.temporal-cloud`.

Run the in-process demo. It starts a worker, starts a workflow, waits until the
review gate, approves it, and prints the result:

```bash
uv run python -m examples.lifecycle_review.main run
```

## Run As App + Worker

Start the worker:

```bash
uv run python -m examples.lifecycle_review.main worker
```

In another shell, start a workflow:

```bash
uv run python -m examples.lifecycle_review.main start --workflow-id lifecycle-review-demo
```

Query status directly from Temporal:

```bash
uv run python -m examples.lifecycle_review.main status lifecycle-review-demo
```

Submit a review decision to release the review gate. The decision must be one
of the `user_decisions` keys in `typeflux.yaml` (`prepare_submission`,
`route_department`, or `send_email`):

```bash
uv run python -m examples.lifecycle_review.main review lifecycle-review-demo send_email \
  --reviewer demo-user \
  --notes "Routed to email after checking the packet"
```

Each decision routes the workflow to its configured step, and execution falls
through from there: the routed step and every later YAML step run in order. For
example, `route_department` runs `route_to_department` and then `send_email`,
while `send_email` skips the two earlier post-review steps.

Request cooperative cancellation:

```bash
uv run python -m examples.lifecycle_review.main cancel lifecycle-review-demo \
  --reason "user requested cancel"
```

Export durable lifecycle audit events from Temporal history:

```bash
uv run python -m examples.lifecycle_review.main audit lifecycle-review-demo
```

For retention pipelines, emit one normalized audit event per line:

```bash
uv run python -m examples.lifecycle_review.main audit lifecycle-review-demo --jsonl
```

Audit exports preserve reviewer identity, cancellation reasons, and failure
messages verbatim by default for compliance use. Add `--redact` to mask those
sensitive fields when the export leaves the trust boundary:

```bash
uv run python -m examples.lifecycle_review.main audit lifecycle-review-demo --redact
```

## Temporal Interface

Lifecycle-enabled YAML workflows expose fixed names:

```python
handle = client.get_workflow_handle("lifecycle-review-demo")
status = await handle.query("typeflux_lifecycle_status")
await handle.signal("typeflux_request_cancel", "user requested cancel")
await handle.signal("typeflux_submit_review", {"user_decision": "send_email"})
```

The review payload is a `ReviewCommand`: `user_decision` is required and must
match a `user_decisions` key from `typeflux.yaml`; `reviewer` and `notes` are
optional.

Temporal remains the source of truth. `typeflux_lifecycle_status` is a bounded
operator snapshot with a recent event tail, not the durable audit log. Apps may
mirror queried lifecycle status into their own database or cache, but Typeflux
does not require a database-specific sink.

Audit trails should come from Temporal workflow history. The `audit` command
normalizes workflow, activity, cancellation, and review events into JSON so an
app can store them in its own retention or compliance system. Review decisions
and reviewer identity are included for audit; freeform review notes are not
included in the normalized audit event.

## Optional Langfuse Metadata

Provide Langfuse credentials in `.env`:

```bash
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Run the traced demo. This command opts the YAML runtime into Langfuse, starts a
worker, starts the workflow through `TypefluxYamlRuntime.execute_workflow`,
waits at the review gate, approves it, flushes the trace, and prints a trace
lookup command for the workflow ID:

```bash
uv run python -m examples.lifecycle_review.main run-traced \
  --workflow-id lifecycle-review-langfuse-demo
```

Find the trace:

```bash
uv run python -m typeflux.observability trace list \
  --workflow-id lifecycle-review-langfuse-demo \
  --limit 1 \
  --json
```

Inspect the returned `trace_id`:

```bash
uv run python -m typeflux.observability trace inspect TRACE_ID --json
```

Inspect the trace and verify:

- root metadata includes `typeflux.lifecycle`
- lifecycle metadata contains state/progress/checkpoint fields
- reviewer and freeform notes are not preserved under `typeflux.*`
- workflow input/output redaction still applies

The `status`, `review`, and `cancel` commands use Typeflux runtime
helpers that create separate lifecycle operation observations in Langfuse. In
the Langfuse UI, filter by tags such as `typeflux.lifecycle`,
`typeflux.lifecycle.query:typeflux_lifecycle_status`, or
`typeflux.lifecycle.signal:typeflux_submit_review`. These operation traces carry
safe correlation fields like workflow name, workflow ID, run ID, operation name,
review decision, and status progress. They intentionally do not carry reviewer
identity, review notes, or cancellation reason under `typeflux.*`.

Temporal's raw OpenTelemetry query/signal spans can still appear beside these
Typeflux lifecycle operation traces. Treat the raw spans as low-level Temporal
telemetry and the Typeflux lifecycle operation traces as the stable application
debugging surface.

The demo waits for the review gate with
`runtime.wait_for_lifecycle_state(...)`, which polls untraced at a one-second
interval, then makes a single traced `query_lifecycle_status` call before
submitting the review. A traced demo run therefore shows one root workflow
trace, one curated lifecycle status query, and one curated review signal —
internal polling never appears as lifecycle operations. Follow the same
pattern in clients: poll slowly (one to five seconds, or user-triggered
refresh) with `trace=False` or `wait_for_lifecycle_state`, and reserve traced
status queries for deliberate, auditable checks.
