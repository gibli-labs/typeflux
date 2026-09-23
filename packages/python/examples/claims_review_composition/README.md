# Claims Review Composition Example (#55)

The full **graph-shaped composition** surface from #55, in one organized Typeflux
project, run live: a `parallel:` block, `when:` gating (an if/else pair), a
`workflow:` sub-workflow step, a `map.workflow` fan-out, and **multiple review
gates** (`lifecycle.gates`) — demonstrated in **both authoring modes** at once.

## What it demonstrates

The parent, `claims_review.yaml` (`ClaimsReviewWorkflow`), composes:

1. **`parallel:` block with an if/else pair** — the `screen` step runs two
   heterogeneous, mutually-exclusive branches keyed on `input.priority`:
   - `fast_track` (`when: priority == low`) — a single acknowledge activity;
   - `full_review` (`when: priority != low`) — a **`map.workflow` fan-out** of the
     `claim_triage` sub-workflow over every claim.

   Exactly one branch runs per request; the gated-out branch contributes `None` to
   its `collect` field (the collect object's fields ARE the branch ids — decision D4).
2. **`workflow:` sub-workflow step** — `escalation` runs the `escalation_review`
   sub-workflow as a child execution.
3. **Multiple gates** (`lifecycle.gates`) — two named gates on distinct steps:
   - `intake_gate` (after `consolidate`): `escalate` → run escalation, or `expedite`
     → skip to finalize; a `route` **timeout** expedites if no decision arrives;
   - `compliance_gate` (after `escalation`): `approve` / `reject`, both routing to
     `finalize`.

### Both authoring modes, composed

| Workflow | Mode | How activities are supplied |
|---|---|---|
| `claims_review` (parent) | **YAML + code** | `activities.modules: [activities]` — `AIActivity` objects in `activities.py` |
| `claims_review_pure` (parent twin) | **pure-YAML** | the SAME full graph with `activities.definitions` only — zero code modules |
| `claim_triage` (child) | **pure-YAML** | `activities.definitions` + an inline prompt registry |
| `escalation_review` (child) | **pure-YAML** | `activities.definitions` + an inline prompt registry |

The two parents are twins over the same children: identical step and gate ids, one
authored yaml+code, one standing alone as pure YAML (#55 scope addition A).

Every workflow declares an env-keyed OpenAI provider and out-of-the-box Langfuse
observability (with redaction) directly in YAML, and the sub-workflows resolve through
the project manifest (`../typeflux.project.yaml`).

### Governance closure (#55 §9)

The parent references two sub-workflows, so admitting it **transitively admits the
whole composed program** under the selected project policy — the
`policy_subworkflow_closure` check re-validates `claim_triage` and `escalation_review`
against the parent's composed policy. A child that violated the policy would fail the
parent's admission. See `docs/yaml.md` → *Composition governance and ceilings*.

## Run it

Interactive demo (a local Temporal dev server + `OPENAI_API_KEY`; drives both gates
and prints the child executions — add `--workflow claims_review_pure` for the
pure-YAML twin, or `CLAIMS_REVIEW_DECISION=expedite` to skip escalation):

```bash
temporal server start-dev
cd packages/python
uv run python -m examples.claims_review_composition.main
```

Admission (no server needed) — validates the graph and the transitive-closure
admission:

```bash
cd packages/python
TYPEFLUX_LOCAL_OBSERVABILITY=none uv run python -m typeflux.project \
  validate examples/typeflux.project.yaml --environment local \
  --workflow claims_review --workflow claims_review_pure \
  --workflow claim_triage --workflow escalation_review
```

Live end to end (a real Temporal dev server; a deterministic offline provider stands
in for the vendor call, per repo convention — no API key required):

```bash
temporal server start-dev
cd packages/python
TYPEFLUX_LIVE_TEMPORAL=1 TYPEFLUX_LOCAL_OBSERVABILITY=none \
  uv run --all-extras pytest -m live -k composition_review
```

The live test runs BOTH authoring-mode parents: each starts the workflow, drives
`intake_gate` (`escalate`) and then `compliance_gate` (`approve`) by gate id, and
asserts the two `claim_triage` child executions (`{id}.triage_all-0/1`) and the
`escalation` child (`{id}.escalation`) ran with the parent-link memo, returning
`ReviewPacket(decision="approved")`. A further non-live test fires `intake_gate`'s
timeout at the gate runtime and asserts it routes to `finalize`.
