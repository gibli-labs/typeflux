---
name: gh-resolve-issue
description: Plan and implement a GitHub issue through local validation, a coherent PR, and evidence-based review. Use when the user asks to resolve or plan-and-fix a GitHub issue.
---

# Resolve a GitHub issue

Read the issue, related PRs, repository `AGENTS.md`, and relevant source. Establish what remains; a merged proposal or implementation slice does not necessarily complete the issue.

Present a concrete plan when the user requests planning or material choices remain. Existing approval or an instruction to implement an already discussed plan authorizes that scope; do not ask for the same approval again. A broad implementation request is not authorization for unrelated product or deployment changes.

Once authorized, record the plan on the issue before implementation. Include the outcome, scope, validation, and remaining acceptance work. Use `gh issue comment --body-file` with actual newlines. Use closing keywords only when every issue acceptance criterion is fulfilled; otherwise use `Refs #N`.

Use a clean scoped branch. Preserve existing user work. Finish and validate the implementation before pushing and opening one PR; batch follow-up changes so review bots see coherent diffs.

## Repository development loop

Use the repository's maintained verification entrypoint instead of copying its commands into this skill. In Typeflux, read `docs/development.md`:

- Retrieve bounded `tools.project_graph context <capability-id>` when the capability is modeled; use ordinary search for unknown relationships.
- For work spanning sessions, record intent, acceptance, progress and pending external effects with `tools.project_graph task`. Resume and reconcile before retrying an external write.
- Run focused behavioral checks, then the shared development profile appropriate to the scope. Typeflux's full profile is `uv run --project packages/python python -m tools.development run default` after frozen setup.
- Check generated contract drift and candidate consumers in the same change. Pre-1.0 compatibility shims are unnecessary unless the user or an actual consumer requires them.
- Use per-test results and exact source identity as evidence. Graph mappings, successful job names, agent summaries, and local journals alone are not behavioral or deployment proof.
- Review graph impact against the starting commit. Unknown mappings stay explicit; selection is advisory and cannot replace required CI.

If runtime metadata or observability changes, use the repository's typed metadata contribution framework where applicable. State whether the execution manifest changes and why. Development task events and client control-plane actions do not belong in execution manifests.

## Completion

Commit with sign-off when required, push the completed branch, and open the PR. Attach it to the current Codex task when that capability is available. Read required checks and reviews; wait without frequent unchanged polls. Follow the Bugbot-resolution skill when findings exist, documenting evidence before resolving threads.

Merge only within the user's authorization and after required checks/review pass. Preserve unresolved evaluation, live-environment, or rollout obligations explicitly. Never equate merging tooling with proving productivity or production recovery.
