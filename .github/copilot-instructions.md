# Repository Instructions for Copilot Code Review

This is a Python 3.11+ library for Temporal-native typed AI activities. Review changes as production library code, not as an application script.

- Keep public APIs backward compatible unless an issue explicitly calls for a breaking change.
- Temporal workflow code must stay deterministic and sandbox-friendly. Flag workflow imports, randomness, clocks, environment reads, network calls, or file IO inside workflow execution paths.
- Default CI must run non-live tests only: `uv run --directory packages/python pytest -q -m "not live"`. Live OpenAI, Anthropic, and Langfuse tests require explicit human opt-in and real credentials.
- Never suggest committing `.env` or real API keys. Treat OpenAI, Anthropic, Langfuse, Temporal, and GitHub tokens as sensitive.
- Preserve Typeflux manifest and observability metadata compatibility. Changes to `typeflux.*` metadata, manifest hashes, prompt refs, workflow IDs, activity names, or trace reconstruction should include tests.
- Prefer narrow, issue-linked PRs with focused tests. Avoid unrelated refactors.
- Use existing patterns in `packages/python/src/typeflux` before introducing new abstractions.
- For optional SDK integrations, keep imports lazy or guarded so minimal installs still work.
- For retry/error handling, prefer SDK exception types and status codes over broad message substring matching.
