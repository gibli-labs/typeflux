# Repository Instructions for Copilot Code Review

This is a Python/TypeScript monorepo for Temporal-native typed AI activities, with a generated HTTP client, console and MCP server. Review changes as production library code, not as an application script.

- Follow the approved issue scope. Before stable adoption, update contracts and candidate consumers together; do not add compatibility shims or intermediate release steps without an actual consumer requirement.
- Temporal workflow code must stay deterministic and sandbox-friendly. Flag workflow imports, randomness, clocks, environment reads, network calls, or file IO inside workflow execution paths.
- Use the shared checks documented in `docs/development.md`. Default CI includes non-live suites and isolated local Temporal with fake providers. External OpenAI, Anthropic and Langfuse tests still require explicit opt-in and credentials. Graph mappings cannot replace passing test evidence.
- Never suggest committing `.env` or real API keys. Treat OpenAI, Anthropic, Langfuse, Temporal, and GitHub tokens as sensitive.
- Use typed metadata contributors for runtime metadata. Keep execution manifests focused on execution contracts; development and client control-plane events do not belong there. Meaningful metadata/manifest changes need behavioral verification.
- Prefer narrow, issue-linked PRs with focused tests. Avoid unrelated refactors.
- Use existing patterns in `packages/python/src/typeflux` before introducing new abstractions.
- For optional SDK integrations, keep imports lazy or guarded so minimal installs still work.
- For retry/error handling, prefer SDK exception types and status codes over broad message substring matching.
