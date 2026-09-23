"""The cross-surface erasure seam + ``ErasureReceipt`` audit record (#715 slice 5).

``erase_subject`` orchestrates the four surface drivers slices 1-4 shipped — the
keystore crypto-shred (``yaml/subject_keystore.py``), the ``DeleteWorkflowExecution``
driver (``project/erase_executions.py``), the Langfuse subject-trace deletion
(``observability``), and the cache subject invalidation (``execution/cache.py``) —
into ONE dry-run-first operation that emits the compliance artifact: an
:class:`ErasureReceipt`.

Library-first (the #298 ``admit_spec`` precedent): the consuming adopter drives this
seam directly; the ``typeflux erase`` CLI (``project/__main__.py``) is a thin wiring
layer over it. The control-plane contract change is DEFERRED (Phase-C style) with a
recorded re-open trigger: a hosted-console "erase subject" button (then the #616
contract-first flow applies).

Design invariants (docs/design/design-erasure-715.md §5-§7):

* **Dry-run defaults ON** and is provably mutation-free: the keystore is probed with
  the NON-MINTING ``subject_key_state`` introspection (never ``data_key(create=True)``,
  never ``destroy``), the execution driver runs enumeration-only, the trace driver
  lists without ``delete_multiple``, and the cache reads its index without deleting.
  The dry-run receipt has the SAME shape as an executed one — planned
  (``shreddable`` / ``deletable`` / ``trace_ids`` / ``keys_found``) vs performed
  (``shredded`` / ``deleted`` / ``deleted_count`` / ``keys_deleted``).
* **Skipped is loud, never silent**: every surface appears in every receipt; a surface
  whose dependency is not configured (no keystore, no trace reader, no cache store) or
  that was deselected is reported ``skipped`` with an explicit reason.
* **One bad surface never aborts the others**: each surface (and each subject within
  it) is isolated; failures are recorded in the receipt, and :attr:`ErasureReceipt.failed`
  is True whenever ANY selected surface failed — the CLI exits non-zero on it.
* **Ids and counts only** — the receipt never carries erased content, and never other
  subjects' ids (the embedded driver reports already enforce the count-only
  conflicted convention).
* The always-present ``unreachable`` block documents the surfaces Typeflux does NOT
  control (provider logs, exported artifacts) and the mixed-workflow granularity
  limit, so the receipt never over-claims.

The receipt is the PROOF of erasure: it must be persisted OUTSIDE the erased surfaces
(the CLI emits it to stdout with ``--json`` and lets the caller store it).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar

from typeflux.core.subjects import normalize_subject_ids
from typeflux.execution.cache import (
    SubjectCacheErasureReport,
    erase_subject_from_cache,
)
from typeflux.observability.inspect import SubjectTraceDeletionReport
from typeflux.project.erase_executions import (
    SubjectExecutionDeletionReport,
    delete_executions_for_subject,
)
from typeflux.yaml.subject_keystore import (
    SubjectKeystore,
    subject_key_state,
)

__all__ = [
    "ERASURE_SURFACES",
    "CacheSurfaceSection",
    "ErasureReceipt",
    "KeystoreShredEntry",
    "LangfuseSurfaceSection",
    "SubjectSurfaceFailure",
    "TemporalExecutionsSection",
    "TemporalKeystoreSection",
    "TemporalSurfaceSection",
    "UNREACHABLE_SURFACES",
    "UnreachableSurfaceNote",
    "erase_subject",
]

#: The erasure surfaces, in the design's canonical order (§5): temporal, langfuse,
#: cache. The ``surfaces`` selector accepts any subset; execution and the receipt
#: always follow this order.
ERASURE_SURFACES: tuple[str, ...] = ("temporal", "langfuse", "cache")

SurfaceStatus = Literal["ok", "skipped", "failed"]


@dataclass(frozen=True)
class UnreachableSurfaceNote:
    """One document-only surface the erasure cannot reach (§8 non-goals)."""

    surface: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"surface": self.surface, "note": self.note}


#: The ALWAYS-PRESENT document-only surfaces block (§7/§8): erasure cannot reach these,
#: and every receipt says so rather than over-claiming completeness.
UNREACHABLE_SURFACES: tuple[UnreachableSurfaceNote, ...] = (
    UnreachableSurfaceNote(
        surface="provider_logs",
        note=(
            "provider-owned: Typeflux cannot delete the model provider's request logs. "
            "Erase via your provider's retention/deletion controls (see docs/privacy.md "
            "'Retention & Erasure')."
        ),
    ),
    UnreachableSurfaceNote(
        surface="exported_artifacts",
        note=(
            "caller-owned: manifests and audit bundles exported out of Typeflux are "
            "outside its control — the exporter is responsible for erasing them."
        ),
    ),
    UnreachableSurfaceNote(
        surface="mixed_workflow_payloads",
        note=(
            "granularity limit: per-payload shred INSIDE a mixed-subject workflow is "
            "infeasible with the content-blind whole-Payload codec. A mixed execution's "
            "payloads are sealed under a key combined from ALL its subjects' records, "
            "so erasing any member shreds the shared history wholesale — never one "
            "subject's slice of it."
        ),
    ),
)


@dataclass(frozen=True)
class SubjectSurfaceFailure:
    """One per-subject driver failure inside a surface — recorded, never swallowed."""

    subject_id: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {"subject_id": self.subject_id, "error": self.error}


@dataclass(frozen=True)
class KeystoreShredEntry:
    """One subject's keystore outcome, same shape on dry-run and execute.

    ``state_before`` is the record's state before this operation (``live`` /
    ``destroyed`` / ``absent``) — probed WITHOUT minting on a dry run, derived from
    the destruction result on execute. ``would_shred`` is the PLAN (a live record
    exists to destroy); ``shredded`` is the PERFORMED outcome (always False on a dry
    run). A dry run never writes: no mint, no tombstone.
    """

    subject_id: str
    state_before: Literal["live", "destroyed", "absent"]
    would_shred: bool
    shredded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "state_before": self.state_before,
            "would_shred": self.would_shred,
            "shredded": self.shredded,
        }


@dataclass(frozen=True)
class TemporalKeystoreSection:
    """The crypto-shred half of the temporal surface (§4.1 primary mechanism)."""

    status: SurfaceStatus
    skip_reason: str | None = None
    entries: tuple[KeystoreShredEntry, ...] = ()
    #: The PLAN count: live records that would be (or were about to be) destroyed.
    shreddable_key_records: int = 0
    #: The PERFORMED count: live records actually destroyed (0 on a dry run).
    shredded_key_records: int = 0
    failures: tuple[SubjectSurfaceFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.skip_reason is not None:
            result["skip_reason"] = self.skip_reason
            return result
        result["entries"] = [entry.to_dict() for entry in self.entries]
        result["shreddable_key_records"] = self.shreddable_key_records
        result["shredded_key_records"] = self.shredded_key_records
        result["failures"] = [failure.to_dict() for failure in self.failures]
        return result


@dataclass(frozen=True)
class TemporalExecutionsSection:
    """The ``DeleteWorkflowExecution`` half of the temporal surface (§4.1 complement).

    ``reports`` carries the slice-4 driver's audit-honest report VERBATIM per subject
    (deletable-vs-deleted, conflicted categories, still-running, index coverage,
    truncation warnings) — the receipt aggregates, it does not re-shape.
    """

    status: SurfaceStatus
    skip_reason: str | None = None
    reports: tuple[SubjectExecutionDeletionReport, ...] = ()
    failures: tuple[SubjectSurfaceFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.skip_reason is not None:
            result["skip_reason"] = self.skip_reason
            return result
        result["reports"] = [report.to_dict() for report in self.reports]
        result["failures"] = [failure.to_dict() for failure in self.failures]
        return result


@dataclass(frozen=True)
class TemporalSurfaceSection:
    """The temporal surface: keystore shred + execution deletion, independently wired.

    The two mechanisms have independent dependencies (a keystore backend vs a Temporal
    client), so each half carries its own skipped/failed state; the surface ``status``
    is ``failed`` if either half failed, ``skipped`` only when BOTH are, else ``ok``.
    """

    status: SurfaceStatus
    keystore: TemporalKeystoreSection
    executions: TemporalExecutionsSection
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.skip_reason is not None:
            result["skip_reason"] = self.skip_reason
            return result
        result["keystore"] = self.keystore.to_dict()
        result["executions"] = self.executions.to_dict()
        return result


@dataclass(frozen=True)
class LangfuseSurfaceSection:
    """The Langfuse surface: the slice-2 dual-channel trace-deletion reports verbatim."""

    status: SurfaceStatus
    skip_reason: str | None = None
    reports: tuple[SubjectTraceDeletionReport, ...] = ()
    failures: tuple[SubjectSurfaceFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.skip_reason is not None:
            result["skip_reason"] = self.skip_reason
            return result
        result["reports"] = [report.to_dict() for report in self.reports]
        result["failures"] = [failure.to_dict() for failure in self.failures]
        return result


@dataclass(frozen=True)
class CacheSurfaceSection:
    """The cache surface: the slice-3 erasure reports verbatim (incl. not-supported)."""

    status: SurfaceStatus
    skip_reason: str | None = None
    reports: tuple[SubjectCacheErasureReport, ...] = ()
    failures: tuple[SubjectSurfaceFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.skip_reason is not None:
            result["skip_reason"] = self.skip_reason
            return result
        result["reports"] = [report.to_dict() for report in self.reports]
        result["failures"] = [failure.to_dict() for failure in self.failures]
        return result


@dataclass(frozen=True)
class ErasureReceipt:
    """The audit record of one erasure run (§7) — ids/counts only, NEVER erased content.

    The same shape on dry-run and execute (planned-vs-performed lives inside the
    surface sections). Every receipt carries all three surfaces (selected or skipped
    with a reason), the always-present ``unreachable`` document-only block, and the
    aggregated ``warnings``. The receipt is the proof of erasure — persist it OUTSIDE
    the erased surfaces.
    """

    subject_ids: tuple[str, ...]
    executed_at: str
    actor: str
    dry_run: bool
    temporal: TemporalSurfaceSection
    langfuse: LangfuseSurfaceSection
    cache: CacheSurfaceSection
    unreachable: tuple[UnreachableSurfaceNote, ...] = UNREACHABLE_SURFACES
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> bool:
        """True when ANY surface failed — the CLI's non-zero-exit signal.

        A failure is unmistakable: a driver raised, or an executed driver reported
        per-item failures. Skipped surfaces are not failures (they are loudly
        reported as skipped instead)."""

        return (
            self.temporal.status == "failed"
            or self.langfuse.status == "failed"
            or self.cache.status == "failed"
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict (omit-absent, matching the slice-4 report convention)."""

        return {
            "subject_ids": list(self.subject_ids),
            "executed_at": self.executed_at,
            "actor": self.actor,
            "dry_run": self.dry_run,
            "surfaces": {
                "temporal": self.temporal.to_dict(),
                "langfuse": self.langfuse.to_dict(),
                "cache": self.cache.to_dict(),
            },
            "unreachable": [note.to_dict() for note in self.unreachable],
            "warnings": list(self.warnings),
        }


