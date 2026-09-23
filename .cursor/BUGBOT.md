# Bugbot Review Instructions

This repository is a Python 3.11+ library for Temporal-native typed AI activities. Review PRs as production library changes, not one-off scripts.

## Review Priorities

- Flag bugs that could affect Temporal workflow determinism, replay safety, activity retry behavior, secret handling, or observability trace reconstruction.
- Prefer concrete correctness findings over style-only comments. CI already runs Ruff, mypy, pytest, build, CodeQL, dependency review, and Gitleaks.
- Keep public APIs backward compatible unless the linked issue explicitly proposes a breaking change.
- Expect PRs to be narrow and issue-linked. Call out unrelated refactors or behavior changes that are not explained in the PR.

## Temporal Rules

- Workflow code must stay deterministic and sandbox-friendly.
- Flag randomness, clocks, environment reads, file IO, network calls, mutable global state, or heavyweight imports inside workflow execution paths.
- Activity code may perform IO, but async Temporal activity wrappers must not block the event loop with synchronous provider calls unless the call is isolated in a worker thread.
- Generated YAML workflows should preserve configured Temporal workflow type names and avoid module-global collisions.

## AI Provider And Retry Rules

- Optional SDK imports must stay lazy or guarded so minimal installs continue to work.
- Retry classification should use SDK exception types and HTTP status codes before any text fallback.
- Avoid compounding retry loops without clear ownership between provider SDKs, Instructor, Typeflux validation repair, and Temporal retries.
- Live provider tests must remain opt-in. Default CI should only run `pytest -q -m "not live"`.

## Observability And Metadata

- Preserve Typeflux manifest and trace metadata compatibility unless a migration issue says otherwise.
- Treat `typeflux.*` metadata, manifest hashes, prompt refs, workflow IDs, activity names, trace IDs, tags, and environment/deployment metadata as compatibility-sensitive.
- Langfuse trace reader changes should preserve CLI output shape and include tests for list, search, inspect, export, and pagination behavior where relevant.
- Redaction changes must avoid leaking secrets and should preserve non-sensitive data fidelity.

## Secrets And Data

- Never suggest committing `.env`, API keys, Langfuse credentials, OpenAI/Anthropic keys, Temporal credentials, GitHub tokens, or live customer payloads.
- `.env.example` may contain blank or obvious placeholder values only.
- If a PR prints or snapshots provider responses, traces, or metadata, verify that sensitive fields are redacted or excluded.

## Test Expectations

- For local/default validation, prefer:
  - `ruff check .`
  - `ruff format --check .`
  - `mypy src/typeflux`
  - `python -m build`
  - `pytest -q -m "not live"`
- Live OpenAI, Anthropic, and Langfuse tests require explicit human opt-in and valid credentials.
