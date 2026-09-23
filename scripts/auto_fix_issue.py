#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Mirrors CI's Quality-gates job (which runs from packages/python) — kept
# runnable from the REPO ROOT via --directory, since the automation itself
# needs root-relative paths (.github/label-policy.json, git plumbing).
DEFAULT_VALIDATION_COMMANDS = (
    ("uv", "run", "--directory", "packages/python", "ruff", "check", ".", "../../scripts"),
    (
        "uv",
        "run",
        "--directory",
        "packages/python",
        "ruff",
        "format",
        "--check",
        ".",
        "../../scripts",
    ),
    ("uv", "run", "--directory", "packages/python", "mypy", "src/typeflux"),
    ("uv", "run", "--directory", "packages/python", "python", "-m", "build"),
    ("uv", "run", "--directory", "packages/python", "pytest", "-q", "-m", "not live"),
)

LABEL_POLICY_PATH = Path(".github/label-policy.json")
MAX_ISSUE_COMMENT_CHARS = 60000


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: frozenset[str]
    url: str


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoFixPolicy:
    required_label: str
    candidate_label: str
    protected_path_override_label: str
    safe_signal_labels: frozenset[str]
    blocking_labels: frozenset[str]
    protected_path_patterns: tuple[str, ...]


@dataclass(frozen=True)
class CommandInvocation:
    command: tuple[str, ...]
    stdin_prompt: bool


def load_auto_fix_policy(cwd: Path) -> AutoFixPolicy:
    payload = json.loads((cwd / LABEL_POLICY_PATH).read_text())
    auto_fix = payload["auto_fix"]
    return AutoFixPolicy(
        required_label=str(auto_fix["required_label"]),
        candidate_label=str(auto_fix["candidate_label"]),
        protected_path_override_label=str(auto_fix["protected_path_override_label"]),
        safe_signal_labels=frozenset(str(label) for label in auto_fix["safe_signal_labels"]),
        blocking_labels=frozenset(str(label) for label in auto_fix["blocking_labels"]),
        protected_path_patterns=tuple(
            str(pattern) for pattern in auto_fix["protected_path_patterns"]
        ),
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:48].strip("-") or "issue"


def branch_name(issue: Issue, *, prefix: str = "codex/") -> str:
    return f"{prefix}issue-{issue.number}-{slugify(issue.title)}"


