from __future__ import annotations

import shlex

from scripts.auto_fix_issue import (
    DEFAULT_VALIDATION_COMMANDS,
    AutoFixPolicy,
    Issue,
    _command_invocation,
    branch_name,
    build_planning_prompt,
    changed_files_from_outputs,
    create_plan_comment,
    create_pr_body,
    evaluate_issue,
    protected_path_violations,
    slugify,
)

POLICY = AutoFixPolicy(
    required_label="auto-fix:approved",
    candidate_label="auto-fix:candidate",
    protected_path_override_label="auto-fix:allow-protected-paths",
    safe_signal_labels=frozenset(
        {
            "bug",
            "cleanup",
            "correctness",
            "documentation",
            "good first issue",
            "stability",
        }
    ),
    blocking_labels=frozenset(
        {
            "api-change",
            "architecture",
            "auto-fix:blocked",
            "changes-ergonomics",
            "human-review",
            "needs-design",
            "new-feature",
            "provider-semantics",
            "requires-design",
            "ux-change",
            "yaml-change",
        }
    ),
    protected_path_patterns=(".github/workflows/*", "pyproject.toml"),
)


def _issue(*labels: str) -> Issue:
    return Issue(
        number=8,
        title="Remove duplicate OSError branch in activity executor",
        body="",
        labels=frozenset(labels),
        url="https://example.test/issues/8",
    )


def test_evaluate_issue_requires_approval_label() -> None:
    eligibility = evaluate_issue(_issue("bug"), POLICY)

    assert not eligibility.ok
    assert eligibility.reasons == ("missing required label: auto-fix:approved",)


def test_evaluate_issue_rejects_blocking_labels() -> None:
    eligibility = evaluate_issue(_issue("auto-fix:approved", "bug", "yaml-change"), POLICY)

    assert not eligibility.ok
    assert eligibility.reasons == ("blocking label(s) present: yaml-change",)


def test_evaluate_issue_allows_approved_safe_bug() -> None:
    eligibility = evaluate_issue(_issue("auto-fix:approved", "bug"), POLICY)

    assert eligibility.ok
    assert eligibility.reasons == ()
    assert eligibility.warnings == ()


def test_evaluate_issue_warns_without_safe_signal() -> None:
    eligibility = evaluate_issue(_issue("auto-fix:approved"), POLICY)

    assert eligibility.ok
    assert eligibility.warnings


def test_slugify_and_branch_name_are_stable() -> None:
    assert (
        slugify("Only retry Langfuse trace list calls!") == "only-retry-langfuse-trace-list-calls"
    )
    assert (
        branch_name(_issue("auto-fix:approved", "bug"))
        == "codex/issue-8-remove-duplicate-oserror-branch-in-activity-exec"
    )


def test_protected_path_violations_can_be_overridden_by_label() -> None:
    paths = [".github/workflows/ci.yml", "src/typeflux/execution/executor.py"]

    assert protected_path_violations(paths, frozenset(), POLICY) == [".github/workflows/ci.yml"]
    assert (
        protected_path_violations(paths, frozenset({"auto-fix:allow-protected-paths"}), POLICY)
        == []
    )


def test_changed_files_from_outputs_includes_untracked_files_once() -> None:
    assert changed_files_from_outputs(
        "src/typeflux/execution/executor.py\nnew_test.py\n",
        "new_test.py\ntests/test_new_behavior.py\n",
    ) == [
        "src/typeflux/execution/executor.py",
        "new_test.py",
        "tests/test_new_behavior.py",
    ]


def test_legacy_codex_plan_command_uses_current_read_only_exec() -> None:
    invocation = _command_invocation("codex exec --plan {prompt_file}", "/tmp/prompt.md")

    assert invocation.command == ("codex", "exec", "--sandbox", "read-only")
    assert invocation.stdin_prompt


def test_legacy_codex_full_auto_command_uses_current_unattended_exec() -> None:
    invocation = _command_invocation("codex exec --full-auto {prompt_file}", "/tmp/prompt.md")

    assert invocation.command == (
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
    )
    assert invocation.stdin_prompt


def test_current_codex_exec_prompt_file_placeholder_is_piped_on_stdin() -> None:
    invocation = _command_invocation(
        "codex exec --sandbox read-only {prompt_file}",
        "/tmp/prompt.md",
    )

    assert invocation.command == ("codex", "exec", "--sandbox", "read-only")
    assert invocation.stdin_prompt


def test_non_codex_prompt_file_placeholder_is_preserved() -> None:
    invocation = _command_invocation("tool --prompt {prompt_file}", "/tmp/prompt.md")

    assert invocation.command == ("tool", "--prompt", "/tmp/prompt.md")
    assert not invocation.stdin_prompt


def test_build_planning_prompt_requires_compact_comment_shape() -> None:
    prompt = build_planning_prompt(_issue("auto-fix:approved", "bug"))

    assert "Return only this compact template" in prompt
    assert "under 12 nonblank lines" in prompt
    assert "Decision:" in prompt
    assert "Files/tests:" in prompt
    assert "Stop if:" in prompt


def test_create_plan_comment_uses_brief_audit_framing() -> None:
    comment = create_plan_comment(
        _issue("auto-fix:approved", "bug"),
        "Decision: safe automated fix\nPlan:\n- Update code\n- Run tests",
    )

    assert comment.startswith("## Auto-Fix Plan\n\nIssue #8. Recorded before implementation.")
    assert "Codex planning pass" not in comment
    assert "Decision: safe automated fix" in comment


def test_create_pr_body_uses_default_validation_commands() -> None:
    body = create_pr_body(_issue("auto-fix:approved", "bug"))

    for command in DEFAULT_VALIDATION_COMMANDS:
        assert f"- `{shlex.join(command)}`" in body
    assert "- `ruff check .`" not in body