def _normalize_surfaces(surfaces: Sequence[str]) -> tuple[str, ...]:
    """Validate the surface selection: known names only, at least one, canonical order."""

    if isinstance(surfaces, str):
        # A bare string would char-iterate ("temporal" -> "t", "e", ...); reject loudly.
        raise ValueError(
            "surfaces must be a sequence of surface names, not a bare string "
            f"(got {surfaces!r}); pass e.g. ('temporal', 'langfuse', 'cache')"
        )
    selected = set(surfaces)
    unknown = selected - set(ERASURE_SURFACES)
    if unknown:
        raise ValueError(
            f"unknown erasure surface(s) {sorted(unknown)!r}; valid surfaces are "
            f"{list(ERASURE_SURFACES)!r}"
        )
    if not selected:
        raise ValueError(
            "erase_subject needs at least one surface; pass surfaces from "
            f"{list(ERASURE_SURFACES)!r}"
        )
    return tuple(name for name in ERASURE_SURFACES if name in selected)


_DESELECTED = "surface not selected for this erasure run (surfaces argument)"


class _PerSubjectReport(Protocol):
    """What the shared per-subject runner needs of a driver report: its failures."""

    @property
    def failures(self) -> Sequence[Any]: ...


_ReportT = TypeVar("_ReportT", bound=_PerSubjectReport)


