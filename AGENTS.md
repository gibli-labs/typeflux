# Agent Workflow Rules

## Pull Request Timing

- Do not open a pull request until the implementation is finished, committed, and locally validated for the requested scope.
- Prefer pushing a completed branch once, then opening the PR. Avoid repeated half-finished pushes that trigger Cursor Bugbot, CodeQL, dependency review, and CI on incomplete diffs.
- If a PR must be opened early for visibility, open it as a draft and clearly state what is incomplete. Mark it ready for review only after tests pass and the branch is no longer expected to churn.
- Before opening or marking a PR ready, run the relevant focused tests plus the default non-live suite when practical (from `packages/python/`, prefixed with `uv run`):
  - `ruff check . ../../scripts`
  - `ruff format --check . ../../scripts`
  - `mypy src/typeflux`
  - `python -m build`
  - `pytest -q -m "not live"`
- For small documentation-only changes, use judgment and run the smallest useful validation.

## Review Bot Hygiene

- Treat Cursor Bugbot and other PR reviewers as scarce review signals, not a development loop.
- Do not manually trigger Bugbot on work-in-progress diffs unless the user asks for an early review.
- After addressing review feedback, batch follow-up commits where reasonable so review bots see coherent updates.

## Metadata And Observability

- Do not hand-roll new `typeflux.*` metadata directly in feature code when the typed metadata contribution framework can express it.
- Prefer `typeflux.metadata` contributors for runtime features that emit workflow, activity, lifecycle, provider, YAML, or observability metadata.
- Keep workflow execution manifests focused on planned workflow/activity contract and execution-shape data. Do not put client-side control-plane events such as queries, signals, reviews, or ad hoc CLI actions into the execution manifest unless they are part of the workflow execution graph.
- Contributor metadata must declare redaction exclusions only for safe operational fields, and must not preserve user freeform text, reviewer identity, secrets, or PII under `typeflux.*`.
- Search tags must stay low-cardinality. Avoid workflow IDs, run IDs, manifest hashes, map indexes, queue timings, and other per-run values as tags.
- When planning work that touches metadata, manifests, observability, tracing, lifecycle, providers, YAML runtime behavior, or search, explicitly state whether the plan uses metadata contributors and whether the workflow manifest should change.