def evaluate_issue(issue: Issue, policy: AutoFixPolicy) -> Eligibility:
    reasons: list[str] = []
    warnings: list[str] = []
    labels = issue.labels

    if policy.required_label not in labels:
        reasons.append(f"missing required label: {policy.required_label}")

    blocked = sorted(labels & policy.blocking_labels)
    if blocked:
        reasons.append(f"blocking label(s) present: {', '.join(blocked)}")

    safe_signals = sorted(labels & policy.safe_signal_labels)
    if not safe_signals:
        warnings.append(
            "no safe-signal label present; expected one of: "
            + ", ".join(sorted(policy.safe_signal_labels))
        )

    return Eligibility(ok=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def protected_path_violations(
    paths: Sequence[str], labels: set[str] | frozenset[str], policy: AutoFixPolicy
) -> list[str]:
    if policy.protected_path_override_label in labels:
        return []
    return [
        path
        for path in paths
        if any(fnmatch.fnmatch(path, pattern) for pattern in policy.protected_path_patterns)
    ]


def _issue_context(issue: Issue) -> str:
    labels = ", ".join(sorted(issue.labels)) or "(none)"
    return f"""GitHub issue #{issue.number}: {issue.title}
URL: {issue.url}
Labels: {labels}

Issue body:
---
{issue.body.strip() or "(no body provided)"}
---
"""


def build_planning_prompt(issue: Issue) -> str:
    return f"""You are Codex working in the typeflux repository.

Prepare an implementation plan for this issue. Do not edit files.

{_issue_context(issue)}

Planning requirements:
- Decide whether this belongs in the automated safe-fix lane.
- Identify the smallest safe change that resolves the issue.
- Name the likely files and tests to touch.
- Call out any reason to stop instead of implementing, especially public API changes, YAML schema changes, UX/ergonomics changes, provider semantics changes, dependency strategy changes, or broad architecture decisions.
- Keep the plan concise and implementation-ready.

Return only this compact template, with the entire response under 12 nonblank lines:

Decision: <safe automated fix | stop, needs human review>
Plan:
- <smallest implementation step>
- <test/validation step>
Files/tests: <likely files and tests>
Stop if: <blocking condition, or "none known">
"""


def build_prompt(issue: Issue, *, codex_plan: str | None = None) -> str:
    validation_commands = "\n".join(
        " - " + shlex.join(command) for command in DEFAULT_VALIDATION_COMMANDS
    )
    plan_section = ""
    if codex_plan:
        plan_section = f"""
Codex planning pass:
---
{codex_plan.strip()}
---
"""
    return f"""You are Codex working in the typeflux repository.

Resolve this issue using the already persisted planning pass.

{_issue_context(issue)}
{plan_section}

Automation lane rules:
- Only proceed if this is a correctness, stability, bug, cleanup, or narrow documentation/example fix.
- Stop without editing if the fix requires a public API change, YAML schema change, new ergonomics, new feature design, dependency strategy change, or broad architecture decision.
- Keep the diff tightly scoped to the issue.
- Preserve backward compatibility unless the issue explicitly says otherwise.
- Add or update focused tests when behavior changes.
- Do not edit generated artifacts, caches, dist/build outputs, or environment files.
- Do not open a PR yourself; the automation wrapper handles validation, commit, push, and PR creation after you finish.

Repository validation expected after implementation:
{validation_commands}
"""


#: Absolute paths of the tools this wrapper trusts, resolved at IMPORT time —
#: before any generated code runs — so a shim later planted on a writable PATH
#: entry (e.g. the gitignored .venv/bin) is never picked up.
_TRUSTED_EXECUTABLES: dict[str, str] = {
    name: path for name in ("git", "gh", "uv", "codex") if (path := shutil.which(name)) is not None
}


def _sanitized_path(repo_root: Path) -> str:
    # ALLOWLISTED trusted PATH: only the directories of the import-time-pinned
    # tools (ordered so each tool's own directory is searched before any
    # other) plus the immutable system bins. Exclusion-based filtering is not
    # enough — generated code can drop shims in other user-writable PATH
    # entries (tool caches, ~/.local/bin).
    del repo_root  # retained for call-site clarity; allowlist ignores it
    ordered: list[str] = []
    for name in ("git", "gh", "uv"):
        exe = _TRUSTED_EXECUTABLES.get(name)
        if exe:
            d = str(Path(exe).parent)
            if d not in ordered:
                ordered.append(d)
    for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if d not in ordered:
            ordered.append(d)
    return os.pathsep.join(ordered)


def _resolve_trusted(command: Sequence[str]) -> tuple[str, ...]:
    head = _TRUSTED_EXECUTABLES.get(command[0], command[0])
    return (head, *command[1:])


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _resolve_trusted(command)
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        check=False,
        env=env,
    )


def run_with_output(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        _resolve_trusted(command),
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}"
        )
    return result.stdout


def repo_config_digest(cwd: Path) -> str:
    """Digest of the repo-local + worktree git config (include-resolved).

    Config listing executes nothing, so this is safe as the FIRST git
    operation after untrusted code has run.
    """
    listing = run_with_output(("git", "config", "--local", "--list", "--includes"), cwd=cwd)
    # The worktree scope (extensions.worktreeConfig) is consumed by every git
    # command too; --local does not list it, so digest it explicitly. Without
    # the extension the command fails — fold the return code into the digest
    # so ENABLING the extension also trips it.
    worktree_result = subprocess.run(
        _resolve_trusted(("git", "config", "--worktree", "--list", "--includes")),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    worktree_state = f"{worktree_result.returncode}:{worktree_result.stdout}"
    return hashlib.sha256((listing + "\x00" + worktree_state).encode()).hexdigest()


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = run(command, cwd=cwd, input_text=input_text, env=env)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(command)}")


