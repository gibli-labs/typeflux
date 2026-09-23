# Replay fixtures

Recorded Temporal histories for the YAML workflow replay harness
(`tests/test_replay.py`). Replay itself is fully local — these tests run in
normal CI without a Temporal server.

- `replay_demo_project/` — self-contained fixture package (schemas,
  activities, deterministic fake provider).
- `compensation.yaml` — a saga (#299) whose `charge` step fails, unwinding
  `book`'s compensation (`cancel_book`); its history ends in
  WorkflowExecutionFailed AFTER the compensation activity ran, so replay
  covers the failure-unwind command sequence (the compensation activity
  scheduled from the outer handler + the compensation lifecycle events). Uses
  its own `comp_activities` module + `ReplayFixtureCompensationProvider`.
- `plain.yaml` / `lifecycle.yaml` / `cached_map.yaml` — fixture specs; the
  lifecycle spec records a review-gate run because the wait/routing path is
  the most replay-sensitive engine code, and the cached-map spec records a
  session-cache-enabled map fan-out so the prep/release cache bracket
  (`__prepare_cache__` → per-item calls → `__release_cache__`, #363/#368) is
  covered — its fake provider is reference-style with a stable cache id so
  the release command is actually emitted.
- `histories/*.json` — recorded histories captured from a real run.

## Regenerating

Start a local Temporal dev server, then:

```bash
uv run python tests/replay_fixtures/generate_histories.py
```

Pass fixture names (`plain`, `lifecycle`, `cached_map`, `composition`,
`subworkflow`, `compensation`) to regenerate selectively — touching one
fixture never churns the others' recorded files.

Regeneration is required when the fixture specs change or when
`GENERATOR_VERSION` is deliberately bumped (the generated control flow
changed semantics). In both cases the replay tests fail loudly first — that
failure is the signal this harness exists to give: an engine change that
breaks replay for in-flight histories must bump `GENERATOR_VERSION` so the
versioned workflow type changes instead.
