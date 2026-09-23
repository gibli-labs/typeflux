"""Subject-identity extraction (#715 slice 1).

A first-class ``subject_ids`` concept that enters at workflow submit and fans to
the ``TypefluxSubjectIds`` keyword-list search attribute (the subject->execution
index), the Langfuse observer (native ``userId`` + ``typeflux.subject:{id}``
tags), and the cross-run cache record. The spine of the erasure epic (#715): a
subject id is the handle every later erasure surface targets.

Two entry points mirror the ``artifacts: { from: input.X }`` shape:

* **Declarative** — a ``subjects:`` block on the workflow spec, each item a
  ``{ from: input.<path> }`` selector pulled off the validated workflow input at
  submit time.
* **Explicit** — a ``subject_ids`` override passed straight to the start/submit
  APIs, which wins over declarative extraction.

Extraction is loud: a required selector whose path is missing/empty raises at
start rather than silently starting a workflow with no subject index — an
un-indexed execution is invisible to erasure, so a missing subject is a bug, not
a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from typeflux.core.artifacts import _value_at_path

__all__ = [
    "SUBJECT_IDS_SEARCH_ATTRIBUTE",
    "SubjectInput",
    "normalize_subject_ids",
    "resolve_subject_ids",
    "subject_index_query",
    "subject_trace_tags",
    "subject_user_id",
]

#: The Temporal keyword-LIST search attribute that indexes an execution by the
#: subject id(s) it processes. A fixed, unversioned attribute name (unlike the
#: opt-in ``runtime.temporal.workflow_search_attribute``): it is THE erasure
#: index, always stamped when an execution has subjects, so a single visibility
#: query (``TypefluxSubjectIds = '<id>'``) enumerates every execution touching a
#: subject. Must be registered on the namespace as a ``KeywordList`` before use
#: (a deploy-time step; see docs/yaml.md and binding.v1.json).
SUBJECT_IDS_SEARCH_ATTRIBUTE = "TypefluxSubjectIds"


@dataclass(frozen=True)
class SubjectInput:
    """A declarative subject selector: pull id(s) from ``from_path`` on the input.

    ``from_path`` is a dotted ``input.<path>`` selector (identical grammar to an
    artifact input's ``from``). The resolved value is a single string subject id,
    or a sequence of them (a review packet spans several subjects). ``required``
    (default True) makes a missing/empty resolution a hard error at start.
    """

    from_path: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.from_path or self.from_path.strip() != self.from_path:
            raise ValueError("subject input from_path must be non-empty and trimmed")
        if not self.from_path.startswith("input."):
            raise ValueError("subject input from_path must start with 'input.'")


def resolve_subject_ids(
    input_value: Any,
    subject_inputs: Sequence[SubjectInput],
) -> tuple[str, ...]:
    """Extract subject id(s) from the validated workflow input.

    Each selector resolves to one id or a sequence of ids; every resolved value
    must be a non-empty string. Order is preserved and duplicates collapse (the
    FIRST occurrence wins, so the primary subject — the Langfuse ``userId`` — is
    stable). A required selector that resolves to ``None``/empty raises; an
    optional one contributes nothing.
    """

    resolved: list[str] = []
    seen: set[str] = set()
    for subject_input in subject_inputs:
        raw = _value_at_path(input_value, subject_input.from_path)
        # A required selector must CONTRIBUTE at least one id: a missing path AND
        # an empty list both yield zero ids, and either would silently start an
        # un-indexed (erasure-invisible) execution — the exact outcome `required`
        # exists to prevent (#715 review round, finding 2).
        values = (
            [] if raw is None else _coerce_subject_values(raw, from_path=subject_input.from_path)
        )
        if not values:
            if subject_input.required:
                raise ValueError(
                    f"subject selector {subject_input.from_path!r} resolved to no value; "
                    "a required subject must be present at start (an un-indexed execution "
                    "is invisible to erasure) — fix the input or mark the selector optional"
                )
            continue
        for value in values:
            if value not in seen:
                seen.add(value)
                resolved.append(value)
    return tuple(resolved)


def _coerce_subject_values(raw: Any, *, from_path: str) -> list[str]:
    if isinstance(raw, str):
        values: list[Any] = [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        values = list(raw)
    else:
        raise TypeError(
            f"subject selector {from_path!r} must resolve to a string or a sequence of "
            f"strings, not {type(raw).__name__}"
        )
    coerced: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"subject selector {from_path!r} resolved to a non-empty-string value "
                f"({value!r}); subject ids must be non-empty strings"
            )
        coerced.append(value)
    return coerced


def normalize_subject_ids(subject_ids: Sequence[str]) -> tuple[str, ...]:
    """Validate + de-duplicate an EXPLICIT ``subject_ids`` override.

    The explicit start-API path bypasses extraction, so it re-runs the same
    non-empty-string check and order-preserving de-dup the declarative path does.
    """

    resolved: list[str] = []
    seen: set[str] = set()
    for value in subject_ids:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"subject_ids must be non-empty strings, got {value!r}")
        if value not in seen:
            seen.add(value)
            resolved.append(value)
    return tuple(resolved)


def subject_user_id(subject_ids: Sequence[str]) -> str | None:
    """The Langfuse native ``userId``: the PRIMARY (first) subject id, or None.

    Native ``userId`` and the portable ``typeflux.subject:{id}`` tags always
    agree — the ratified #715 decision keys ``userId`` on the same identity the
    ``subjects:`` declaration yields (primary when multiple)."""

    return subject_ids[0] if subject_ids else None


def subject_trace_tags(subject_ids: Sequence[str]) -> list[str]:
    """The portable per-subject trace tags: ``typeflux.subject:{id}`` per id."""

    return [f"typeflux.subject:{subject_id}" for subject_id in subject_ids]


def subject_index_query(subject_id: str) -> str:
    """The visibility query that enumerates every execution touching a subject.

    A keyword-LIST attribute matches when the list CONTAINS the value, so
    ``TypefluxSubjectIds = '<id>'`` selects every execution stamped with that
    subject. The id is quote-escaped by DOUBLING single quotes — the Temporal
    visibility SQL convention the existing search-attribute queries use
    (``binding_ts.list_query``, TS ``frozen-version``) — so it is injection-safe."""

    escaped = subject_id.replace("'", "''")
    return f"{SUBJECT_IDS_SEARCH_ATTRIBUTE} = '{escaped}'"