def gh_json(args: Sequence[str], *, cwd: Path) -> object:
    result = subprocess.run(
        ("gh", *args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"gh command failed: {' '.join(args)}")
    return json.loads(result.stdout)


def fetch_issue(issue_number: int, *, cwd: Path) -> Issue:
    payload = gh_json(
        (
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,title,body,labels,url",
        ),
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        raise SystemExit("unexpected gh issue response")
    labels = payload.get("labels", [])
    return Issue(
        number=int(payload["number"]),
        title=str(payload["title"]),
        body=str(payload.get("body") or ""),
        labels=frozenset(str(label["name"]) for label in labels),
        url=str(payload["url"]),
    )


def open_issue_prs(issue_number: int, *, cwd: Path) -> list[str]:
    payload = gh_json(
        (
            "pr",
            "list",
            "--state",
            "open",
            "--search",
            f"#{issue_number} in:body",
            "--json",
            "number,url",
        ),
        cwd=cwd,
    )
    if not isinstance(payload, list):
        raise SystemExit("unexpected gh pr list response")
    return [str(item["url"]) for item in payload if isinstance(item, dict) and "url" in item]


def ensure_clean_worktree(cwd: Path) -> None:
    result = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "git status failed")
    if result.stdout.strip():
        raise SystemExit("working tree is not clean; refusing to start auto-fix run")


def ensure_no_worktree_changes(cwd: Path, *, context: str) -> None:
    result = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "git status failed")
    if result.stdout.strip():
        raise SystemExit(f"{context} left worktree changes; refusing to continue")