async def _foreach_subject(
    subject_ids: Sequence[str],
    call: Callable[[str], Awaitable[_ReportT]],
) -> tuple[tuple[_ReportT, ...], tuple[SubjectSurfaceFailure, ...], bool]:
    """Run one surface driver per subject, fail-isolated (the shared skeleton).

    Sequential per-subject: erasure subject sets are small in practice and every
    driver already bounds its own work; bounded fan-out (the PLAN_PR_LOOKUP_CONCURRENCY
    pattern) is deferred until a real large-scale need — the same deferral the slice-4
    delete loop records. Returns ``(reports, failures, failed)`` where ``failed`` is
    True when any subject's call raised OR any returned report carries per-item
    failures (an executed driver's partial failure is still a failed surface).
    """

    reports: list[_ReportT] = []
    failures: list[SubjectSurfaceFailure] = []
    for subject_id in subject_ids:
        try:
            reports.append(await call(subject_id))
        except Exception as exc:  # noqa: BLE001 - fail-isolated, recorded per subject.
            failures.append(
                SubjectSurfaceFailure(subject_id=subject_id, error=f"{type(exc).__name__}: {exc}")
            )
    failed = bool(failures) or any(report.failures for report in reports)
    return tuple(reports), tuple(failures), failed


def _shred_subjects(
    keystore: SubjectKeystore,
    subject_ids: Sequence[str],
    *,
    dry_run: bool,
) -> TemporalKeystoreSection:
    """Probe (dry run) or destroy (execute) each subject's key record, fail-isolated."""

    entries: list[KeystoreShredEntry] = []
    failures: list[SubjectSurfaceFailure] = []
    for subject_id in subject_ids:
        try:
            if dry_run:
                # NON-MINTING introspection: a dry run must never mint a record
                # (mint-on-first-use is encode-path only) nor leave a tombstone.
                state = subject_key_state(keystore, subject_id)
                entries.append(
                    KeystoreShredEntry(
                        subject_id=subject_id,
                        state_before=state,
                        would_shred=state == "live",
                        shredded=False,
                    )
                )
            else:
                result = keystore.destroy_subject_key(subject_id)
                state_before: Literal["live", "destroyed", "absent"]
                if result.key_existed:
                    state_before = "live"
                elif result.already_destroyed:
                    state_before = "destroyed"
                else:
                    state_before = "absent"
                entries.append(
                    KeystoreShredEntry(
                        subject_id=subject_id,
                        state_before=state_before,
                        would_shred=result.key_existed,
                        shredded=result.key_existed,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - fail-isolated, recorded per subject.
            failures.append(
                SubjectSurfaceFailure(subject_id=subject_id, error=f"{type(exc).__name__}: {exc}")
            )
    return TemporalKeystoreSection(
        status="failed" if failures else "ok",
        entries=tuple(entries),
        shreddable_key_records=sum(1 for entry in entries if entry.would_shred),
        shredded_key_records=sum(1 for entry in entries if entry.shredded),
        failures=tuple(failures),
    )


async def _delete_subject_executions(
    client: Any,
    subject_ids: Sequence[str],
    *,
    namespace: str | None,
    dry_run: bool,
    limit: int,
) -> TemporalExecutionsSection:
    """Run the slice-4 execution-deletion driver per subject, fail-isolated."""

    async def _call(subject_id: str) -> SubjectExecutionDeletionReport:
        return await delete_executions_for_subject(
            client, subject_id, namespace=namespace, dry_run=dry_run, limit=limit
        )

    reports, failures, failed = await _foreach_subject(subject_ids, _call)
    return TemporalExecutionsSection(
        status="failed" if failed else "ok", reports=reports, failures=failures
    )


async def _delete_subject_traces(
    trace_reader: Any,
    subject_ids: Sequence[str],
    *,
    dry_run: bool,
    since: datetime | None,
    until: datetime | None,
) -> LangfuseSurfaceSection:
    """Run the slice-2 trace-deletion driver per subject, fail-isolated."""

    async def _call(subject_id: str) -> SubjectTraceDeletionReport:
        report: SubjectTraceDeletionReport = trace_reader.delete_traces_for_subject(
            subject_id, dry_run=dry_run, since=since, until=until
        )
        return report

    reports, failures, failed = await _foreach_subject(subject_ids, _call)
    return LangfuseSurfaceSection(
        status="failed" if failed else "ok", reports=reports, failures=failures
    )


async def _erase_subject_cache(
    cache_store: object,
    subject_ids: Sequence[str],
    *,
    dry_run: bool,
    require_targeted: bool = False,
) -> CacheSurfaceSection:
    """Run the slice-3 cache erasure per subject, fail-isolated.

    A store without the :class:`SubjectErasableCacheStore` capability yields the
    slice-3 not-supported report (``supported=False`` + the full-flush fallback) —
    honest, but NOT a failure (the fallback is documented, never automatic) UNLESS the
    spec declares ``runtime.cache_erasure: targeted`` (#795): a declared requirement
    makes the incapable store a loud per-subject FAILURE, never a fallback note."""

    async def _call(subject_id: str) -> SubjectCacheErasureReport:
        report = erase_subject_from_cache(cache_store, subject_id, dry_run=dry_run)
        if require_targeted and not report.supported:
            raise RuntimeError(
                "runtime.cache_erasure is 'targeted' but the wired cache store "
                f"({type(cache_store).__module__}.{type(cache_store).__qualname__}) does not "
                "implement SubjectErasableCacheStore — the declared requirement forbids the "
                "full-flush fallback; wire an erasable store or drop the declaration"
            )
        return report

    reports, failures, failed = await _foreach_subject(subject_ids, _call)
    return CacheSurfaceSection(
        status="failed" if failed else "ok", reports=reports, failures=failures
    )


def _aggregate_warnings(
    temporal: TemporalSurfaceSection,
    langfuse: LangfuseSurfaceSection,
    cache: CacheSurfaceSection,
) -> list[str]:
    """Roll the surface-level caveats an operator must see up to the receipt (§7).

    Driver scan-completeness warnings (truncation, page caps), still-running
    executions (reported, never touched), conflicted exclusions (fail-safe skips the
    operator must handle deliberately), and the cache full-flush fallback. The
    always-present index-coverage caveats stay inside the embedded reports."""

    warnings: list[str] = []
    for report in temporal.executions.reports:
        warnings.extend(f"temporal: {warning}" for warning in report.warnings)
        if report.still_running:
            warnings.append(
                f"temporal: {len(report.still_running)} running execution(s) for subject "
                f"{report.subject_id!r} were reported but NOT touched — erase never "
                "terminates; re-run after they close, or cancel/migrate them first."
            )
        if report.conflicted:
            warnings.append(
                f"temporal: {len(report.conflicted)} matched execution(s) for subject "
                f"{report.subject_id!r} were excluded fail-safe (multi-subject, "
                "unreadable subjects, stale index, or unknown status) — see the "
                "temporal surface report and handle them deliberately."
            )
    for trace_report in langfuse.reports:
        warnings.extend(f"langfuse: {warning}" for warning in trace_report.warnings)
        if trace_report.conflicted:
            warnings.append(
                f"langfuse: {len(trace_report.conflicted)} matched trace(s) for subject "
                f"{trace_report.subject_id!r} were excluded fail-safe (they carry other "
                "subjects' markers, or their tags were unreadable) — see the langfuse "
                "surface report and handle them deliberately."
            )
    for cache_report in cache.reports:
        if not cache_report.supported and cache_report.full_flush_fallback is not None:
            warnings.append(f"cache: {cache_report.full_flush_fallback}")
        warnings.extend(
            f"cache: erasure failure for subject {cache_report.subject_id!r}: {failure}"
            for failure in cache_report.failures
        )
    # Order-preserving dedupe: repeated per-subject caveats collapse to one line.
    seen: set[str] = set()
    unique: list[str] = []
    for warning in warnings:
        if warning not in seen:
            seen.add(warning)
            unique.append(warning)
    return unique


async def erase_subject(
    subject_ids: str | Sequence[str],
    *,
    actor: str,
    dry_run: bool = True,
    surfaces: Sequence[str] = ERASURE_SURFACES,
    temporal_client: Any | None = None,
    temporal_namespace: str | None = None,
    subject_keystore: SubjectKeystore | None = None,
    trace_reader: Any | None = None,
    cache_store: object | None = None,
    require_targeted_cache: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
    execution_limit: int = 1000,
) -> ErasureReceipt:
    """Erase subject(s) across the configured surfaces and return the audit receipt.

    ``subject_ids`` is one id or a sequence (validated non-empty strings, order
    preserved, de-duplicated); an EMPTY set is a loud error — an erasure for zero
    subjects is meaningless. ``actor`` names who ran it (a compliance artifact must
    carry a principal). ``dry_run`` defaults ON and performs ZERO mutation.

    Surface dependencies are injected: ``temporal_client`` (+ optional
    ``temporal_namespace``) drives execution deletion, ``subject_keystore`` drives the
    crypto-shred, ``trace_reader`` (any object with the slice-2
    ``delete_traces_for_subject``) drives Langfuse, ``cache_store`` drives the cache.
    A selected surface whose dependency is ``None`` is reported skipped-with-reason —
    never silently omitted, never a crash.

    ``since``/``until`` bound the Langfuse trace scan window. The temporal and cache
    surfaces have NO windowing (enumeration and the key/cache indexes are
    window-less): selecting them alongside a window adds an explicit unsupported note
    to the receipt, and a window whose selection includes no windowable surface is a
    loud error (it would be entirely inert).
    """

    normalized_ids = normalize_subject_ids(
        [subject_ids] if isinstance(subject_ids, str) else subject_ids
    )
    if not normalized_ids:
        raise ValueError(
            "erase_subject requires at least one subject id — an erasure for zero "
            "subjects is meaningless"
        )
    if not actor or actor.strip() != actor:
        raise ValueError(
            "erase_subject requires a non-empty, trimmed actor (the receipt is a "
            "compliance artifact and must name who ran it)"
        )
    if execution_limit < 1:
        # Fail closed (#715 Bugbot): a non-positive limit would enumerate NOTHING and
        # yield an empty, healthy-looking plan with only a truncation note — the
        # temporal deletion would silently do nothing.
        raise ValueError(
            f"execution_limit must be >= 1 (got {execution_limit}); a non-positive "
            "limit would enumerate no executions and report an empty plan as if the "
            "subject had none"
        )
    selected = _normalize_surfaces(surfaces)
    has_window = since is not None or until is not None
    if has_window and "langfuse" not in selected:
        raise ValueError(
            "since/until bound the Langfuse trace scan window, but the langfuse surface "
            "is not selected — the window would be silently inert. Select langfuse or "
            "drop the window (the temporal and cache surfaces are window-less)."
        )

    seam_warnings: list[str] = []
    if has_window:
        for windowless in ("temporal", "cache"):
            if windowless in selected:
                seam_warnings.append(
                    f"{windowless}: since/until do not apply to this surface (its "
                    "enumeration and indexes are window-less); the window bounds only "
                    "the langfuse trace scan."
                )

    # --- temporal (keystore shred + execution deletion), design order first ----------
    if "temporal" not in selected:
        temporal_section = TemporalSurfaceSection(
            status="skipped",
            skip_reason=_DESELECTED,
            keystore=TemporalKeystoreSection(status="skipped", skip_reason=_DESELECTED),
            executions=TemporalExecutionsSection(status="skipped", skip_reason=_DESELECTED),
        )
    else:
        if subject_keystore is None:
            keystore_section = TemporalKeystoreSection(
                status="skipped",
                skip_reason=(
                    "no SubjectKeystore backend was provided: the crypto-shred surface "
                    "cannot be driven from this process. Inject the deployment's SHARED "
                    "keystore backend (the process-local in-memory reference keystore "
                    "holds no records minted elsewhere) — see docs/privacy.md "
                    "'Keystore backends'."
                ),
            )
        else:
            keystore_section = _shred_subjects(subject_keystore, normalized_ids, dry_run=dry_run)
        if temporal_client is None:
            executions_section = TemporalExecutionsSection(
                status="skipped",
                skip_reason=(
                    "no Temporal client was provided: subject-dedicated execution "
                    "deletion (DeleteWorkflowExecution) cannot run. Pass a connected "
                    "client to drive it."
                ),
            )
        else:
            executions_section = await _delete_subject_executions(
                temporal_client,
                normalized_ids,
                namespace=temporal_namespace,
                dry_run=dry_run,
                limit=execution_limit,
            )
        if keystore_section.status == "failed" or executions_section.status == "failed":
            temporal_status: SurfaceStatus = "failed"
        elif keystore_section.status == "skipped" and executions_section.status == "skipped":
            temporal_status = "skipped"
        else:
            temporal_status = "ok"
        temporal_section = TemporalSurfaceSection(
            status=temporal_status,
            keystore=keystore_section,
            executions=executions_section,
        )

    # --- langfuse ---------------------------------------------------------------------
    if "langfuse" not in selected:
        langfuse_section = LangfuseSurfaceSection(status="skipped", skip_reason=_DESELECTED)
    elif trace_reader is None:
        langfuse_section = LangfuseSurfaceSection(
            status="skipped",
            skip_reason=(
                "no trace reader was provided: the Langfuse surface cannot be driven "
                "from this process. Configure the langfuse observability backend "
                "(LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY, plus LANGFUSE_HOST for "
                "self-hosted) or pass a reader exposing delete_traces_for_subject."
            ),
        )
    else:
        langfuse_section = await _delete_subject_traces(
            trace_reader,
            normalized_ids,
            dry_run=dry_run,
            since=since,
            until=until,
        )

    # --- cache ------------------------------------------------------------------------
    if "cache" not in selected:
        cache_section = CacheSurfaceSection(status="skipped", skip_reason=_DESELECTED)
    elif cache_store is None:
        if require_targeted_cache:
            # #795: under a declared targeted requirement, a SELECTED cache surface with no
            # store wired is a loud failure — "skipped" would read as the requirement being
            # satisfied while nothing was verified or erased.
            cache_section = CacheSurfaceSection(
                status="failed",
                failures=tuple(
                    SubjectSurfaceFailure(
                        subject_id=subject_id,
                        error=(
                            "runtime.cache_erasure is 'targeted' and the cache surface was "
                            "selected, but no cache store was provided to this erase "
                            "invocation — pass the deployment's SubjectErasableCacheStore "
                            "(--cache-store-class) so the declared requirement can actually "
                            "be verified and driven"
                        ),
                    )
                    for subject_id in normalized_ids
                ),
            )
        else:
            cache_section = CacheSurfaceSection(
                status="skipped",
                skip_reason=(
                    "no cache store was provided: the cache surface cannot be driven from "
                    "this process. Pass the deployment's CacheStore (a "
                    "SubjectErasableCacheStore for per-subject invalidation) to drive it."
                ),
            )
    else:
        cache_section = await _erase_subject_cache(
            cache_store, normalized_ids, dry_run=dry_run, require_targeted=require_targeted_cache
        )

    warnings = [
        *seam_warnings,
        *_aggregate_warnings(temporal_section, langfuse_section, cache_section),
    ]
    return ErasureReceipt(
        subject_ids=normalized_ids,
        executed_at=datetime.now(UTC).isoformat(),
        actor=actor,
        dry_run=dry_run,
        temporal=temporal_section,
        langfuse=langfuse_section,
        cache=cache_section,
        unreachable=UNREACHABLE_SURFACES,
        warnings=tuple(warnings),
    )
