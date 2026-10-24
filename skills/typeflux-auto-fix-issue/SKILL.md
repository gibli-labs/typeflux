---
name: typeflux-auto-fix-issue
description: Run Typeflux's guarded issue auto-fix wrapper when the user explicitly requests automated issue resolution. Ordinary feature work and architecture changes use the normal development loop.
---

# Typeflux auto-fix

Use the active Typeflux checkout, resolved with `git rev-parse --show-toplevel`; do not assume a personal filesystem path. Read `docs/development.md` and `docs/auto-fix.md` when running the workflow.

`scripts/auto_fix_issue.py` and `.github/label-policy.json` own eligibility, protected paths, validation, and publication sequencing. Do not recreate those rules in prose or silently change labels to bypass them. An explicit instruction to approve a specific eligible issue authorizes adding `auto-fix:approved`; a request to inspect an issue does not.

Start from a clean, current base and inspect open PRs and any existing task journal. Reconcile pending pushes or PR creation before retrying; do not blindly rerun an interrupted task. The wrapper refuses duplicate work and stops when planning does not classify the task as a safe fix.

## Commands

Run from the repository root using its frozen Python environment:

```sh
uv run --project packages/python python -m scripts.auto_fix_issue plan ISSUE_NUMBER \
  --prompt-out /tmp/typeflux-plan-prompt.md
```

For an authorized unattended run:

```sh
uv run --project packages/python python -m scripts.auto_fix_issue run ISSUE_NUMBER \
  --codex-plan-command 'codex exec --sandbox read-only {prompt_file}' \
  --codex-command 'codex exec --sandbox workspace-write {prompt_file}' \
  --comment-plan --push --create-pr
```

Add `--graph-capability capability.workflow-review` when it matches the issue. Unknown capabilities should use code search, not an invented graph ID. Never substitute obsolete `--plan`/`--full-auto` flags or a sandbox-bypass flag.

The wrapper runs the shared polyglot development checks, records task and execution evidence, and pushes only validated changes. Frozen setup needs Node 24, the pinned pnpm/uv versions, Chromium, and the isolated local Temporal test server; follow the maintained setup command rather than assembling ad hoc environments. No live-provider credentials are needed for the default profile.

## Boundaries and review

Planning preserves the worktree. Implementation receives only its required agent credential; validation must not receive publication credentials. Hosted write mode retains the protected `auto-fix` environment approval. Do not remove that control to repair an environment problem.

If the wrapper cannot run, diagnose the concrete failure. Continue independent inspection and preparation. Use a manual implementation path when already authorized for the task, explaining the fallback and preserving scope, protected-path review, verification, and PR hygiene. Do not impose a second approval solely because execution moved from a wrapper to manual work.

After PR creation, attach the PR to the Codex task, inspect required checks and reviews, and handle Bugbot findings with documented validation. A GitHub-token-created PR may require workflow approval; report its actual state instead of asserting CI can never trigger. Complete only when validation and required review are satisfied, or report the exact remaining blocker.