def changed_files(cwd: Path) -> list[str]:
    diff_result = subprocess.run(
        # --no-renames: a staged rename must yield BOTH the deleted source and
        # the new destination, so state snapshots and the staged-tree rebuild
        # see the complete change set.
        _resolve_trusted(("git", "diff", "--name-only", "--no-renames", "HEAD")),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if diff_result.returncode != 0:
        raise SystemExit(diff_result.stderr.strip() or "git diff failed")
    untracked_result = subprocess.run(
        _resolve_trusted(("git", "ls-files", "--others", "--exclude-standard")),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if untracked_result.returncode != 0:
        raise SystemExit(untracked_result.stderr.strip() or "git ls-files failed")
    return changed_files_from_outputs(diff_result.stdout, untracked_result.stdout)


def changed_files_from_outputs(diff_output: str, untracked_output: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for output in (diff_output, untracked_output):
        for line in output.splitlines():
            path = line.strip()
            if path and path not in seen:
                files.append(path)
                seen.add(path)
    return files


#: Credential shapes the VALIDATION subprocesses must never inherit:
#: validation executes freshly generated repository code (tests, build
#: hooks), and a prompt-injected change could exfiltrate anything in its
#: environment — including runner service tokens (ACTIONS_*) that authorize
#: cache/artifact writes.
_VALIDATION_ENV_DENY_SUBSTRINGS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")
_VALIDATION_ENV_DENY_PREFIXES = ("ACTIONS_", "INPUT_")
#: Workflow command files: writes to these ($GITHUB_ENV/$GITHUB_PATH/outputs)
#: let generated code inject env/PATH into LATER runner steps — a step-
#: boundary escape, not just an env read.
_VALIDATION_ENV_DENY_EXACT = (
    "GITHUB_ENV",
    "GITHUB_PATH",
    "GITHUB_OUTPUT",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
)


def _is_denied_env(name: str) -> bool:
    upper = name.upper()
    return (
        upper in _VALIDATION_ENV_DENY_EXACT
        or upper.startswith(_VALIDATION_ENV_DENY_PREFIXES)
        or any(fragment in upper for fragment in _VALIDATION_ENV_DENY_SUBSTRINGS)
    )


def run_validation(cwd: Path) -> None:
    scrubbed = {k: v for k, v in os.environ.items() if not _is_denied_env(k)}
    # The project venv is agent-writable (gitignored): DELETE it and recreate
    # from the lockfile so nothing generated code planted survives —
    # --reinstall alone leaves unowned files (.pth hooks, shims) in place.
    # A symlinked .venv (pointing at a prebuilt poisoned env) is unlinked, and
    # anything left behind fails closed.
    venv_path = cwd / "packages" / "python" / ".venv"
    if venv_path.is_symlink():
        venv_path.unlink()
    elif venv_path.exists():
        shutil.rmtree(venv_path)
    if venv_path.exists() or venv_path.is_symlink():
        raise SystemExit(f"could not fully remove {venv_path} before validation")
    run_checked(
        (
            "uv",
            "sync",
            "--frozen",
            "--extra",
            "live",
            "--group",
            "dev",
            "--project",
            "packages/python",
        ),
        cwd=cwd,
        env=scrubbed,
    )
    for command in DEFAULT_VALIDATION_COMMANDS:
        run_checked(command, cwd=cwd, env=scrubbed)


def _command_invocation(command_template: str, prompt_path: str) -> CommandInvocation:
    uses_prompt_file = "{prompt_file}" in command_template
    command = tuple(
        part.replace("{prompt_file}", prompt_path) for part in shlex.split(command_template)
    )
    if _is_codex_exec(command):
        return _codex_exec_invocation(command, prompt_path, uses_prompt_file=uses_prompt_file)
    return CommandInvocation(command=command, stdin_prompt=not uses_prompt_file)


def _is_codex_exec(command: Sequence[str]) -> bool:
    return len(command) >= 2 and command[0] == "codex" and command[1] == "exec"


def _codex_exec_invocation(
    command: Sequence[str],
    prompt_path: str,
    *,
    uses_prompt_file: bool,
) -> CommandInvocation:
    normalized: list[str] = []
    saw_plan = False
    saw_full_auto = False
    for part in command:
        if part == "--plan":
            saw_plan = True
            continue
        if part == "--full-auto":
            saw_full_auto = True
            continue
        if uses_prompt_file and part == prompt_path:
            continue
        normalized.append(part)

    if saw_plan and not _codex_exec_has_sandbox_mode(normalized):
        normalized.extend(("--sandbox", "read-only"))
    if saw_full_auto and "--dangerously-bypass-approvals-and-sandbox" not in normalized:
        normalized.append("--dangerously-bypass-approvals-and-sandbox")

    return CommandInvocation(command=tuple(normalized), stdin_prompt=True)


def _codex_exec_has_sandbox_mode(command: Sequence[str]) -> bool:
    return (
        "--sandbox" in command
        or "-s" in command
        or "--dangerously-bypass-approvals-and-sandbox" in command
    )


def run_codex(command_template: str, *, cwd: Path, prompt: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as prompt_file:
        prompt_file.write(prompt)
        prompt_path = prompt_file.name
    try:
        invocation = _command_invocation(command_template, prompt_path)
        run_checked(
            invocation.command,
            cwd=cwd,
            input_text=(prompt if invocation.stdin_prompt else None),
        )
    finally:
        Path(prompt_path).unlink(missing_ok=True)


def run_codex_capture(command_template: str, *, cwd: Path, prompt: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as prompt_file:
        prompt_file.write(prompt)
        prompt_path = prompt_file.name
    last_message_path: str | None = None
    wrapper_last_message: str | None = None
    try:
        invocation = _command_invocation(command_template, prompt_path)
        command = invocation.command
        # A caller-provided output option is honored by READING that file;
        # otherwise the wrapper appends its own. Only codex-exec commands are
        # scanned — `-o` may mean anything else to a custom planner.
        caller_last_message: str | None = None
        if _is_codex_exec(command):
            for i, part in enumerate(command):
                if part in ("--output-last-message", "-o") and i + 1 < len(command):
                    caller_last_message = command[i + 1]
                elif part.startswith("--output-last-message="):
                    caller_last_message = part.split("=", 1)[1]
                elif part.startswith("-o") and part != "-o":
                    # Attached short forms: -oFILE and -o=FILE.
                    caller_last_message = part[2:].lstrip("=")
        if caller_last_message is not None:
            # Codex resolves a relative output path against ITS cwd — mirror it.
            last_message_path = str((cwd / caller_last_message).resolve())
        if _is_codex_exec(command) and caller_last_message is None:
            # Capture ONLY the agent's final message: combined stdout/stderr
            # carries banners, tool traces, and token diagnostics that bloat
            # plan comments and pollute the implementation prompt.
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as last:
                wrapper_last_message = last.name
            last_message_path = wrapper_last_message
            command = (*command, "--output-last-message", last_message_path)
        combined = run_with_output(
            command,
            cwd=cwd,
            input_text=(prompt if invocation.stdin_prompt else None),
        )
        if last_message_path is not None and Path(last_message_path).is_file():
            last_text = Path(last_message_path).read_text().strip()
            if last_text:
                return last_text
        return combined
    finally:
        Path(prompt_path).unlink(missing_ok=True)
        if wrapper_last_message is not None:
            Path(wrapper_last_message).unlink(missing_ok=True)


def create_plan_comment(issue: Issue, codex_plan: str) -> str:
    plan = codex_plan.strip() or "(plan command produced no stdout)"
    body = f"""## Auto-Fix Plan

Issue #{issue.number}. Recorded before implementation.

```text
{plan}
```
"""
    if len(body) > MAX_ISSUE_COMMENT_CHARS:
        body = body[: MAX_ISSUE_COMMENT_CHARS - 80] + "\n```\n\n_Comment truncated._\n"
    return body


def comment_on_issue(issue: Issue, body: str, *, cwd: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as body_file:
        body_file.write(body)
        body_path = body_file.name
    try:
        run_checked(
            ("gh", "issue", "comment", str(issue.number), "--body-file", body_path), cwd=cwd
        )
    finally:
        Path(body_path).unlink(missing_ok=True)


def create_pr_body(issue: Issue) -> str:
    tests_run = "\n".join(f"- `{shlex.join(command)}`" for command in DEFAULT_VALIDATION_COMMANDS)
    return f"""## Summary

- Automated safe-fix lane for #{issue.number}

## Linked Issue

Closes #{issue.number}

## Risk / Architecture Impact

- Intended to be a correctness/stability cleanup with no public API, YAML schema, or UX changes.

## Tests Run

{tests_run}

## Live Validation

- Not run; non-live change.

## Secrets / Data Handling

- No secrets or sensitive data added.
"""


def command_plan(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    issue = fetch_issue(args.issue_number, cwd=cwd)
    policy = load_auto_fix_policy(cwd)
    eligibility = evaluate_issue(issue, policy)
    planning_prompt = build_planning_prompt(issue)

    if args.prompt_out:
        Path(args.prompt_out).write_text(planning_prompt)

    codex_plan = ""
    if args.codex_plan_command:
        ensure_no_worktree_changes(cwd, context="Codex planning preflight")
        codex_plan = run_codex_capture(args.codex_plan_command, cwd=cwd, prompt=planning_prompt)
        ensure_no_worktree_changes(cwd, context="Codex planning pass")
        if args.plan_out:
            Path(args.plan_out).write_text(codex_plan)
        if args.comment:
            comment_on_issue(issue, create_plan_comment(issue, codex_plan), cwd=cwd)

    print(f"Issue: #{issue.number} {issue.title}")
    print(f"URL: {issue.url}")
    print(f"Labels: {', '.join(sorted(issue.labels)) or '(none)'}")
    print(f"Eligible: {'yes' if eligibility.ok else 'no'}")
    for reason in eligibility.reasons:
        print(f"Blocker: {reason}")
    for warning in eligibility.warnings:
        print(f"Warning: {warning}")
    print(f"Branch: {branch_name(issue, prefix=args.branch_prefix)}")
    if codex_plan:
        print("Codex plan persisted: yes")
    return 0 if eligibility.ok else 2


def command_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    issue = fetch_issue(args.issue_number, cwd=cwd)
    policy = load_auto_fix_policy(cwd)
    eligibility = evaluate_issue(issue, policy)
    if not eligibility.ok:
        for reason in eligibility.reasons:
            print(f"Blocker: {reason}", file=sys.stderr)
        return 2

    existing_prs = open_issue_prs(issue.number, cwd=cwd)
    if existing_prs:
        print("Open PR already appears linked to this issue:", file=sys.stderr)
        for url in existing_prs:
            print(f" - {url}", file=sys.stderr)
        return 2

    codex_command = args.codex_command or os.environ.get("AUTO_FIX_CODEX_COMMAND")
    if not codex_command:
        raise SystemExit(
            "missing Codex command; pass --codex-command or set AUTO_FIX_CODEX_COMMAND"
        )
    codex_plan_command = args.codex_plan_command or os.environ.get("AUTO_FIX_CODEX_PLAN_COMMAND")
    if not codex_plan_command:
        raise SystemExit(
            "missing Codex plan command; pass --codex-plan-command or set "
            "AUTO_FIX_CODEX_PLAN_COMMAND"
        )

    ensure_clean_worktree(cwd)
    branch = branch_name(issue, prefix=args.branch_prefix)
    run_checked(("git", "switch", "-c", branch), cwd=cwd)

    planning_prompt = build_planning_prompt(issue)
    codex_plan = run_codex_capture(codex_plan_command, cwd=cwd, prompt=planning_prompt)
    ensure_no_worktree_changes(cwd, context="Codex planning pass")
    if args.plan_out:
        Path(args.plan_out).write_text(codex_plan)
    if args.comment_plan:
        comment_on_issue(issue, create_plan_comment(issue, codex_plan), cwd=cwd)

    prompt = build_prompt(issue, codex_plan=codex_plan)
    if args.prompt_out:
        Path(args.prompt_out).write_text(prompt)

    # Baseline the repo config BEFORE the implementation pass: a generated
    # change to .git/config (fsmonitor, filters, helpers, remote rewrite)
    # must never become the accepted baseline. Verified as the FIRST git
    # operation after Codex returns — config listing executes nothing.
    pre_codex_config = repo_config_digest(cwd)

    run_codex(codex_command, cwd=cwd, prompt=prompt)

    if repo_config_digest(cwd) != pre_codex_config:
        raise SystemExit(
            "repo-local git config changed during the implementation pass — refusing to continue"
        )

    files = changed_files(cwd)
    if not files:
        raise SystemExit("Codex completed without producing a diff")
    if len(files) > args.max_changed_files:
        raise SystemExit(
            f"changed file count {len(files)} exceeds limit {args.max_changed_files}: "
            + ", ".join(files)
        )
    protected = protected_path_violations(files, issue.labels, policy)
    if protected:
        raise SystemExit(
            f"protected path changes require {policy.protected_path_override_label}: "
            + ", ".join(protected)
        )

    # Generated code must not smuggle execution into the trusted phase.
    # Neutralize repo-local git hooks before validation, snapshot the repo
    # config, and after validation (which executes generated code) verify the
    # config is byte-identical and point hooksPath at a FRESH directory the
    # generated code never saw. A tampered config (new filters, fsmonitor,
    # credential helpers, ...) fails closed. The prior hooksPath is restored
    # for local runs in the finally block.
    # Capture the LOCAL scope only: that is the only scope we mutate, so it is
    # the only scope restoration may touch (global/worktree values stay put).
    # Presence and value are captured separately so an explicitly EMPTY value
    # is restored as empty, not unset.
    previous_hooks_result = subprocess.run(
        _resolve_trusted(("git", "config", "--local", "core.hooksPath")),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    previous_hooks_present = previous_hooks_result.returncode == 0
    previous_hooks = previous_hooks_result.stdout.rstrip("\n") if previous_hooks_present else ""
    hooks_dir = Path(tempfile.mkdtemp(prefix="no-hooks-"))

    def local_config_digest() -> str:
        return repo_config_digest(cwd)

    # The trusted phase ignores global/system config entirely: generated code
    # can write $HOME/.gitconfig (filters, fsmonitor, helpers), so post-
    # validation git commands read a FRESH global file and no system file.
    # The caller's identity is captured BEFORE any generated code runs and
    # seeded into that file so commit/--signoff keep the right author.
    identity_name = run_with_output(
        ("git", "config", "--default", "", "user.name"), cwd=cwd
    ).strip()
    identity_email = run_with_output(
        ("git", "config", "--default", "", "user.email"), cwd=cwd
    ).strip()

    def make_trusted_env() -> dict[str, str]:
        # Created ONLY when called — after validation — so generated code never
        # sees the file it could tamper with. Values are written through git's
        # own config writer (proper escaping for quotes/#/; in identities).
        trusted_global = Path(tempfile.mkdtemp(prefix="trusted-git-")) / "gitconfig"
        trusted_global.touch()
        if identity_name:
            run_checked(
                ("git", "config", "--file", str(trusted_global), "user.name", identity_name),
                cwd=cwd,
            )
        if identity_email:
            run_checked(
                ("git", "config", "--file", str(trusted_global), "user.email", identity_email),
                cwd=cwd,
            )
        return {
            **os.environ,
            "PATH": _sanitized_path(cwd),
            "GIT_CONFIG_GLOBAL": str(trusted_global),
            "GIT_CONFIG_SYSTEM": os.devnull,
        }

    # Hooks in the trusted phase resolve against a path no process can
    # populate (a non-directory): immune to a lingering validation daemon
    # racing a fresh temp directory.
    no_hooks = ("-c", f"core.hooksPath={os.devnull}")

    def staged_tree_id(env: dict[str, str]) -> str:
        # What WOULD be committed (blob contents after clean filters, plus
        # mode bits), computed against a throwaway index so nothing real moves.
        index_file = Path(tempfile.mkdtemp(prefix="stage-check-")) / "index"
        stage_env = {**env, "GIT_INDEX_FILE": str(index_file)}
        run_checked(("git", *no_hooks, "read-tree", "HEAD"), cwd=cwd, env=stage_env)
        run_checked(("git", *no_hooks, "add", "--", *files), cwd=cwd, env=stage_env)
        return run_with_output(("git", "write-tree"), cwd=cwd, env=stage_env).strip()

    try:
        run_checked(("git", "config", "core.hooksPath", str(hooks_dir)), cwd=cwd)
        config_digest = local_config_digest()

        # Repository-state snapshot: validation may not move HEAD, touch the
        # index, or change WHICH files are dirty — the reviewed change set was
        # fixed before validation ran.
        def candidate_hashes(env: dict[str, str] | None = None) -> dict[str, str]:
            # Deleted/renamed-away candidates get a sentinel instead of a
            # hash-object failure.
            hashes: dict[str, str] = {}
            for f in files:
                if (cwd / f).is_file():
                    hashes[f] = run_with_output(
                        ("git", "hash-object", "--", f), cwd=cwd, env=env
                    ).strip()
                else:
                    hashes[f] = "DELETED"
            return hashes

        # Both snapshots use equivalently isolated config so caller-side
        # global/system settings (e.g. status.showUntrackedFiles) cannot make
        # before/after diverge for reasons unrelated to validation.
        pre_env = make_trusted_env()
        head_before = run_with_output(
            ("git", *no_hooks, "rev-parse", "HEAD"), cwd=cwd, env=pre_env
        ).strip()
        symbolic_before = run_with_output(
            ("git", *no_hooks, "symbolic-ref", "-q", "HEAD"), cwd=cwd, env=pre_env
        ).strip()
        status_before = run_with_output(
            ("git", *no_hooks, "status", "--porcelain", "--untracked-files=all"),
            cwd=cwd,
            env=pre_env,
        )
        # Porcelain status keeps the same code when contents are rewritten in
        # place — hash the candidate files' BYTES so what gets committed is
        # exactly what validation ran against.
        content_before = candidate_hashes(pre_env)
        tree_before = staged_tree_id(pre_env)

        run_validation(cwd)

        # ORDER MATTERS after generated code has run: the config-integrity
        # check comes FIRST (config listing executes nothing), because a
        # tampered local config could hang a malicious fsmonitor/filter on any
        # other git command. Everything after runs config-isolated.
        if local_config_digest() != config_digest:
            raise SystemExit(
                "repo-local git config changed during validation — refusing to "
                "continue (generated code may have installed filters/hooks/helpers)"
            )
        post_env = make_trusted_env()
        head_after = run_with_output(
            ("git", *no_hooks, "rev-parse", "HEAD"), cwd=cwd, env=post_env
        ).strip()
        if head_after != head_before:
            raise SystemExit(
                f"HEAD moved during validation ({head_before} -> {head_after}) — "
                "generated code created commits; refusing to continue"
            )
        symbolic_after = run_with_output(
            ("git", *no_hooks, "symbolic-ref", "-q", "HEAD"), cwd=cwd, env=post_env
        ).strip()
        if symbolic_after != symbolic_before:
            raise SystemExit(
                f"HEAD ref changed during validation ({symbolic_before} -> {symbolic_after}) — "
                "refusing to continue"
            )
        status_after = run_with_output(
            ("git", *no_hooks, "status", "--porcelain", "--untracked-files=all"),
            cwd=cwd,
            env=post_env,
        )
        if status_after != status_before:
            raise SystemExit(
                "worktree/index state changed during validation — the change set "
                "no longer matches what was checked; refusing to continue\n"
                f"before:\n{status_before}\nafter:\n{status_after}"
            )
        content_after = candidate_hashes(post_env)
        if content_after != content_before:
            changed = sorted(f for f in files if content_before[f] != content_after[f])
            raise SystemExit(
                "candidate file contents changed during validation — the bytes to "
                f"commit are not the bytes that were validated: {changed}"
            )
        trusted_env = make_trusted_env()
        # Staged-tree identity: covers clean-filter output and mode bits that
        # raw content hashes cannot see.
        tree_after = staged_tree_id(trusted_env)
        if tree_after != tree_before:
            raise SystemExit(
                f"staged tree changed during validation ({tree_before} -> {tree_after}) — "
                "the commit would not match what was validated"
            )
        run_checked(("git", *no_hooks, "add", "--", *files), cwd=cwd, env=trusted_env)
        staged_now = run_with_output(("git", "write-tree"), cwd=cwd, env=trusted_env).strip()
        if staged_now != tree_before:
            raise SystemExit(
                f"real index tree {staged_now} != verified tree {tree_before} — refusing to commit"
            )
        # --signoff: the DCO gate (.github/workflows/dco.yml) requires every PR
        # commit to carry a Signed-off-by matching the configured author identity.
        run_checked(
            ("git", *no_hooks, "commit", "--signoff", "-m", f"Resolve issue #{issue.number}"),
            cwd=cwd,
            env=trusted_env,
        )

        if args.push:
            # The checkout runs with persist-credentials: false so validation of
            # generated code has no on-disk credential; authenticate git only now,
            # in the trusted post-validation phase (the credential helper lands
            # in the trusted global file, invisible outside this phase).
            run_checked(("gh", "auth", "setup-git"), cwd=cwd, env=trusted_env)
            # Push the VERIFIED commit by sha, not the mutable local branch
            # ref — validation cannot redirect what lands on the remote.
            verified_head = run_with_output(
                ("git", *no_hooks, "rev-parse", "HEAD"), cwd=cwd, env=trusted_env
            ).strip()
            run_checked(
                (
                    "git",
                    *no_hooks,
                    "push",
                    "-u",
                    "origin",
                    f"{verified_head}:refs/heads/{branch}",
                ),
                cwd=cwd,
                env=trusted_env,
            )
    finally:
        # Local runs keep their normal hooks once the wrapper is done —
        # restore exactly the local-scope state we found (an explicitly empty
        # value stays an explicitly empty value).
        if previous_hooks_present:
            run(("git", "config", "--local", "core.hooksPath", previous_hooks), cwd=cwd)
        else:
            run(("git", "config", "--local", "--unset", "core.hooksPath"), cwd=cwd)
    if args.create_pr:
        if not args.push:
            raise SystemExit("--create-pr requires --push")
        pr_args = [
            "gh",
            "pr",
            "create",
            # The verified push was sha-addressed, so no upstream tracking was
            # configured — name the head branch explicitly.
            "--head",
            branch,
            "--title",
            f"Resolve issue #{issue.number}: {issue.title}",
            "--body",
            create_pr_body(issue),
        ]
        if args.draft_pr:
            pr_args.append("--draft")
        # Still the trusted phase: gh spawns git and carries the write token,
        # so it must see the sanitized PATH and isolated config too.
        run_checked(tuple(pr_args), cwd=cwd, env=make_trusted_env())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".", help="Repository root")
    subparsers = parser.add_subparsers(required=True)

    plan = subparsers.add_parser("plan", help="Check eligibility and write the Codex prompt")
    plan.add_argument("issue_number", type=int)
    plan.add_argument("--branch-prefix", default="codex/")
    plan.add_argument("--prompt-out")
    plan.add_argument("--codex-plan-command")
    plan.add_argument("--plan-out")
    plan.add_argument("--comment", action="store_true")
    plan.set_defaults(func=command_plan)

    run_parser = subparsers.add_parser("run", help="Run Codex, validate, commit, and optionally PR")
    run_parser.add_argument("issue_number", type=int)
    run_parser.add_argument("--branch-prefix", default="codex/")
    run_parser.add_argument("--codex-command")
    run_parser.add_argument("--codex-plan-command")
    run_parser.add_argument("--prompt-out")
    run_parser.add_argument("--plan-out")
    run_parser.add_argument("--comment-plan", action="store_true")
    run_parser.add_argument("--max-changed-files", type=int, default=8)
    run_parser.add_argument("--push", action="store_true")
    run_parser.add_argument("--create-pr", action="store_true")
    run_parser.add_argument("--draft-pr", action="store_true")
    run_parser.set_defaults(func=command_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
