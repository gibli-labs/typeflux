# Automated Issue Resolution

This repository has a conservative auto-fix lane for issues that are safe to
resolve with Codex and review bots. It is intended for correctness, stability,
bug, cleanup, and narrow documentation/example fixes.

The central machine-readable label policy is
[`.github/label-policy.json`](../.github/label-policy.json). Agents should use
that file as the source of truth for label meanings and auto-fix eligibility.

It is not intended for public API changes, YAML schema changes, user-facing
workflow changes, provider behavior changes, or architecture work.

## Labels

An issue must have:

- `auto-fix:approved`
- at least one safe signal label such as `bug`, `correctness`, `stability`,
  `cleanup`, `documentation`, or `good first issue`

The automation refuses issues with any blocking label listed in
`.github/label-policy.json`, including:

- `auto-fix:blocked`
- `api-change`
- `yaml-change`
- `ux-change`
- `new-feature`
- `architecture`
- `needs-design`
- `requires-design`
- `changes-ergonomics`
- `provider-semantics`
- `human-review`

Use `auto-fix:candidate` while triaging. Replace it with
`auto-fix:approved` only when the issue is ready for automation.

Use `auto-fix:allow-protected-paths` only when an approved issue intentionally
needs to touch protected files such as workflow definitions, dependency
metadata, or the YAML schema.

## Manual Planning

Generate the Codex prompt and eligibility report locally:

```bash
uv run --project packages/python python scripts/auto_fix_issue.py plan 8 --prompt-out /tmp/typeflux-auto-fix.md
```

The GitHub Action named `Auto Fix Issue` also supports `plan` mode. Plan mode is
the recommended first run for newly approved issues.

To run a Codex planning pass and persist it as an issue comment:

```bash
uv run --project packages/python python scripts/auto_fix_issue.py plan 8 \
  --codex-plan-command "codex exec --plan {prompt_file}" \
  --plan-out /tmp/typeflux-auto-fix-plan.md \
  --comment
```

## Automated Run

Run mode requires a Codex command to be available in the runner environment.
The command can read the prompt from stdin, or it can include `{prompt_file}` to
receive a path to the generated prompt.

Example local shape:

```bash
uv run --project packages/python python scripts/auto_fix_issue.py run 8 \
  --codex-plan-command "codex exec --plan {prompt_file}" \
  --codex-command "codex exec --full-auto {prompt_file}" \
  --push \
  --create-pr
```

The wrapper will:

1. verify issue labels
2. refuse issues with open linked PRs
3. create a `codex/issue-<number>-<slug>` branch
4. run a Codex planning pass and fail if planning leaves a diff
5. persist the plan as an issue comment
6. run Codex implementation with the persisted plan included in the prompt
7. enforce changed-file limits and protected-path rules
8. run the standard validation suite
9. commit, push, and optionally open a PR

The standard validation suite is:

```bash
uv run --directory packages/python ruff check . ../../scripts
uv run --directory packages/python ruff format --check . ../../scripts
uv run --directory packages/python mypy src/typeflux
uv run --directory packages/python python -m build
uv run --directory packages/python pytest -q -m "not live"
```

## Review Bot Hygiene

The workflow intentionally opens a PR only after local validation passes. Cursor
Bugbot and other review bots should review complete diffs, not work in progress.

## Privileged run mode: environment protection

`run` mode executes behind the protected `auto-fix` environment. Before the
first dispatch, a repository admin must:

1. Create the `auto-fix` environment (Settings → Environments) with a
   **required reviewers** rule — every run-mode dispatch then waits for an
   explicit human approval, which the job verifies from its own approval
   record and refuses to proceed without.
2. Set the `CODEX_API_KEY` secret (environment-scoped; a repo-level copy
   additionally enables codex-backed planning).

Re-runs of an approved run are refused by design — dispatch a fresh run
instead. Plan mode is read-only and needs no environment.
