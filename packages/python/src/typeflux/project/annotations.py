"""Insight acknowledgement annotations (#733 §1, #577): an in-repo, PR-reviewed
record of which console insights have been acknowledged/suppressed, and why.

The maintainer decision (#577, 2026-07-17) is that ack/suppression state lives in
the repo — git stays the single source of truth, PR-reviewed like everything else;
there is NO console-side or control-plane-side mutable state. This module is the
read side of that decision: it parses ``.typeflux/annotations.yaml`` (beside the
project manifest) into a normalized, contract-shaped projection the read tier
serves, and it feeds the validation surface so a malformed file is an authoring
error rather than a silent drop.

Shape of the file (a MAPPING, like every other Typeflux YAML — extensible, and its
``annotations`` field mirrors the served projection envelope exactly)::

    annotations:
      - insight_id_pattern: "policy.drift.*"     # exact id or glob
        reason: "tracked upstream; not actionable until the provider ships the fix"
        tracked_in: "https://github.com/acme/infra/issues/412"   # optional issue URL
        expires: 2026-12-31                                       # optional ISO date

* ``insight_id_pattern`` matches the console's stable insight ids (exact or glob);
  it is required and must be a non-empty, trimmed string.
* ``reason`` is required (audit honesty: an ack without a reason is not an ack).
* ``tracked_in`` is optional and, when present, must be an http(s) URL — the SHAPE
  is validated (scheme + host), never fetched (no network in the read tier).
* ``expires`` is an optional ISO date. An expired entry is STILL SERVED with its
  expiry (rendering — the "stale ack" insight — is the console's job, slice 3); the
  projection only carries what the console needs to derive staleness.

Parsing is **fail-closed** (never partial/silent). Three outcomes:

* **absent file** → an EMPTY projection, NOT an error (the common case — most
  projects acknowledge nothing).
* **valid file** → the parsed annotations, in file order.
* **malformed/unparseable file** → an EMPTY projection AND a recorded parse error.
  The projection stays empty (never a half-parsed subset); the error is surfaced as
  a project VALIDATION ISSUE by :func:`typeflux.project.loader.validate_project`
  (an authoring error on the read/validation surface — NOT an enforcement verdict,
  keeping the explicit enforcement code-set untouched, #723).

:func:`read_project_annotations` is the single parse (both the projection and the
validation issue derive from it, so they can never disagree); :func:`load_project_annotations`
is the projection-only convenience.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator

from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.loader import strict_safe_load

#: The annotations file's location relative to the project manifest: ``.typeflux/``
#: beside the manifest, matching the design (#733). A dot-directory keeps the ack
#: ledger out of the workflow-file namespace while staying in-repo and PR-reviewed.
ANNOTATIONS_DIRNAME = ".typeflux"
ANNOTATIONS_FILENAME = "annotations.yaml"


class InsightAnnotation(BaseModel):
    """One acknowledged/suppressed insight entry (#733). ``insight_id_pattern`` and
    ``reason`` are required; ``tracked_in`` (shape-validated issue URL) and ``expires``
    (ISO date) are optional. Frozen + ``extra=forbid``: an unknown key is an authoring
    error, surfaced as a validation issue rather than silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    insight_id_pattern: str
    reason: str
    tracked_in: str | None = None
    expires: date | None = None

    @field_validator("insight_id_pattern", "reason")
    @classmethod
    def _non_empty_trimmed(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("must be a non-empty, trimmed string")
        return value

    @field_validator("tracked_in")
    @classmethod
    def _validate_tracked_in(cls, value: str | None) -> str | None:
        # SHAPE only — an http(s) URL with a host. The read tier never fetches it
        # (no network); the console renders it as a link (slice 3).
        if value is None:
            return value
        if not value or value.strip() != value:
            raise ValueError("tracked_in must be a non-empty, trimmed URL")
        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise ValueError(f"tracked_in is not a valid URL: {exc}") from exc
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("tracked_in must be an http(s) URL citing the tracking issue")
        return value


class ProjectAnnotations(BaseModel):
    """The served annotations projection envelope (#733): every acknowledgement entry,
    in file order. Empty for a project with no annotations file (the common case) or a
    malformed one (fail-closed — the malformed file is a validation issue instead)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annotations: tuple[InsightAnnotation, ...] = ()


class _AnnotationsFile(BaseModel):
    """The on-disk file shape: a mapping with an ``annotations`` list. ``extra=forbid``
    so a typo'd top-level key (``annotation:``) fails loudly instead of yielding an
    empty-but-valid projection that silently drops every entry."""

    model_config = ConfigDict(extra="forbid")

    annotations: tuple[InsightAnnotation, ...] = ()


class AnnotationsReadResult(NamedTuple):
    """The single parse both consumers derive from. ``annotations`` is always a valid
    (possibly empty) projection — never a partial parse; ``error`` is ``None`` on
    success or absence, and a human-readable parse message when the file exists but is
    malformed (the validation-issue source); ``path`` is the resolved file location
    (for the issue's ``path`` field)."""

    annotations: ProjectAnnotations
    error: str | None
    path: Path


def annotations_path(project: TypefluxProjectSpec) -> Path:
    """The project's annotations file location: ``<manifest_dir>/.typeflux/annotations.yaml``."""
    return project.project_dir / ANNOTATIONS_DIRNAME / ANNOTATIONS_FILENAME


def read_project_annotations(project: TypefluxProjectSpec) -> AnnotationsReadResult:
    """Parse the project's annotations file, fail-closed (#733).

    Absent file → empty projection, no error (the common case). A YAML/schema failure
    → empty projection AND a parse-error message (the projection is never a partial
    subset). An empty/comments-only file (``None``) → empty projection, no error.
    """
    path = annotations_path(project)
    if not path.exists():
        return AnnotationsReadResult(ProjectAnnotations(), None, path)
    try:
        raw = strict_safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any load failure is an authoring error, reported.
        return AnnotationsReadResult(
            ProjectAnnotations(), f"annotations file is not valid YAML: {exc}", path
        )
    if raw is None:
        # An empty or comments-only file is an empty ledger, not a malformed one.
        return AnnotationsReadResult(ProjectAnnotations(), None, path)
    try:
        parsed = _AnnotationsFile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError (+ any coercion error).
        return AnnotationsReadResult(
            ProjectAnnotations(), f"annotations file does not match the schema: {exc}", path
        )
    return AnnotationsReadResult(ProjectAnnotations(annotations=parsed.annotations), None, path)


def load_project_annotations(project: TypefluxProjectSpec) -> ProjectAnnotations:
    """The annotations projection the read tier serves (#733): the parsed entries, or an
    empty projection when the file is absent or malformed (fail-closed). The malformed
    case additionally surfaces as a validation issue via ``validate_project``."""
    return read_project_annotations(project).annotations


__all__ = [
    "ANNOTATIONS_DIRNAME",
    "ANNOTATIONS_FILENAME",
    "AnnotationsReadResult",
    "InsightAnnotation",
    "ProjectAnnotations",
    "annotations_path",
    "load_project_annotations",
    "read_project_annotations",
]
