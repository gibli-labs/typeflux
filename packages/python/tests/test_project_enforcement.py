"""Unit tests for the pure enforcement-events normalization helpers (#723 §1).

These exercise the helpers in isolation from the HTTP layer: admission
normalization from a validation report, runtime extraction from Langfuse
traces, filtering/windowing/ordering, and cursor pagination.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from typeflux.manifests import build_workflow_execution_manifest
from typeflux.observability.inspect import ObservationRecord, TraceRecord
from typeflux.project.enforcement import (
    DEFAULT_WINDOW,
    EnforcementEvent,
    EnforcementEvidence,
    EnforcementReadResult,
    admission_events_from_report,
    build_enforcement_feed,
    decode_cursor,
    encode_cursor,
    filter_events,
    filter_fingerprint,
    is_admission_enforcement_code,
    observer_from_report,
    paginate,
    resolve_window,
    runtime_events_from_traces,
    sort_events,
)
from typeflux.project.spec import (
    ProjectResolvedWorkflowValidation,
    ProjectValidationCheck,
    ProjectValidationIssue,
    ProjectValidationReport,
)

# ---------------------------------------------------------------------------
# Admission normalization
# ---------------------------------------------------------------------------


def _report(
    *,
    issues: tuple[ProjectValidationIssue, ...] = (),
    resolved: tuple[ProjectResolvedWorkflowValidation, ...] = (),
) -> ProjectValidationReport:
    return ProjectValidationReport(
        project_name="demo",
        manifest_path="/tmp/typeflux.project.yaml",
        ok=not issues,
        issues=issues,
        resolved_workflows=resolved,
    )


def test_is_admission_enforcement_code_uses_explicit_verdict_sets() -> None:
    # Genuine policy/admission VERDICT codes (exact membership, not substrings).
    assert is_admission_enforcement_code("policy_provider")
    assert is_admission_enforcement_code("policy_risk_tier")
    assert is_admission_enforcement_code("risk_tier_binding")  # #788
    assert is_admission_enforcement_code("policy_selection")
    assert is_admission_enforcement_code("admission_unknown_workflow")
    assert is_admission_enforcement_code("admission_policy_selection")
    # COMPOSITION errors are broken policy YAML, not verdicts — excluded even though
    # they contain "policy" (the substring bug swept them in and then mislabeled
    # their policy-id reference as a workflow id).
    assert not is_admission_enforcement_code("policy_composition")
    assert not is_admission_enforcement_code("invalid_policy_composition")
    assert not is_admission_enforcement_code("invalid_target_policy_composition")
    # Authoring/config errors are NOT enforcement verdicts.
    assert not is_admission_enforcement_code("admission_parse")
    assert not is_admission_enforcement_code("admission_spec_shape")
    assert not is_admission_enforcement_code("provider_import_policy")
    assert not is_admission_enforcement_code("duplicate_workflow_name")
    assert not is_admission_enforcement_code("unknown_profile_reference")
    assert not is_admission_enforcement_code("unknown_validation_policy")
    # The lifted-issue form is never classified directly (the issues loop skips
    # `resolved_*`), so the raw prefix is not in the verdict set.
    assert not is_admission_enforcement_code("resolved_policy_composition_failed")


def test_admission_events_from_resolved_failed_policy_checks() -> None:
    resolved = ProjectResolvedWorkflowValidation(
        workflow_id="workflow",
        environment_id="prod",
        ok=False,
        checks=(
            ProjectValidationCheck(code="environment_workflow_resolution", status="passed"),
            # The recorded applied-policy provenance for this resolved workflow.
            ProjectValidationCheck(
                code="policy_selection",
                status="passed",
                details={"applied_policy_ids": ["base", "regulated"]},
            ),
            ProjectValidationCheck(
                code="policy_provider",
                status="failed",
                message="model claude-x not in allowlist",
            ),
            ProjectValidationCheck(code="workflow_graph", status="skipped"),
        ),
    )
    events = admission_events_from_report(_report(resolved=(resolved,)), environment_id="prod")

    assert len(events) == 1
    event = events[0]
    assert event.source == "admission"
    assert event.verdict == "rejected"
    assert event.rule == "policy_provider"
    assert event.workflow_id == "workflow"
    assert event.environment_id == "prod"
    # policy_ids is the RECORDED applied provenance (from policy_selection), NOT a caller filter.
    assert event.policy_ids == ("base", "regulated")
    assert event.detail == "model claude-x not in allowlist"
    assert event.occurred_at is None  # admission reflects current state, not a moment.


def test_admission_events_drop_policy_ids_without_recorded_selection() -> None:
    # No policy_selection check → no recorded provenance → the field is dropped
    # (empty), never a caller filter masquerading as provenance.
    resolved = ProjectResolvedWorkflowValidation(
        workflow_id="workflow",
        environment_id="prod",
        ok=False,
        checks=(
            ProjectValidationCheck(code="policy_secrets", status="failed", message="secret X"),
        ),
    )
    events = admission_events_from_report(_report(resolved=(resolved,)))
    assert events[0].policy_ids == ()


def test_admission_events_exclude_composition_errors_and_never_mislabel_policy_id() -> None:
    # The read tier lifts a failed resolved check into a top-level resolved_*
    # issue; normalizing both would double-count, so the top-level lift is
    # skipped. Composition/authoring issues are NOT enforcement verdicts, and
    # critically their `reference` is a POLICY id — it must never surface as a
    # workflow_id (the substring-classifier bug).
    resolved = ProjectResolvedWorkflowValidation(
        workflow_id="workflow",
        environment_id="prod",
        ok=False,
        checks=(
            ProjectValidationCheck(code="policy_secrets", status="failed", message="secret X"),
        ),
    )
    issues = (
        ProjectValidationIssue(
            code="resolved_policy_secrets_failed", message="secret X", reference="prod:workflow"
        ),
        ProjectValidationIssue(code="duplicate_workflow_name", message="dup"),
        # reference="base" is a POLICY id, not a workflow id.
        ProjectValidationIssue(
            code="invalid_policy_composition",
            message="base conflicts with regulated",
            reference="base",
        ),
    )
    events = admission_events_from_report(_report(issues=issues, resolved=(resolved,)))

    rules = sorted(event.rule for event in events)
    # Only the genuine policy_secrets verdict; the resolved_* lift, the duplicate-name
    # config issue, and the composition error are all excluded.
    assert rules == ["policy_secrets"]
    # The policy id "base" is NEVER labeled as a workflow id.
    assert all(event.workflow_id != "base" for event in events)


def test_evidence_surface_is_trace_id_only() -> None:
    # P2-10 (#723): langfuse_url/temporal_url had no producer and were removed; the
    # console derives links from trace_id + the bundle (the #719 pattern).
    evidence = EnforcementEvidence(trace_id="t-1")
    assert evidence.model_dump() == {"trace_id": "t-1"}
    with pytest.raises(Exception):
        EnforcementEvidence(langfuse_url="http://x")  # extra="forbid"


def test_observer_from_report_reads_observability_config_check() -> None:
    resolved = ProjectResolvedWorkflowValidation(
        workflow_id="workflow",
        environment_id="prod",
        ok=True,
        checks=(
            ProjectValidationCheck(
                code="observability_config", status="passed", details={"type": "langfuse"}
            ),
        ),
    )
    assert observer_from_report(_report(resolved=(resolved,))) == "langfuse"
    assert observer_from_report(_report()) is None


# ---------------------------------------------------------------------------
# Runtime extraction from Langfuse traces
# ---------------------------------------------------------------------------


def _runtime_trace(
    trace_id: str,
    *,
    workflow_name: str = "SupportWorkflow",
    environment: str = "prod",
    observations: tuple[ObservationRecord, ...],
) -> TraceRecord:
    workflow = build_workflow_execution_manifest(
        workflow_name=workflow_name,
        workflow_id=f"{trace_id}-exec",
        task_queue="support",
        activities=[],
    ).to_dict()
    workflow["code_provenance"]["environment"] = environment
    return TraceRecord(
        trace_id=trace_id,
        timestamp=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        metadata={"typeflux": {"execution_manifest": workflow}},
        observations=observations,
    )


def test_runtime_moderation_block_is_a_blocked_event() -> None:
    trace = _runtime_trace(
        "trace-mod",
        observations=(
            ObservationRecord(
                observation_id="act-1",
                name="classify",
                metadata={
                    "typeflux_moderation": {
                        "decision": "block",
                        "categories": ["hate", "violence"],
                        "max_score": 0.9,
                    }
                },
                start_time=datetime(2026, 7, 1, 12, 0, 5, tzinfo=UTC),
            ),
            # A passed moderation verdict is not an enforcement event.
            ObservationRecord(
                observation_id="act-2",
                metadata={"typeflux_moderation": {"decision": "allow", "categories": []}},
            ),
        ),
    )
    events = runtime_events_from_traces([trace])

    assert len(events) == 1
    event = events[0]
    assert event.source == "runtime"
    assert event.verdict == "blocked"
    assert event.rule == "moderation.on_violation.block"
    assert event.execution_id == "trace-mod-exec"
    assert event.workflow_id == "SupportWorkflow"
    assert event.environment_id == "prod"
    assert event.evidence.trace_id == "trace-mod"
    assert "hate, violence" in event.detail
    assert event.occurred_at == "2026-07-01T12:00:05+00:00"


def test_runtime_no_review_rejection_from_errored_signal_span() -> None:
    # P0-2 (#723): an errored lifecycle-signal span is NOT a review verdict — the
    # same span prefix wraps cancel and marks any transport error, and real review
    # verdicts are decided server-side and never error this client-side span. No
    # honest heuristic exists, so nothing is extracted (a writer-side marker lands
    # in slice 2+).
    trace = _runtime_trace(
        "trace-review",
        observations=(
            ObservationRecord(
                observation_id="sig-1",
                name="TypefluxLifecycleSignal:review",
                level="ERROR",
            ),
        ),
    )
    assert runtime_events_from_traces([trace]) == []


def test_runtime_ignores_non_enforcement_observations() -> None:
    trace = _runtime_trace(
        "trace-clean",
        observations=(
            ObservationRecord(observation_id="ok-1", name="classify"),
            ObservationRecord(
                observation_id="ok-2",
                name="TypefluxLifecycleQuery:status",
                level="DEFAULT",
            ),
        ),
    )
    assert runtime_events_from_traces([trace]) == []


def test_runtime_workflow_name_normalized_to_project_id() -> None:
    # A trace records the workflow TYPE NAME; the mapping normalizes it to the
    # PROJECT id the API filters/validates against. An unmapped name falls back to
    # the raw name.
    mapped = _runtime_trace(
        "trace-mapped",
        workflow_name="SupportWorkflow",
        observations=(
            ObservationRecord(
                observation_id="a",
                metadata={"typeflux_moderation": {"decision": "block", "categories": ["hate"]}},
            ),
        ),
    )
    unmapped = _runtime_trace(
        "trace-unmapped",
        workflow_name="RenamedWorkflow",
        observations=(
            ObservationRecord(
                observation_id="b",
                metadata={"typeflux_moderation": {"decision": "block", "categories": ["hate"]}},
            ),
        ),
    )
    events = runtime_events_from_traces(
        [mapped, unmapped], workflow_name_to_id={"SupportWorkflow": "support"}
    )
    by_exec = {event.execution_id: event.workflow_id for event in events}
    assert by_exec["trace-mapped-exec"] == "support"  # name → project id
    assert by_exec["trace-unmapped-exec"] == "RenamedWorkflow"  # fallback to raw name


def test_runtime_naive_reader_timestamp_is_normalized_not_a_crash() -> None:
    # A naive timestamp from an injected reader must not later blow up window
    # comparison (naive-vs-aware TypeError → 500); it is normalized to aware-UTC at
    # ingestion, so filtering works.
    naive = datetime(2026, 7, 3, 12, 0)  # no tzinfo
    trace = _runtime_trace(
        "trace-naive",
        observations=(
            ObservationRecord(
                observation_id="a",
                metadata={"typeflux_moderation": {"decision": "block", "categories": ["hate"]}},
                start_time=naive,
            ),
        ),
    )
    events = runtime_events_from_traces([trace])
    assert events[0].occurred_at == "2026-07-03T12:00:00+00:00"
    kept = filter_events(
        events,
        since=datetime(2026, 7, 1, tzinfo=UTC),
        until=datetime(2026, 7, 8, tzinfo=UTC),
    )
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# Filtering, ordering, windowing, pagination
# ---------------------------------------------------------------------------


def _event(**kwargs) -> EnforcementEvent:
    base = {"source": "admission", "rule": "policy_provider", "verdict": "rejected", "detail": "d"}
    base.update(kwargs)
    return EnforcementEvent(**base)


def test_filter_by_workflow_environment_and_verdict() -> None:
    events = [
        _event(workflow_id="a", environment_id="prod", verdict="rejected"),
        _event(workflow_id="b", environment_id="prod", verdict="blocked", source="runtime"),
        _event(workflow_id="a", environment_id="staging", verdict="rejected"),
    ]
    assert [e.workflow_id for e in filter_events(events, workflow_ids=("a",))] == ["a", "a"]
    assert [e.environment_id for e in filter_events(events, environment_id="prod")] == [
        "prod",
        "prod",
    ]
    assert [e.verdict for e in filter_events(events, verdicts=("blocked",))] == ["blocked"]


def test_filter_by_policy_id_intersects_recorded_provenance() -> None:
    # The policy_id filter keeps an event whose RECORDED applied policy ids overlap
    # the request set — applied consistently to admission and runtime events.
    events = [
        _event(workflow_id="a", policy_ids=("base",), source="admission"),
        _event(
            workflow_id="b", policy_ids=("base", "regulated"), source="runtime", verdict="blocked"
        ),
        _event(workflow_id="c", policy_ids=("other",), source="admission"),
        _event(workflow_id="d", policy_ids=(), source="admission"),  # no provenance
    ]
    kept = filter_events(events, policy_ids=("regulated",))
    assert [e.workflow_id for e in kept] == ["b"]
    kept_base = filter_events(events, policy_ids=("base",))
    assert sorted(e.workflow_id for e in kept_base) == ["a", "b"]


def test_filter_window_drops_runtime_but_keeps_admission() -> None:
    since = datetime(2026, 7, 1, tzinfo=UTC)
    until = datetime(2026, 7, 8, tzinfo=UTC)
    in_window = _event(source="runtime", verdict="blocked", occurred_at="2026-07-03T00:00:00+00:00")
    out_window = _event(
        source="runtime", verdict="blocked", occurred_at="2026-06-01T00:00:00+00:00"
    )
    admission = _event()  # no occurred_at → always in-window (current state)
    kept = filter_events([in_window, out_window, admission], since=since, until=until)
    assert in_window in kept
    assert admission in kept
    assert out_window not in kept


def test_sort_events_newest_first_admission_before_dated() -> None:
    older = _event(source="runtime", verdict="blocked", occurred_at="2026-07-01T00:00:00+00:00")
    newer = _event(source="runtime", verdict="blocked", occurred_at="2026-07-05T00:00:00+00:00")
    admission = _event()
    ordered = sort_events([older, admission, newer])
    assert ordered == [admission, newer, older]


def test_resolve_window_defaults_to_seven_days() -> None:
    now = datetime(2026, 7, 8, tzinfo=UTC)
    since, until = resolve_window(None, None, now=now)
    assert until == now
    assert since == now - DEFAULT_WINDOW
    assert DEFAULT_WINDOW == timedelta(days=7)


def _fingerprint(**kwargs) -> str:
    base = {"since": datetime(2026, 7, 1, tzinfo=UTC), "until": datetime(2026, 7, 8, tzinfo=UTC)}
    base.update(kwargs)
    return filter_fingerprint(**base)


def test_cursor_round_trips_and_rejects_garbage() -> None:
    fp = _fingerprint()
    assert decode_cursor(None, fingerprint=fp) == 0
    assert decode_cursor(encode_cursor(40, fp), fingerprint=fp) == 40
    with pytest.raises(ValueError):
        decode_cursor("!!!not-base64!!!", fingerprint=fp)


def test_cursor_bound_to_filters_rejects_mismatch() -> None:
    # A cursor minted under one filter set is rejected when reused under another
    # (bound to the filter fingerprint), never a silent skip/dupe.
    minted = encode_cursor(20, _fingerprint(verdicts=("blocked",)))
    with pytest.raises(ValueError):
        decode_cursor(minted, fingerprint=_fingerprint(verdicts=("rejected",)))
    # Same filters → round-trips.
    assert decode_cursor(minted, fingerprint=_fingerprint(verdicts=("blocked",))) == 20


def test_fingerprint_stable_across_unpinned_window() -> None:
    # An unpinned window (since/until None) fingerprints identically regardless of
    # the resolved now() — so pagination stays valid between requests.
    assert filter_fingerprint(environment_id="prod") == filter_fingerprint(environment_id="prod")


def test_paginate_emits_next_cursor_only_when_more_remain() -> None:
    fp = _fingerprint()
    events = [_event(workflow_id=str(i)) for i in range(5)]
    page, cursor = paginate(events, offset=0, limit=2, fingerprint=fp)
    assert [e.workflow_id for e in page] == ["0", "1"]
    assert decode_cursor(cursor, fingerprint=fp) == 2
    last, last_cursor = paginate(events, offset=4, limit=2, fingerprint=fp)
    assert [e.workflow_id for e in last] == ["4"]
    assert last_cursor is None


def test_build_feed_merges_sources_and_reports_partial() -> None:
    resolved = ProjectResolvedWorkflowValidation(
        workflow_id="workflow",
        environment_id="prod",
        ok=False,
        checks=(
            ProjectValidationCheck(code="policy_provider", status="failed", message="blocked"),
        ),
    )
    trace = _runtime_trace(
        "trace-mod",
        observations=(
            ObservationRecord(
                observation_id="a",
                metadata={"typeflux_moderation": {"decision": "block", "categories": ["hate"]}},
                start_time=datetime(2026, 7, 3, tzinfo=UTC),
            ),
        ),
    )
    feed = build_enforcement_feed(
        report=_report(resolved=(resolved,)),
        read_result=EnforcementReadResult("ok", (trace,)),
        environment_id="prod",
        policy_ids=(),
        workflow_ids=(),
        verdicts=(),
        since=datetime(2026, 7, 1, tzinfo=UTC),
        until=datetime(2026, 7, 8, tzinfo=UTC),
        limit=50,
        offset=0,
        cursor_fingerprint="fp",
    )
    assert feed.partial.langfuse == "ok"
    assert {e.source for e in feed.events} == {"admission", "runtime"}
    assert feed.next_cursor is None
    assert feed.since == "2026-07-01T00:00:00+00:00"


def test_build_feed_not_configured_still_serves_admission() -> None:
    resolved = ProjectResolvedWorkflowValidation(
        workflow_id="workflow",
        environment_id="prod",
        ok=False,
        checks=(
            ProjectValidationCheck(code="policy_provider", status="failed", message="blocked"),
        ),
    )
    feed = build_enforcement_feed(
        report=_report(resolved=(resolved,)),
        read_result=EnforcementReadResult("not_configured"),
        environment_id="prod",
        policy_ids=(),
        workflow_ids=(),
        verdicts=(),
        since=datetime(2026, 7, 1, tzinfo=UTC),
        until=datetime(2026, 7, 8, tzinfo=UTC),
        limit=50,
        offset=0,
        cursor_fingerprint="fp",
    )
    assert feed.partial.langfuse == "not_configured"
    assert len(feed.events) == 1  # the admission event still serves.
    assert feed.events[0].source == "admission"
