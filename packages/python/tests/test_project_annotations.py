"""Unit tests for the insight-acknowledgement annotations surface (#733 §1).

Covers the fail-closed parse (valid / absent / empty / malformed / schema-invalid),
the served projection shape (expiry served, nulls excluded), and the validation-issue
emission — a malformed file is an AUTHORING error on the validation surface, never an
enforcement verdict, and always with an EMPTY projection.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from typeflux.project import load_project_spec
from typeflux.project.annotations import (
    ProjectAnnotations,
    annotations_path,
    load_project_annotations,
    read_project_annotations,
)
from typeflux.project.loader import validate_project


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def _project(tmp_path: Path, annotations: str | None = None) -> Path:
    _write(tmp_path / "workflow.yaml", "placeholder: true\n")
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        """
        version: "1"
        name: annotations-demo
        workflows:
          - id: workflow
            path: workflow.yaml
        """,
    )
    if annotations is not None:
        _write(tmp_path / ".typeflux" / "annotations.yaml", annotations)
    return manifest


def test_annotations_path_is_dot_typeflux_beside_manifest(tmp_path: Path) -> None:
    project = load_project_spec(_project(tmp_path))
    assert annotations_path(project) == tmp_path / ".typeflux" / "annotations.yaml"


def test_valid_annotations_parse_in_file_order_with_optional_fields(tmp_path: Path) -> None:
    project = load_project_spec(
        _project(
            tmp_path,
            """
            annotations:
              - insight_id_pattern: "policy.drift.*"
                reason: tracked upstream
                tracked_in: "https://github.com/acme/infra/issues/412"
              - insight_id_pattern: runtime.pin.exact
                reason: accepted for the migration window
                expires: 2026-12-31
            """,
        )
    )
    projection = load_project_annotations(project)
    assert isinstance(projection, ProjectAnnotations)
    assert [a.insight_id_pattern for a in projection.annotations] == [
        "policy.drift.*",
        "runtime.pin.exact",
    ]
    first, second = projection.annotations
    assert first.tracked_in == "https://github.com/acme/infra/issues/412"
    assert first.expires is None
    # An expired entry is SERVED with its expiry (rendering is the console's job, slice 3).
    assert second.expires == date(2026, 12, 31)
    assert second.tracked_in is None
    # Absent optional fields are excluded from the served JSON (response_model_exclude_none).
    assert projection.model_dump(mode="json", exclude_none=True)["annotations"][0] == {
        "insight_id_pattern": "policy.drift.*",
        "reason": "tracked upstream",
        "tracked_in": "https://github.com/acme/infra/issues/412",
    }


def test_absent_file_is_empty_projection_not_an_error(tmp_path: Path) -> None:
    project = load_project_spec(_project(tmp_path, annotations=None))
    result = read_project_annotations(project)
    assert result.annotations.annotations == ()
    assert result.error is None
    # And no annotations validation issue for the common (absent) case.
    report = validate_project(project)
    assert not any(issue.code == "invalid_annotations_file" for issue in report.issues)


def test_empty_or_comments_only_file_is_empty_not_malformed(tmp_path: Path) -> None:
    project = load_project_spec(_project(tmp_path, "# only a comment\n"))
    result = read_project_annotations(project)
    assert result.annotations.annotations == ()
    assert result.error is None


def test_empty_annotations_list_is_valid(tmp_path: Path) -> None:
    project = load_project_spec(_project(tmp_path, "annotations: []\n"))
    result = read_project_annotations(project)
    assert result.annotations.annotations == ()
    assert result.error is None


@pytest.mark.parametrize(
    "body",
    [
        # Unparseable YAML.
        "annotations: [oops\n",
        # Top-level is a bare list, not the mapping schema.
        "- insight_id_pattern: x\n  reason: y\n",
        # Unknown top-level key (typo) — extra=forbid.
        "annotation:\n  - insight_id_pattern: x\n    reason: y\n",
        # Unknown entry key — extra=forbid.
        "annotations:\n  - insight_id_pattern: x\n    reason: y\n    note: nope\n",
        # Missing required reason.
        "annotations:\n  - insight_id_pattern: x\n",
        # Empty / untrimmed pattern.
        'annotations:\n  - insight_id_pattern: "  "\n    reason: y\n',
        # tracked_in is not an http(s) URL.
        "annotations:\n  - insight_id_pattern: x\n    reason: y\n    tracked_in: not-a-url\n",
        # expires is not a date.
        "annotations:\n  - insight_id_pattern: x\n    reason: y\n    expires: someday\n",
    ],
)
def test_malformed_file_is_empty_projection_and_validation_issue(tmp_path: Path, body: str) -> None:
    project = load_project_spec(_project(tmp_path, body))
    result = read_project_annotations(project)
    # Fail-closed: EMPTY projection, never a partial parse.
    assert result.annotations.annotations == ()
    assert result.error is not None
    # The malformed file surfaces as ONE validation issue (an authoring error) whose
    # path points at the offending file — NOT an enforcement verdict.
    report = validate_project(project)
    issues = [issue for issue in report.issues if issue.code == "invalid_annotations_file"]
    assert len(issues) == 1
    assert not report.ok
    assert issues[0].path is not None
    assert issues[0].path.endswith(".typeflux/annotations.yaml")


def test_https_and_http_tracked_in_both_accepted(tmp_path: Path) -> None:
    project = load_project_spec(
        _project(
            tmp_path,
            """
            annotations:
              - insight_id_pattern: a
                reason: r
                tracked_in: "http://issues.example.com/1"
              - insight_id_pattern: b
                reason: r
                tracked_in: "https://issues.example.com/2"
            """,
        )
    )
    result = read_project_annotations(project)
    assert result.error is None
    assert len(result.annotations.annotations) == 2
