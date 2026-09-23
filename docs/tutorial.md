# Tutorial: from zero to a governed, observed, deployed AI workflow

This is the **effective path** through Typeflux Temporal — the opinionated way to take an AI
workflow from nothing to production and then operate it. It is a guided arc, not an API
reference: each section links out to the reference doc that carries the field-by-field
detail, and every command and YAML block below was run against a live local stack before it
was written down.

The loop you are learning is the one the console teaches: **author in code → validate →
plan → PR → promote → observe.**

> **Using the TypeScript SDK?** There is a TS-first mirror of this tutorial at
> [docs/typescript/tutorial.md](typescript/tutorial.md). The `typeflux.yaml` in Section 1 is
> language-neutral and runs on both editions; the SDK wiring differs (Pydantic vs Zod,
> import-path vs injected resolution). This page narrates the Python path.

**Sections**

1. [Your first workflow in 10 minutes](#1-your-first-workflow-in-10-minutes)
2. [Make it real](#2-make-it-real) — typed activities, prompts, composition, gates, compensation
3. [Govern it](#3-govern-it) — policies, risk tiers, `require_*`, admission
4. [Ship it](#4-ship-it) — environments, deployment plans, the PR-approval flow, secrets
5. [Operate it](#5-operate-it) — the console, the MCP server, the audit story
6. [Effectiveness tips](#6-effectiveness-tips) — the opinionated dos and don'ts

---

## 1. Your first workflow in 10 minutes

### The mental model

Temporal owns durable execution; Typeflux owns the AI activity layer. **A workflow is a
plain, replayable Temporal graph** — the model call happens inside a Temporal *activity*, and
the workflow only orchestrates typed activity calls. Keep that boundary and everything else
falls into place (see [Concepts](concepts.md)).

### Install and start a local stack

Install from PyPI — no credentials, no checkout:

```bash
pip install "typeflux[openai]"   # or [anthropic], [gemini], [live]
```

(Working from a checkout of the monorepo instead? `cd packages/python &&
uv sync --extra live --group dev`, then prefix commands with `uv run`.)

**No provider account yet?** The authoring and validation loop below works
offline with zero paid credentials: swap `type: openai` for `type: fake` in
the specs and every `validate` / preflight step passes as written. Actually
*executing* a workflow needs model output, which the YAML-built fake does not
synthesize yet ([#953](https://github.com/gibli-labs/typeflux-temporal/issues/953)) —
for credential-free execution use the testing route instead: construct the
runtime programmatically and inject `typeflux.testing.FakeProvider([...])`
with scripted responses, exactly as the offline test suite does.

For real providers, supply credentials as **environment variables** — Typeflux
specs reference key *names*, never values. A `.env` in the working directory is loaded automatically; a different
file is loaded with `TYPEFLUX_ENV_FILE=/path/to/.env` (see the
[README → env loading](../README.md#quickstart)). At minimum:

```text
OPENAI_API_KEY=...
TYPEFLUX_OPENAI_MODEL=gpt-4o-mini
```

Start a Temporal dev server. **Use a non-default port** so it can't collide with another
Temporal on `7233`, and wire that address through the environment — the spec interpolates it:

```bash
temporal server start-dev --port 7333 --ui-port 8333 --ip 127.0.0.1
# health: temporal operator cluster health --address 127.0.0.1:7333  -> SERVING
```

Every command below runs against it by exporting `TEMPORAL_ADDRESS=127.0.0.1:7333`.

### Create the project

Everything in this section is created from scratch — no checkout needed. Make a
fresh directory with this layout (the `.env` from above lives beside the YAML):

```text
my-first-typeflux/
  .env
  typeflux.yaml
  tutorial_quickstart/
    __init__.py        # empty file — makes the schemas importable
    schemas.py
```

`tutorial_quickstart/schemas.py` holds the two Pydantic models — the workflow's
typed input and the contract the model's answer must satisfy:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TicketInput(BaseModel):
    """A raw support ticket handed to the workflow."""

    subject: str = Field(description="Ticket subject line.")
    body: str = Field(description="Ticket body as written by the customer.")


class Triage(BaseModel):
    """The typed result the model must return."""

    category: Literal["billing", "bug", "how_to", "account", "other"] = Field(
        description="Best-fit category for routing the ticket."
    )
    urgency: Literal["low", "medium", "high"] = Field(
        description="How quickly a human should look at this ticket."
    )
    summary: str = Field(description="One-sentence summary a human agent can skim.")
```

### The smallest useful spec

A minimal `typeflux.yaml` is one typed activity with an inline prompt and a provider that
reads its key from the environment (a tested copy of this whole project ships in the
repository as
[`examples/tutorial_quickstart/`](../packages/python/examples/tutorial_quickstart/)):

```yaml
project: tutorial_quickstart
name: tutorial_quickstart
task_queue: ${TEMPORAL_TASK_QUEUE:-tutorial-quickstart-typeflux}

runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS:-localhost:7233}
    namespace: ${TEMPORAL_NAMESPACE:-default}
    tls: ${TEMPORAL_TLS:-false}
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
        required: false

  # `inline` keeps the prompt in this file — no external prompt registry needed
  # to run your first workflow. Section 2 shows moving it to Langfuse/LangSmith.
  registry:
    type: inline
    prompts:
      triage_ticket:
        model: ${TYPEFLUX_OPENAI_MODEL:-gpt-4o-mini}
        temperature: 0
        messages:
          - role: system
            content: >
              You triage inbound support tickets. Choose the best-fit category
              and urgency, and write a one-sentence summary. Answer only from the
              ticket text; do not invent account details.
          - role: user
            content: |
              Subject: {{ subject }}

              {{ body }}

  # The provider reads its key from the environment (OPENAI_API_KEY). Never put
  # secret values in YAML — only the env-var name.
  provider:
    type: openai
    model: ${TYPEFLUX_OPENAI_MODEL:-gpt-4o-mini}
    structured_mode: json_schema
    api_key:
      value_from:
        env: OPENAI_API_KEY

  # No tracing backend for the first run. Section 5 turns on Langfuse.
  observability:
    type: none

activities:
  definitions:
    - name: triage_ticket
      input: schemas:TicketInput
      output: schemas:Triage
      prompt:
        name: triage_ticket
        type: chat
      validation_retries: 1

workflow:
  name: TutorialQuickstartWorkflow
  input: schemas:TicketInput
  output: schemas:Triage
  steps:
    - id: triage
      activity: triage_ticket
```

The `input: schemas:TicketInput` / `output: schemas:Triage` refs resolve to the Pydantic
models you created above: module refs import as `{project}.{module}`, so `schemas:` means
`tutorial_quickstart.schemas`. The output model is the contract the provider must satisfy —
Typeflux validates the model's response against it and retries on a mismatch
(`validation_retries`).

### Run it

Start the YAML worker in one terminal, from `my-first-typeflux/`:

```bash
TEMPORAL_ADDRESS=127.0.0.1:7333 python -m typeflux.yaml.run typeflux.yaml
# INFO starting Typeflux YAML worker: spec=… workflow=TutorialQuickstartWorkflow task_queue=tutorial-quickstart-typeflux
```

(From a monorepo checkout instead, the tested copy runs as `uv run python -m
typeflux.yaml.run examples/tutorial_quickstart/typeflux.yaml` from
`packages/python/`, with `examples.tutorial_quickstart` as the project.)

Submit a ticket from another terminal, same directory:

```bash
TEMPORAL_ADDRESS=127.0.0.1:7333 python - <<'PY'
import asyncio
from uuid import uuid4
from typeflux.env import load_env
from typeflux.yaml import build_runtime, load_yaml_spec
from tutorial_quickstart.schemas import TicketInput, Triage

async def main():
    load_env()
    runtime = await build_runtime(load_yaml_spec("typeflux.yaml"))
    wid = f"tutorial-{uuid4().hex[:8]}"
    result = await runtime.execute_workflow(
        TicketInput(subject="Double charged for my subscription",
                    body="I upgraded yesterday and was billed twice. Please refund the extra charge."),
        id=wid, result_type=Triage, tags=["tutorial"])
    print("workflow_id=", wid)
    print(result.model_dump_json(indent=2))

asyncio.run(main())
PY
```

The typed result comes back from a real provider call:

```json
{
  "category": "billing",
  "urgency": "high",
  "summary": "User reports being double charged for their subscription and requests a refund for the extra charge."
}
```

That is the whole loop: a declarative spec, a worker, a durable Temporal workflow, and a
schema-validated AI result. Everything that follows *hardens* this loop.

> The two-terminal split (worker + starter) is the shape you deploy. For a throwaway smoke you
> can run the worker and starter in one process — wrap the submit in
> `async with runtime.worker.build_worker(): …` (as
> [`examples/contract_risk_review/main.py`](../packages/python/examples/contract_risk_review/main.py)
> does). Prefer the split once you have more than one workflow.

---

## 2. Make it real

A ticket triager is a toy. Real workflows have typed activities with hooks, prompts under
version control, multiple steps, human approval, and recovery from partial failure. Add these
as you need them — each is a small, local edit to the spec.

### Typed activities and IO schemas

Pydantic models *are* the contract. A field's type and description become the JSON schema the
provider must return, and its `schema_hash` flows into every manifest and trace so a schema
change is detectable downstream. Hookless activities live entirely in YAML (as above); an
activity that needs a **hook** (pre/post processing, side channels) is defined in Python with
`@ai_activity.defn` and imported via `activities.modules`. See
[Code-Defined Workflows](code-defined-workflows.md) for the hook lifecycle and
[Concepts → the AI-activity contract](concepts.md).

### Prompts: inline → registry

Start with an `inline` registry (Section 1) so a prompt ships with the spec. Graduate to a
managed registry when prompts need their own review, versioning, and labels:

```yaml
runtime:
  registry:
    type: langfuse       # or: langsmith
    label: production
```

The activity then references the prompt by name/label instead of carrying its text. Push the
prompt to the registry once (e.g. `--bootstrap-langfuse` in the example runners), and the
`production` label is what the worker resolves. `examples/support_triage_langfuse/` (Langfuse)
and `examples/contract_risk_review/typeflux.langsmith.yaml` (LangSmith) are the two worked
examples. Provider model and inference params **stay in the Typeflux YAML** even when the
prompt text lives in the registry — a clean split of "what to say" from "how to run it".

### Composition: parallel, `when`, sub-workflows

A single `steps:` list is sequential. The composition primitives (#55) turn a workflow into a
graph without leaving YAML. [`examples/claims_review_composition/claims_review.yaml`](../packages/python/examples/claims_review_composition/claims_review.yaml)
exercises the whole surface — quote it, don't reinvent it:

- **`parallel:`** — a block of branches that run concurrently, each optionally guarded by a
  **`when:`** predicate over the input (`when: { path: input.priority, eq: low }`), with a
  typed `collect.output`.
- **`map: { workflow: … , over: … }`** — fan a *sub-workflow* over a list with bounded
  concurrency and a typed `collect`.
- **`workflow: <id>`** — call another workflow as a durable child step.

`claims_review_pure.yaml` is the same graph with zero Python (pure-YAML children); the
divergence is only how activities are supplied. Field semantics are in
[docs/yaml.md](yaml.md) and [Code-Defined Workflows](code-defined-workflows.md).

### Review gates: a human in the loop

A review gate pauses the workflow for a human decision and routes on the answer. Declare them
under `lifecycle.gates` (or `lifecycle.review` for the single-gate form). Each gate names its
allowed answers under `user_decisions`, mapping every decision to the step it `route`s to, and
an optional `timeout` routes when no decision arrives in time. Gate 1 from
[`examples/claims_review_composition/claims_review.yaml`](../packages/python/examples/claims_review_composition/claims_review.yaml),
quoted exactly:

```yaml
lifecycle:
  gates:
    - id: intake_gate
      after_step: consolidate
      invalid_user_decision: fail
      user_decisions:
        escalate:
          route: escalation
        expedite:
          route: finalize
      timeout:
        seconds: 3600
        on_timeout: route
        route: finalize
```

The workflow blocks after `consolidate`, exposes a status query, and resumes when a decision
signal arrives — routing to `escalation` or `finalize`, or to `finalize` if the 3600s timer
fires first. `examples/lifecycle_review/` is the minimal gate;
`examples/claims_review_composition/claims_review.yaml` runs *two* gates around its
sub-workflows.

### Compensation: undo on failure (#299)

For workflows that touch the outside world, a review gate prevents the wrong action and
**compensation** recovers from the unforeseen one. Mark each external write's *activity
definition* `side_effecting` and give its *step* a `compensate:`; on a later failure the saga
interpreter runs recorded compensations in **reverse (LIFO)** before re-raising. Quoted exactly
from [`examples/compensation_saga/typeflux.yaml`](../packages/python/examples/compensation_saga/typeflux.yaml)
(the `charge_card` write and the `charge` step that undoes it):

```yaml
activities:
  definitions:
    - name: charge_card
      input: schemas:Booking
      output: schemas:ChargeResult
      prompt: saga-charge
      side_effecting: true
workflow:
  # Declares the tier the require_compensation policy governs (examples/policies/saga.yaml).
  risk_tier: human_gated
  steps:
    - id: charge
      activity: charge_card
      compensate:
        activity: refund_card       # input defaults to charge's own output (the ChargeResult)
    # The finalizer is not side-effecting, so it needs no compensate to satisfy the
    # require_compensation policy. If IT fails, `charge` then `book` unwind in reverse.
```

Thread a caller `idempotency_key` through each side-effecting activity so a retry — or a
compensation retry — is a no-op, never a double-charge.
[`examples/review_before_side_effect/`](../packages/python/examples/review_before_side_effect/README.md)
combines the gate and the compensation into the safest pattern: *approve before the
irreversible action, compensate the rest.*

---

## 3. Govern it

Governance in Typeflux is **fail-closed**: a missing policy, an unresolved secret, or an
unmet requirement **blocks** — it never silently proceeds. You preview exactly what will be
enforced *before* anything runs. This is the highest-leverage part of the framework.

### Policies compose

A policy is a YAML allow-list/requirement set bound to workflows through the project manifest.
Policies `extends` one another and **compose by tightening**: `require_*` booleans OR-merge,
allow-lists intersect, numeric ceilings take the most restrictive. The shipped set is
[`examples/policies/`](../packages/python/examples/policies/) — `base.yaml`,
`regulated.yaml` (`extends: [base]`, narrows models, requires TLS + API key + a `us-east`
region + review routes), and `composition.yaml` (composition ceilings). A policy carries a
`policy_hash` over its canonical payload — that hash is what a deployment pins (Section 4).

Policy dimensions worth knowing (full field map in
[`project/policy.py`](../packages/python/src/typeflux/project/policy.py)): allowed
providers/models, observability backends and redaction requirements, Temporal posture
(regions/TLS/API key/**payload codec**), registry hosts, artifact sources/media/size, review
routes, moderation, import roots, secret-reference requirements, composition ceilings, and
risk-tier macros.

### Risk tiers (#300)

Rather than wiring each requirement by hand, a workflow declares a **risk tier** and the
policy attaches requirements to that tier. The vocabulary is four ordered tiers:

```
safe  <  policy_gated  <  human_gated  <  prohibited
```

A workflow opts in with `workflow.risk_tier: human_gated`. A policy's `risk_tiers:` block sets
a `min_tier` **floor** (undeclared workflows are lifted to it), can `require_declared`, and
attaches macro requirements per tier — `require_review`, `require_moderation`,
`require_redaction`, `require_compensation`, `require_payload_codec`, and provider
constraints. Each macro maps to an *existing* check (no parallel engine); reaching
`prohibited` denies admission outright. This is why the compensation example above declares
`human_gated` — a policy whose `human_gated` tier sets `require_compensation: true` is what
turns "every `side_effecting` step must have a `compensate:`" into an enforced rule.

### `require_*`: payload codec (#188) and custom redaction

Two governance knobs matter for regulated data, and they act on **different paths**:

- **Payload codec** — `runtime.temporal.payload_codec` encrypts the *whole* Temporal payload
  at rest (workflow history). A policy demands it with `runtime.temporal.require_payload_codec:
  true` (or the risk-tier macro `require_payload_codec`).
- **Custom redaction** — `observability.redaction.custom_rules` masks PII on the *separate*
  Langfuse egress path. A policy demands named rules with
  `observability.redaction.require_custom_rules: [case_reference, uk_nino]`.

[`examples/privacy_governance/`](../packages/python/examples/privacy_governance/) puts both in
one spec (AES-256-GCM codec with rotation keys from `value_from.env`, plus `case_reference` /
`uk_nino` rules on top of the built-in email/SSN/phone/Luhn catalog) under a `human_gated`
tier, governed by its `regulated.yaml` policy. Remove the codec block and admission fails
closed. The distinction — encryption at rest vs. redaction on egress — is covered in
[Privacy & Data Protection](privacy.md).

### Admission: `validate` → `admit`

Two gates enforce all of the above, both **deterministic and provider-free** (no model calls):

**`validate`** is the manifest-side gate you run in CI over trusted files. It resolves each
workflow against an environment and policy and runs the full check pipeline:

```bash
uv run typeflux-project validate examples/typeflux.project.yaml \
  --environment local --workflow claims_review --policy composition
# Project 'typeflux-examples' is valid for environment 'local'.
# Resolved workflow checks passed: 1. Policy checks applied: 1.
```

`--json` emits every `ProjectValidationCheck` with its code — `policy_selection`,
`policy_provider`, `policy_composition_ceilings`, `policy_risk_tier`, and the rest — so you can
see *which* rule passed or failed and why.

**`admit`** is the runtime-side gate for a spec that an agent authored or that arrived from
outside your trusted filesystem. It resolves the *same* effective runtime and policy and runs
the *same* checks, plus structural gates for untrusted input. Watch it fail closed — here an
`external`-origin spec that imports code, run against the `regulated` policy at a `local`
(non-TLS) environment:

```bash
uv run typeflux-project admit examples/typeflux.project.yaml \
  examples/regulated_disclosure_review/typeflux.yaml \
  --environment local --workflow regulated_disclosure_review --policy regulated --origin external
```
```text
Admission: REJECTED
Origin: external
...
- [failed] admission_external_modules_forbidden: external-origin spec declares module-import-gated
    capabilities that are forbidden for untrusted submissions (arbitrary code execution):
    activities.modules ('activities')
- [failed] policy_temporal: Temporal region 'local' is not allowed; Temporal TLS is required by
    project policy; Temporal API key is required by project policy
```

The same spec as an `operator` origin under `base` at `local` is `ADMITTED`. The lesson is the
lesson: **untrusted code is refused, and posture requirements are enforced before anything
connects to Temporal.** The `--origin external` mode additionally forbids `activities.modules`,
`class:` providers/registries/observers, and moderator callables, and requires a governing
policy — it fails closed with none.

---

## 4. Ship it

Shipping is the same resolve-and-check pipeline, frozen into an immutable artifact and gated
by a human merge.

### Environments and profiles

An environment profile overlays safe runtime settings (Temporal address/namespace/TLS,
observability mode, provider model, task queue) and points at an **ignored** `.env` for
secrets. [`examples/environments/local.yaml`](../packages/python/examples/environments/local.yaml)
is the dev profile; [`temporal-cloud-dev.yaml`](../packages/python/examples/environments/temporal-cloud-dev.yaml)
(bound to the `temporal_cloud_dev` environment id) targets Cloud. Resolution layers
`profiles < environment overlay < per-workflow overrides`, and only *safe provenance* (which
fields were overridden, never the values) is recorded in manifests. Resolve to see the
effective runtime:

```bash
uv run typeflux-project resolve examples/typeflux.project.yaml \
  --workflow contract_risk_review --environment local --json
```

### Deployment plans: plan → verify → promote (#687)

A deployment plan is an **immutable, content-hashed** file that pins a workflow's identity,
composed policy hash, and a **digest-pinned** worker image. Generate one:

```bash
uv run typeflux-project deploy examples/typeflux.project.yaml \
  --environment local --workflow contract_risk_review --policy base \
  --image ghcr.io/your-org/yaml-worker@sha256:<digest> \
  --plan-out deployments
# writes deployments/contract_risk_review.local.<hash>.yaml
```

The plan records exactly what will be enforced (excerpt of a real generated file):

```yaml
identity:
  workflow_name: ContractRiskReviewWorkflow
  workflow_type: ContractRiskReviewWorkflow.7d0d416e25fe
  spec_digest: 7d0d416e25fe…
  environment_id: local
  code: { sha: <git-sha>, branch: <branch>, repo_url: https://github.com/… }
policy:
  applied_policy_ids: [base]
  policy_hash: 59e0b2d1ba0a…
deployment:
  image: ghcr.io/your-org/yaml-worker@sha256:aaaa…
  image_digest_pinned: true
  preflight: { ok: true, issue_codes: [] }
```

**The PR *is* the approval.** You commit the plan file and open a PR; a merged review is the
promotion approval — the CLI writes nothing to a running system. To **verify + render** an
approved plan, pass `--apply`: the plan file (not CLI flags) becomes authoritative, and drift
between the plan and the current resolution **fails closed**:

```bash
uv run typeflux-project deploy examples/typeflux.project.yaml \
  --apply deployments/contract_risk_review.local.<hash>.yaml --output ./out
# Wrote deployment artifacts to ./out:
# - deployment-plan.json   (secret-free plan)
# - kubernetes.yaml        (apply-safe ConfigMap + Deployment; no Secret manifests)
# - secret.scaffold.yaml   (blank Secret template)
# - secrets.env.example    (required Secret-key checklist)
```

The rendered `kubernetes.yaml` runs non-root with preflight startup/readiness/liveness probes
and carries `--expect-policy-hash` from the composed policy — so a worker started under a
drifted policy refuses to run. The reference worker image is
[`deploy/yaml-worker/Dockerfile`](../deploy/yaml-worker/Dockerfile); the full runbook
(including a minikube smoke) is [YAML Worker Deployment](yaml-worker-deployment.md).

### Secrets discipline

Secrets are **references, never values**, end to end: the spec names an env var
(`api_key.value_from.env: OPENAI_API_KEY`), the profile points at an ignored `.env`, the
rendered manifests emit **no** Secret bodies — only a blank scaffold and a checklist of the
keys you must supply out of band. Nothing in a committed plan, manifest, or trace contains a
credential. Keep it that way: put real keys only in ignored env files, GitHub Secrets, or a
secret manager. See [Production Readiness](production-readiness.md).

---

## 5. Operate it

Authoring and shipping are done in code and Git. **Operating** is where the read surfaces earn
their keep — the console for humans, the MCP server for agents, both reading the same
control plane.

### The control plane

Everything the console and MCP server show comes from a read-first HTTP API over your project
manifest ([Control Plane](control-plane.md)). Start it (behind the `api` extra):

```bash
uv run --extra api typeflux-controlplane serve \
  examples/typeflux.project.yaml --host 127.0.0.1 --port 8400
```

`GET /api/v1/meta` advertises what this control plane can do for you — including honest
capability flags:

```json
{ "project": "typeflux-examples", "runtime": "python",
  "capabilities": { "can_start": true, "enforcement_events": true, "github_provenance": false } }
```

Curlable read endpoints (`inspect` tier) include `/workflows`, `/environments`, `/policies`,
`/validate`, `/workflows/{id}/bundle|catalog|connections|prompt-status`, `/deployments`, and
the two audit surfaces below. Temporal-connected operations (`/start|review|cancel`,
`/status`, `/executions`) sit behind capability tokens.

### The console

The [console](../clients/console/README.md) is deliberately **not** an authoring tool —
"authoring stays in code, and the console renders what the resolved contracts say." Run it
against the API (the console proxies `/api` to the control plane; it needs a GitHub Packages
PAT with `read:packages` in `NODE_AUTH_TOKEN` to install its pinned client):

```bash
cd clients/console && export NODE_AUTH_TOKEN=<PAT with read:packages>
npm ci && npm run dev        # http://127.0.0.1:5173
```

Each surface answers one operator question:

| Surface | The question it answers |
| --- | --- |
| **Overview** | What is every workflow's state — validation, digests, policy hash, ranked insights? |
| **Drift** | What is drifting right now (plan/prompt/env), why does it matter, what do I do next? |
| **Runs** | What ran recently, failures first? |
| **Governance** | Where does policy apply, where are the gaps, and where did it actually fire? |
| **Deployments** | Which plans are ready to promote vs. drifted, and what's the promote command? |
| **Personas** | Role-scoped read-only landing views (governance, security, operations, executive). |

The console teaches the loop through its empty states rather than one banner — a workflow
with nothing pinning it prompts "Generate a plan on the Deployments page and approve it by
merging its PR"; a drifted plan says "Promotion fails closed until a fresh plan is generated
and re-approved via PR." That copy *is* the workflow.

### The enforcement feed and provenance: the audit story

Two surfaces answer "did governance actually fire, and who approved what?"

- **Enforcement events (#723)** — `GET /api/v1/enforcement-events` (rendered on the console's
  Governance surface) is a queryable feed of where policy fired: **admission verdicts**
  (workflows that would be rejected now) and **runtime blocks** (moderation, from traces). It
  degrades honestly — if the observer is unreachable it says so and marks the runtime source
  incomplete rather than showing a falsely-empty feed. A live query returns real rejections,
  e.g. `provider 'anthropic' is not allowed by selected project policy` (`verdict: rejected`,
  `source: admission`).
- **GitHub provenance (#727)** — `GET /api/v1/github-provenance` links each approved plan to
  the PR that merged its plan file, and reports HEAD-vs-served drift. This is a **control-plane
  endpoint today, not yet a console page** — don't look for a UI. It reads at request time and
  **fabricates nothing**.

  **The honest-degradation lesson:** provenance needs a server-side token
  (`TYPEFLUX_GITHUB_TOKEN` / `GITHUB_TOKEN`). With none set, it makes **no network call** and
  returns a truthful "not configured" rather than a silent empty success:

  ```bash
  curl -s http://127.0.0.1:8400/api/v1/github-provenance
  # {"plans": [], "partial": {"github": "not_configured"}}
  ```

  That `not_configured` — never a fake `ok` — is the whole posture: a read surface that can't
  prove something says it can't, loudly.

### The MCP server: an agent's hands on the project

[`typeflux-mcp`](../clients/mcp/README.md) makes an AI agent fluent in a Typeflux project and
lets it *operate* runs, under the same fail-closed rules. It exposes 28 tools across tiers —
read (`list_workflows`, `validate_project`, `get_bundle`, `list_enforcement_events`,
`get_github_provenance`, …), operate (`start_workflow`, `submit_review`, `cancel_workflow`,
`repin_operations`, `refresh_project`), and local authoring aids (`scaffold_ai_activity`,
`scaffold_workflow_yaml`, `doctor`) — plus guided `/typeflux:*` recipes. Its safety model is
worth internalizing: **`start_workflow` is preview-then-commit** (a first call returns the
resolved policy hash and a validation report and starts nothing; you re-call with that hash,
so a policy that drifted is rejected before any Temporal connection), authoring aids **return
content, never write files**, and operate tools are **hidden when your token can't perform
them**. The `trace_*` tools are intentionally **deferred** — the control-plane contract exposes
no trace route yet.

The zero-install invocation works from the public npm registry:

```bash
npx -y typeflux-mcp
# typeflux-mcp 0.4.0 listening on stdio (mode: managed-local)
```

Point an editor's MCP config at that command (or at a source build's
`node .../clients/mcp/dist/index.js` when working inside this monorepo), or serve it over HTTP
for a team. HTTP mode **fails closed without a bearer token**
(`TYPEFLUX_MCP_HTTP_TOKEN`); an unauthenticated request gets a `401`, and every session is
isolated with its own control-plane backend:

```bash
TYPEFLUX_MCP_TRANSPORT=http TYPEFLUX_MCP_HTTP_TOKEN=$TEAM_TOKEN \
  TYPEFLUX_CP_URL=http://127.0.0.1:8400 node clients/mcp/dist/index.js
# typeflux-mcp 0.4.0 listening on http://127.0.0.1:8765/mcp (mode: attach; bearer auth required)
```

### Observability

Turn on tracing by setting `runtime.observability.type: langfuse` (or `langsmith`) — it's
explicit opt-in, so credentials in the environment never enable tracing by themselves.
**Tags locate traces; metadata reconstructs them**: Typeflux writes low-cardinality search
tags (workflow name, activity, prompt ref, model, environment) and keeps high-cardinality
values (IDs, hashes, versions) in metadata. The trace root carries the workflow execution
manifest; activity spans carry resolved activity manifests. Search, inspect, and diff traces
from the CLI:

```bash
uv run typeflux-trace trace search --prompt-ref triage_ticket
uv run typeflux-trace trace diff <left-id> <right-id>
```

Redaction is on by default for Langfuse and masks PII on egress while preserving the
operational metadata Typeflux needs. Full detail: [Observability](observability.md),
[Privacy](privacy.md), and the end-to-end [Live OpenAI + Langfuse Walkthrough](live.md).

---

## 6. Effectiveness tips

The opinionated rules, mined from the repo's own design docs and `AGENTS.md`. Internalize
these and Typeflux stays easy; fight them and you fight the framework.

1. **Keep workflows deterministic; put models in activities.** A workflow is a replayable
   Temporal graph — no time, randomness, network, or I/O in the workflow body, and **never
   resolve a prompt or call a model in workflow code.** Those live in activities.
   ([Concepts](concepts.md).)

2. **Let the YAML stand alone.** A hookless project needs no Python — pure-YAML specs inject no
   code, which is exactly why they're safe to admit from untrusted sources. Reach for Python
   only when an activity needs a hook.

3. **Trust fail-closed governance; preview before you run.** A missing policy, unresolved
   secret, or unmet risk-tier requirement blocks. Run `validate` in CI and `admit` on anything
   external; read `get_bundle` to see the resolved contract *before* claiming a change is
   correct. Never start a workflow just to test whether it's valid.

4. **Declare a risk tier and let macros do the wiring.** `workflow.risk_tier` + a policy's
   `risk_tiers:` block is less error-prone than hand-listing `require_*` on every workflow, and
   it reads as intent.

5. **Secrets are references, forever.** Name env vars in specs (`value_from.env`), keep values
   in ignored env files / secret managers, and never let a value reach a plan, manifest, or
   trace. The renderer emits scaffolds, not secrets — keep it honest.

6. **Pin deployments to hashes, approve by PR.** A digest-pinned image + a content-hashed plan
   + a merged PR is the whole promotion story. Drift fails closed on both `--apply` verify and
   the worker's `--expect-policy-hash`. Don't hand-edit a resolved plan.

7. **Contract-first when you touch the control plane.** The OpenAPI contract, the emitted
   schema, and the TS client move together; `typeflux-controlplane
   conformance` fails on drift. Regenerate, don't hand-patch.

8. **Observability is a product surface: link out, don't reconstruct.** Tags locate, metadata
   reconstructs. Keep tags low-cardinality (no IDs, run IDs, or hashes as tags) and let the
   trace backend carry the rest. Don't hand-roll `typeflux.*` metadata — use the typed
   contributors.

9. **Prefer the guided path.** The MCP `/typeflux:*` recipes and the console's per-surface
   copy encode the vetted sequence (validate → plan → PR → promote → observe). When in doubt,
   follow them rather than freelancing.

### Where to go next

- [Concepts](concepts.md) · [Code-Defined Workflows](code-defined-workflows.md) — the model in depth
- [YAML Runtime](yaml.md) — every spec field
- [Control Plane](control-plane.md) · [Control-Plane Auth](control-plane-auth.md) — the operate API
- [Observability](observability.md) · [Privacy](privacy.md) — the run-visibility and data-protection surfaces
- [Production Readiness](production-readiness.md) · [YAML Worker Deployment](yaml-worker-deployment.md) — shipping
- [TypeScript tutorial](typescript/tutorial.md) — the same arc, TS-first
