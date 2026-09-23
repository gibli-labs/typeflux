"""Enforcement-events feed (#723 §1): a normalized, read-at-request surface over
the two enforcement sources the control plane already computes.

The control plane persists nothing here — every request re-reads both sources
(the stateless re-read-per-request design of #577 stands):

* **Admission events** are *normalized* from the read tier's existing
  ``validate_project`` report — the policy/admission-gate verdicts it already
  derives — never re-computed. A plain authoring/config error
  (``duplicate_workflow_name``, a broken-policy-YAML ``policy_composition``
  conflict) is NOT an enforcement verdict and stays in the validation surface.
  Which codes count as verdicts is an EXPLICIT allow-set
  (:data:`_ADMISSION_ENFORCEMENT_CODES`), never a substring heuristic — a
  substring match on ``"policy"`` wrongly swept in config errors whose
  ``reference`` is a policy id, mislabeling it as a workflow id.
* **Runtime events** are extracted from Langfuse traces queried through the
  injected transport seam (#573). The ONLY confirmed runtime marker this slice
  is a moderation ``on_violation=block`` verdict (recorded as
  ``typeflux_moderation.decision == "block"`` on the activity span, #158). The
  query is **bounded** — an explicit time window (default 7d) and a row cap —
  never an unbounded scan.

There is deliberately NO runtime *review-rejection* source this slice. The only
signal available client-side was an errored ``TypefluxLifecycleSignal:*`` span,
and no honest heuristic can be built on it: the same span prefix also wraps
``typeflux_request_cancel`` and marks ANY raised exception (a transport hiccup
would fabricate a "review rejected" verdict), while a *real* review verdict is
decided server-side inside the workflow and never errors this client-side signal
span. A faithful review-rejection event needs a dedicated writer-side marker
recorded by the engine at the point of decision (slice 2+, #723).

Reachability of the runtime source **degrades loudly**: an unconfigured
observer reports ``langfuse: "not_configured"`` and a transport failure reports
``langfuse: "unreachable"`` in the response's ``partial`` marker — the admission
events still serve, and the runtime portion is NEVER a silent empty list.

Every helper below is a **pure function** over data (report / traces /
events); the only impurity is :func:`default_langfuse_enforcement_reader`, the
transport seam the control plane injects and tests replace with a fixture.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from typeflux.observability.inspect import _as_utc_aware
from typeflux.project.spec import (
    ProjectResolvedWorkflowValidation,
    ProjectValidationReport,
)

if TYPE_CHECKING:
    from typeflux.observability.inspect import ObservationRecord, TraceRecord

#: Default read window when the caller pins neither ``since`` nor ``until`` (#723
#: constraint: an explicit, bounded time window, never an unbounded Langfuse scan).
DEFAULT_WINDOW = timedelta(days=7)
#: Default page size and hard cap for pagination and the bounded trace fetch.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# The reachability vocabulary this best-effort source degrades into (``partial.langfuse``).
# Sibling of ``typeflux.project.github_provenance.GithubStatus`` (the
# github-provenance shape, which adds ``rate_limited``); a shared reachability module is
# deferred until a third best-effort source appears (#577) — until then the two shapes are
# kept parallel by convention, not a common type.
LangfuseStatus = Literal["ok", "unreachable", "not_configured"]
#: Runtime moderation blocks produce ``blocked``; admission policy verdicts
#: produce ``rejected``. There is no ``warned`` producer this slice, so the
#: vocabulary is closed to the two verdicts that are actually emitted.
Verdict = Literal["blocked", "rejected"]
Source = Literal["admission", "runtime"]


class EnforcementEvidence(BaseModel):
    """Where to see the evidence for one enforcement event: the console derives a
    Langfuse deep link from ``trace_id`` (paired with the bundle's links, the
    established #719 pattern). Optional — an admission event derived from a static
    report has no trace to point at."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str | None = None


class EnforcementEvent(BaseModel):
    """One normalized enforcement decision (#723). ``source`` distinguishes an
    admission-time verdict (normalized from the validation surface) from a
    runtime verdict (read from a Langfuse trace). ``occurred_at`` is the trace
    timestamp for runtime events; an admission event reflects *current*
    admission state (not a past point in time) so it omits it. ``policy_ids`` is
    the *recorded* applied policy provenance for the event — the composed policy
    the admission verdict was evaluated under, or the trace's recorded applied
    policies for a runtime event — NOT the caller's filter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurred_at: str | None = None
    source: Source
    policy_ids: tuple[str, ...] = ()
    #: The enforcement rule/verdict code, e.g. ``policy_provider`` (admission) or
    #: ``moderation.on_violation.block`` (runtime).
    rule: str
    workflow_id: str | None = None
    environment_id: str | None = None
    execution_id: str | None = None
    verdict: Verdict
    evidence: EnforcementEvidence = EnforcementEvidence()
    detail: str


class EnforcementPartial(BaseModel):
    """Loud-degradation marker (#723): the reachability of each best-effort
    source. Admission events are always computed (no partiality); only the
    runtime source (Langfuse over the #573 transport seam) can be partial."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    langfuse: LangfuseStatus


class EnforcementEventList(BaseModel):
    """The enforcement-events response envelope (#723). ``since``/``until`` echo
    the resolved (bounded) window; ``next_cursor`` is an opaque pagination token,
    present only when more events remain.

    The cursor binds its offset to a fingerprint of the filter set it was minted
    under (:func:`filter_fingerprint`): reusing a cursor after changing any
    filter is rejected (422), never silently skips/dupes. Residual caveat: the
    fingerprint cannot detect a shift in the *underlying data* between pages
    (the feed is stateless and re-reads both sources each request), so a page
    boundary may still skip/duplicate if events appear or vanish mid-scroll —
    acceptable for a governance review surface, unlike a filter change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[EnforcementEvent, ...] = ()
    partial: EnforcementPartial
    since: str
    until: str
    next_cursor: str | None = None


class EnforcementReadResult(NamedTuple):
    """The transport seam's result: a reachability ``status`` (mapped straight
    into the ``partial`` marker) and the bounded set of traces it read (empty
    unless ``status == "ok"``)."""

    status: LangfuseStatus
    traces: tuple[TraceRecord, ...] = ()


# ---------------------------------------------------------------------------
# Admission events — normalized from the read tier's validation report.
# ---------------------------------------------------------------------------

#: Policy-verdict CHECK codes: a resolved workflow that FAILS one of these was
#: rejected by its composed project policy (emitted by ``validate_project_policy``
#: / ``policy_enforcement.py``, #300/#454/#298). Each is a governance verdict
#: against a workflow, so it becomes an admission enforcement event.
_POLICY_VERDICT_CODES: frozenset[str] = frozenset(
    {
        "policy_allowlists",  # composed allowlist gate
        "policy_artifacts",  # artifact policy gate
        "policy_composition_ceilings",  # #298 admission ceilings
        "policy_floor",  # policy floor gate
        "policy_imports",  # import allowlist gate
        "policy_observability",  # observability policy gate
        "policy_provider",  # provider/model allowlist gate
        "policy_provider_limits",  # provider limit gate
        "policy_provider_retry",  # provider retry gate
        "policy_registry",  # prompt-registry gate
        "policy_review",  # review-route gate
        "policy_risk_tier",  # #300 risk-tier gate
        "risk_tier_binding",  # #788 elevated-tier-must-be-enforced gate
        "policy_secrets",  # secret-reference gate
        "policy_selection",  # policy-selection gate
        "policy_semantics",  # moderation/semantics gate
        "policy_subworkflow_closure",  # #55 transitive-closure gate
        "policy_temporal",  # Temporal-config gate
    }
)

#: Admission-gate verdict codes (``project/admission.py`` ``admit_spec``,
#: #298/D298-2): genuine admission decisions against a submitted spec. Included
#: for a complete classifier even though the enforcement feed reads only
#: ``validate_project`` today — the ``admission_`` prefix must never be dropped
#: by an "unknown" substring (the old dead ``startswith`` branch's bug).
_ADMISSION_VERDICT_CODES: frozenset[str] = frozenset(
    {
        "admission_unknown_workflow",  # an explicit slot the manifest doesn't declare
        "admission_policy_selection",  # external-origin spec with no governing policy
    }
)

#: Every code that is a genuine admission/policy ENFORCEMENT VERDICT. Membership
#: is exact — NOT a substring test. DELIBERATELY EXCLUDED (authoring/config
#: errors that merely mention "policy"/"admission" — not verdicts against a
#: workflow, and whose ``reference`` is a policy/target id, never a workflow id):
#:   * ``policy_composition`` / ``invalid_policy_composition`` /
#:     ``invalid_target_policy_composition`` — broken policy YAML (a composition
#:     conflict), reference is a policy/target id.
#:   * ``admission_parse`` / ``admission_spec_shape`` / ``admission_schema_ref_roots``
#:     — the submitted spec failed to parse / validate / structurally gate.
#:   * ``provider_import_policy`` — spec-level custom-import validity
#:     (``spec.runtime.imports``), not a composed-project-policy verdict.
#:   * ``unknown_validation_policy`` / ``unknown_target_policy`` — config
#:     reference errors that name a policy/target.
#:   * ``policy_enforcement`` — the "no policy selected" SKIP marker (never fails).
_ADMISSION_ENFORCEMENT_CODES: frozenset[str] = _POLICY_VERDICT_CODES | _ADMISSION_VERDICT_CODES


def is_admission_enforcement_code(code: str) -> bool:
    """Whether a validation issue/check ``code`` is a POLICY or admission-gate
    *enforcement verdict* (as opposed to a plain authoring/config error).

    Exact membership in :data:`_ADMISSION_ENFORCEMENT_CODES` — a broken-policy-YAML
    composition error or a spec-shape authoring error is NOT a verdict against a
    workflow and stays in the validation surface."""
    return code in _ADMISSION_ENFORCEMENT_CODES


def _split_issue_reference(reference: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a validation issue ``reference`` into
    ``(workflow_id, environment_id)``. Only reached for the enforcement-verdict
    codes above, whose lift uses ``"{environment}:{workflow}"``; a bare reference
    is a workflow id. (Config-error references — a bare policy/target id — never
    reach here because their codes are excluded from the verdict set.)"""
    if not reference:
        return None, None
    if ":" in reference:
        environment, _, workflow = reference.partition(":")
        return (workflow or None), (environment or None)
    return reference, None


def _applied_policy_ids(resolved: ProjectResolvedWorkflowValidation) -> tuple[str, ...]:
    """The composed policy this resolved workflow was evaluated under, read from
    the ``policy_selection`` check's ``applied_policy_ids`` detail — the genuine
    recorded provenance (never the caller's filter)."""
    for check in resolved.checks:
        if check.code == "policy_selection":
            applied = check.details.get("applied_policy_ids")
            if isinstance(applied, list):
                return tuple(str(policy_id) for policy_id in applied)
    return ()


def admission_events_from_report(
    report: ProjectValidationReport,
    *,
    environment_id: str | None = None,
) -> list[EnforcementEvent]:
    """Normalize the enforcement verdicts out of a ``validate_project`` report.

    Per-workflow failed policy/admission checks become the primary events; a
    top-level enforcement issue with no resolved-workflow context is added too.
    The read tier *lifts* each failed resolved check into a top-level
    ``resolved_*`` issue — those are skipped here so they are not double-counted.

    ``policy_ids`` on each event is the RECORDED applied policy provenance for
    that resolved workflow (from its ``policy_selection`` check), not the caller's
    filter — the caller's ``policy_id`` filter is applied later in
    :func:`filter_events`."""
    events: list[EnforcementEvent] = []
    for resolved in report.resolved_workflows:
        applied = _applied_policy_ids(resolved)
        for check in resolved.checks:
            if check.status != "failed" or not is_admission_enforcement_code(check.code):
                continue
            events.append(
                EnforcementEvent(
                    source="admission",
                    verdict="rejected",
                    rule=check.code,
                    policy_ids=applied,
                    workflow_id=resolved.workflow_id,
                    environment_id=resolved.environment_id or environment_id,
                    detail=check.message or f"admission rejected: {check.code}",
                )
            )
    for issue in report.issues:
        if issue.code.startswith("resolved_"):
            continue  # already normalized from the resolved-workflow check above.
        if not is_admission_enforcement_code(issue.code):
            continue
        workflow_id, env_id = _split_issue_reference(issue.reference)
        events.append(
            EnforcementEvent(
                source="admission",
                verdict="rejected",
                rule=issue.code,
                # A top-level issue has no resolved-workflow context, so no recorded
                # applied-policy provenance — the field is dropped (empty), never the
                # caller's filter masquerading as provenance.
                workflow_id=workflow_id,
                environment_id=env_id or environment_id,
                detail=issue.message,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Runtime events — extracted from Langfuse traces (through the #573 seam).
# ---------------------------------------------------------------------------


def _iso_utc(value: datetime | None) -> str | None:
    """ISO-8601 of a timestamp normalized to aware-UTC at ingestion — so a naive
    timestamp from an injected reader can never later compare naive-vs-aware
    against the window (which raises ``TypeError`` → 500)."""
    normalized = _as_utc_aware(value)
    return normalized.isoformat() if normalized is not None else None


def _event_from_observation(
    observation: ObservationRecord,
    *,
    trace_id: str,
    workflow_id: str | None,
    workflow_name: str | None,
    environment_id: str | None,
    trace_timestamp: datetime | None,
    policy_ids: Sequence[str],
) -> EnforcementEvent | None:
    """Recognize an enforcement verdict on one trace observation, or ``None``.

    The only confirmed runtime marker this slice is
    ``typeflux_moderation.decision == "block"`` — a moderation
    ``on_violation=block`` verdict (#158), recorded by the engine on the activity
    span (never re-derived). (A review-gate rejection has no honest client-side
    marker; see the module docstring.)"""
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    applied = tuple(policy_ids)
    moderation = metadata.get("typeflux_moderation")
    if isinstance(moderation, dict) and moderation.get("decision") == "block":
        categories = moderation.get("categories")
        listed = ", ".join(str(c) for c in categories) if isinstance(categories, list) else ""
        return EnforcementEvent(
            occurred_at=_iso_utc(observation.start_time) or _iso_utc(trace_timestamp),
            source="runtime",
            verdict="blocked",
            rule="moderation.on_violation.block",
            policy_ids=applied,
            workflow_id=workflow_name,
            environment_id=environment_id,
            execution_id=workflow_id,
            evidence=EnforcementEvidence(trace_id=trace_id),
            detail=f"moderation blocked activity output: {listed or 'unspecified'}",
        )
    return None


def runtime_events_from_traces(
    traces: Iterable[TraceRecord],
    *,
    environment_id: str | None = None,
    workflow_name_to_id: Mapping[str, str] | None = None,
) -> list[EnforcementEvent]:
    """Extract every runtime enforcement event from a set of Langfuse traces.

    Trace-level fields (execution/workflow/environment/policy) come from the same
    :class:`TraceSummaryView` the correlation tier already uses; the
    per-observation markers come from :func:`_event_from_observation`.

    A trace records the workflow *type name* (the Temporal/YAML name), not the
    PROJECT workflow id the API filters and validates against. ``workflow_name_to_id``
    (built from the validation report's resolved workflows) normalizes each event's
    ``workflow_id`` to the project id so the API's workflow filter matches; an
    unmapped name falls back to the raw name (a trace for a workflow not in the
    resolved set — e.g. renamed/removed — still surfaces, keyed by its raw name)."""
    from typeflux.observability.inspect import TraceSummaryView

    name_to_id = workflow_name_to_id or {}
    events: list[EnforcementEvent] = []
    for trace in traces:
        summary = TraceSummaryView.from_trace(trace)
        applied = summary.policy.applied_policy_ids if summary.policy is not None else ()
        env = summary.environment or environment_id
        for observation in trace.observations:
            event = _event_from_observation(
                observation,
                trace_id=summary.trace_id,
                workflow_id=summary.workflow_id,
                workflow_name=summary.workflow_name,
                environment_id=env,
                trace_timestamp=summary.timestamp,
                policy_ids=applied,
            )
            if event is None:
                continue
            if event.workflow_id is not None and event.workflow_id in name_to_id:
                event = event.model_copy(update={"workflow_id": name_to_id[event.workflow_id]})
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Window / filtering / ordering / pagination — pure helpers.
# ---------------------------------------------------------------------------


def resolve_window(
    since: datetime | None,
    until: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve the bounded read window, defaulting to the last :data:`DEFAULT_WINDOW`."""
    until_dt = until or (now or datetime.now(UTC))
    since_dt = since or (until_dt - DEFAULT_WINDOW)
    return since_dt, until_dt


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 event timestamp to an aware-UTC datetime. Handles a
    trailing ``Z`` and normalizes any naive/offset timestamp to UTC (shared with
    observability/{langfuse,langsmith}) so window comparison never mixes
    naive-vs-aware."""
    if not value:
        return None
    try:
        return _as_utc_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def filter_events(
    events: Iterable[EnforcementEvent],
    *,
    workflow_ids: Sequence[str] = (),
    environment_id: str | None = None,
    verdicts: Sequence[str] = (),
    policy_ids: Sequence[str] = (),
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[EnforcementEvent]:
    """Apply the request filters to the MERGED feed. A runtime event outside the
    ``[since, until]`` window is dropped; an admission event (no ``occurred_at``)
    reflects current state and is always in-window. The ``policy_ids`` filter
    keeps an event whose RECORDED applied policy ids intersect the request set —
    applied consistently to both admission and runtime events."""
    workflow_set = set(workflow_ids)
    verdict_set = set(verdicts)
    policy_set = set(policy_ids)
    kept: list[EnforcementEvent] = []
    for event in events:
        if workflow_set and event.workflow_id not in workflow_set:
            continue
        if environment_id is not None and event.environment_id not in (None, environment_id):
            continue
        if verdict_set and event.verdict not in verdict_set:
            continue
        if policy_set and not (policy_set & set(event.policy_ids)):
            continue
        occurred = _parse_iso(event.occurred_at)
        if occurred is not None:
            if since is not None and occurred < since:
                continue
            if until is not None and occurred > until:
                continue
        kept.append(event)
    return kept


def _event_sort_key(event: EnforcementEvent) -> tuple:
    # Newest first; an admission event (no timestamp) reflects current state and
    # sorts ahead of dated runtime events. Every remaining key is ascending and
    # total, so ordering is fully deterministic (conformance depends on it).
    occurred = _parse_iso(event.occurred_at)
    primary = float("-inf") if occurred is None else -occurred.timestamp()
    return (
        primary,
        event.source,
        event.rule,
        event.workflow_id or "",
        event.environment_id or "",
        event.execution_id or "",
        event.detail,
    )


def sort_events(events: Iterable[EnforcementEvent]) -> list[EnforcementEvent]:
    return sorted(events, key=_event_sort_key)


def filter_fingerprint(
    *,
    workflow_ids: Sequence[str] = (),
    environment_id: str | None = None,
    verdicts: Sequence[str] = (),
    policy_ids: Sequence[str] = (),
    since: datetime | None = None,
    until: datetime | None = None,
) -> str:
    """A short, stable fingerprint of the filter set a cursor was minted under. A
    cursor is a bare offset into a filtered+ordered list; reusing it after the
    filters change would silently skip or duplicate events, so the offset is bound
    to this fingerprint and a mismatch is rejected (422).

    ``since``/``until`` are the caller's RAW (unresolved) window bounds — ``None``
    when unpinned — NOT the resolved window: an unpinned ``until`` resolves to
    ``now()`` afresh each request, so fingerprinting the resolved value would
    reject every paginated call. Fingerprinting the caller's intent keeps
    pagination stable while still rejecting a changed explicit bound."""
    payload = json.dumps(
        {
            "w": sorted(workflow_ids),
            "e": environment_id,
            "v": sorted(verdicts),
            "p": sorted(policy_ids),
            "s": since.isoformat() if since is not None else None,
            "u": until.isoformat() if until is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def encode_cursor(offset: int, fingerprint: str) -> str:
    """Encode an opaque pagination cursor: the offset bound to the filter
    ``fingerprint``. Base64-opaque by design (an operator must not hand-craft an
    offset) — never a bare stringified int."""
    token = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":"))
    return base64.urlsafe_b64encode(token.encode("ascii")).decode("ascii")


def decode_cursor(cursor: str | None, *, fingerprint: str) -> int:
    """Decode an opaque pagination cursor to an offset, verifying it was minted
    under the current filters. A malformed cursor or a filter mismatch is a bad
    request — raise ``ValueError`` (the API maps it to 422)."""
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        payload = json.loads(raw)
        offset = int(payload["o"])
        bound = payload["f"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, base64.binascii.Error) as exc:
        raise ValueError(f"malformed pagination cursor: {cursor!r}") from exc
    if not isinstance(bound, str) or bound != fingerprint:
        raise ValueError(
            "pagination cursor does not match the current filters; omit the cursor to "
            "restart from the first page when any filter changes"
        )
    if offset < 0:
        raise ValueError(f"pagination cursor offset must be >= 0: {offset}")
    return offset


def paginate(
    events: Sequence[EnforcementEvent], *, offset: int, limit: int, fingerprint: str
) -> tuple[list[EnforcementEvent], str | None]:
    window = list(events[offset : offset + limit])
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset, fingerprint) if next_offset < len(events) else None
    return window, next_cursor


def build_enforcement_feed(
    *,
    report: ProjectValidationReport | None,
    read_result: EnforcementReadResult,
    environment_id: str | None,
    policy_ids: Sequence[str],
    workflow_ids: Sequence[str],
    verdicts: Sequence[str],
    since: datetime,
    until: datetime,
    limit: int,
    offset: int,
    cursor_fingerprint: str,
) -> EnforcementEventList:
    """Compose the enforcement feed from both sources: normalize, filter, order,
    and paginate — the one place the two sources are merged. ``policy_ids`` here
    is the caller's FILTER (applied in :func:`filter_events`); each event carries
    its own recorded applied-policy provenance. ``cursor_fingerprint`` (computed by
    the caller from the RAW window intent, see :func:`filter_fingerprint`) binds
    the emitted ``next_cursor`` to this filter set."""
    events: list[EnforcementEvent] = []
    name_to_id: dict[str, str] = {}
    if report is not None:
        events.extend(admission_events_from_report(report, environment_id=environment_id))
        # workflow NAME → PROJECT id, so runtime events (keyed by trace workflow
        # name) normalize onto the ids the API filters/validates against.
        for resolved in report.resolved_workflows:
            if resolved.workflow_name:
                name_to_id.setdefault(resolved.workflow_name, resolved.workflow_id)
    events.extend(
        runtime_events_from_traces(
            read_result.traces,
            environment_id=environment_id,
            workflow_name_to_id=name_to_id,
        )
    )
    events = filter_events(
        events,
        workflow_ids=workflow_ids,
        environment_id=environment_id,
        verdicts=verdicts,
        policy_ids=policy_ids,
        since=since,
        until=until,
    )
    ordered = sort_events(events)
    page, next_cursor = paginate(
        ordered, offset=offset, limit=limit, fingerprint=cursor_fingerprint
    )
    return EnforcementEventList(
        events=tuple(page),
        partial=EnforcementPartial(langfuse=read_result.status),
        since=since.isoformat(),
        until=until.isoformat(),
        next_cursor=next_cursor,
    )


def observer_from_report(report: ProjectValidationReport) -> str | None:
    """The resolved observer type (``langfuse``/``none``/…) for the report's
    environment, read from the ``observability_config`` check the read tier
    already emits — ``None`` when nothing resolved (so the runtime source is
    reported ``not_configured``)."""
    for resolved in report.resolved_workflows:
        for check in resolved.checks:
            if check.code == "observability_config":
                observer = check.details.get("type")
                if isinstance(observer, str):
                    return observer
    return None


# ---------------------------------------------------------------------------
# The transport seam (the only impure function) — injected by the control plane.
# ---------------------------------------------------------------------------


def _langfuse_env_configured() -> bool:
    # Same posture as the connections/correlation tiers: credentials come from
    # the serving PROCESS environment, not the project's env file.
    return bool(
        (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL"))
        and os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    )


def default_langfuse_enforcement_reader(
    *,
    observer: str | None,
    environment_id: str | None,
    since: datetime,
    until: datetime,
    limit: int,
    backend_cache: dict | None = None,
) -> EnforcementReadResult:
    """The default runtime-events transport seam (#573): a bounded Langfuse trace
    query, degrading loudly. Runtime enforcement events exist only where the
    observer is Langfuse and the process credentials are present; a query failure
    is ``unreachable`` (never a silent empty ``ok``).

    ``backend_cache`` (a per-app dict the control plane threads in) memoizes the
    Langfuse backend so it is built once per app, not per request — the backend
    depends only on the process env."""
    if observer != "langfuse" or not _langfuse_env_configured():
        return EnforcementReadResult("not_configured")
    try:
        from typeflux.observability.inspect import TraceSearchQuery
        from typeflux.observability.langfuse import LangfuseObservabilityBackend

        cached = backend_cache.get("backend") if backend_cache is not None else None
        if isinstance(cached, LangfuseObservabilityBackend):
            backend = cached
        else:
            backend = LangfuseObservabilityBackend.from_env()
            if backend_cache is not None:
                backend_cache["backend"] = backend
        page = backend.reader.search_traces(
            TraceSearchQuery(
                environment=environment_id,
                since=since,
                until=until,
                limit=min(limit, MAX_LIMIT),
            )
        )
    except Exception:  # noqa: BLE001 - degrade loudly, never 500 the feed.
        return EnforcementReadResult("unreachable")
    return EnforcementReadResult("ok", tuple(page.traces))


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_WINDOW",
    "MAX_LIMIT",
    "EnforcementEvent",
    "EnforcementEventList",
    "EnforcementEvidence",
    "EnforcementPartial",
    "EnforcementReadResult",
    "admission_events_from_report",
    "build_enforcement_feed",
    "decode_cursor",
    "default_langfuse_enforcement_reader",
    "encode_cursor",
    "filter_events",
    "filter_fingerprint",
    "is_admission_enforcement_code",
    "observer_from_report",
    "paginate",
    "resolve_window",
    "runtime_events_from_traces",
    "sort_events",
]
