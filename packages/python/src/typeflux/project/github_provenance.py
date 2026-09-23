"""GitHub provenance reads (#727 §1): a normalized, read-at-request surface that
relates the code the control plane *serves* to the code that lives in GitHub —
two drift/audit signals the platform cannot compute from its own state alone:

* **HEAD-vs-served drift** (#577 §1): the served project's recorded git provenance
  (``repo_url`` + ``ref`` + the checkout ``sha`` the registry/git-source recorded)
  compared against the remote branch HEAD (``GET /repos/{owner}/{repo}/commits/{branch}``).
  "This project changed in GitHub but the control plane hasn't picked it up" becomes
  a first-class row (``head.ahead_of_served`` + a bounded ``head.commits_behind``),
  where today only a dirty checkout is an un-reconciled-state signal.
* **Plan → approving PR** (#577 §6): the deployment plan FILE is authored, committed, and
  merged through a review PR *after* the code checkout the plan pins — so a plan's review
  record is the PR that merged its own plan file, NOT the checkout ``sha`` the plan was
  resolved from (``identity.code.sha`` is the plan-GENERATION-time HEAD and predates the
  file's merge commit; attributing the PR to it finds unrelated PRs or none). Each plan's
  ``sha`` is therefore resolved as its plan file's last commit in the served clone
  (``git log -1 --format=%H -- <plan-file>`` — the ``git log`` plumbing), and the PR that
  merged that commit (``GET /repos/{owner}/{repo}/commits/{sha}/pulls``) links every plan
  to its review record. A plan file that is untracked/uncommitted carries ``sha: null`` and
  ``pr: null`` — never a fabricated link (misattribution is worse than absence).

The control plane persists nothing: every request re-reads the served provenance and
re-queries GitHub through the injected transport seam (#573/#723 posture). The read is
**bounded** — one HEAD lookup, at most one ``compare`` for the behind-count, and PR
lookups capped at :data:`PLAN_PR_LOOKUP_CAP` of the most-recent plans (run through a small
bounded thread pool, not one blocking urllib call after another) — never an unbounded
fan-out.

Reachability **degrades loudly** into ``partial.github``:

* ``not_configured`` — no server-side token (``TYPEFLUX_GITHUB_TOKEN`` /
  ``GITHUB_TOKEN``) is set, OR the served project has no GitHub repo provenance. **No
  network call is made** (the Langfuse-seam posture: never a silent empty ``ok``).
* ``rate_limited`` — GitHub answered 403 with the rate-limit signal, or a 429.
* ``unreachable`` — any other genuine transport/HTTP failure.
* ``ok`` — the reads succeeded.

The reads are **per-step isolated** so a routine per-resource miss never nukes the whole
response: a 404/422 on one plan's ``/commits/{sha}/pulls`` (a commit with no PR) leaves that
plan's ``pr: null`` and the loop continues; a ``compare`` failure (force-push / no common
ancestor — exactly when drift matters) keeps the fetched HEAD with ``commits_behind: null``.
And the **HEAD signal, once fetched, survives a later degradation**: if the remote HEAD read
succeeds and a subsequent ``compare``/PR read hits a genuine transport failure, the reader
returns the fetched ``head_sha`` alongside the degraded ``partial`` marker rather than
discarding the endpoint's primary signal — so ``head`` is populated whenever the HEAD was
actually read (a plan's ``sha`` is always local; only its ``pr`` needs the network).

Every helper below is a **pure function** over data; the only impurity is
:func:`default_github_reader`, the transport seam the control plane injects and tests
replace with a fixture.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, NamedTuple
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict

#: The most-recent approved plans whose PR is looked up per request — the fan-out
#: bound (#727: bounded total API calls). Plans are ordered newest-first by
#: ``generated_at`` and the tail is listed WITHOUT a PR lookup (``pr: null``),
#: never dropped — an operator still sees every plan.
PLAN_PR_LOOKUP_CAP = 20
#: Hard cap on the reported ``commits_behind`` so a long-diverged branch reports a
#: bounded, honest "20+" rather than a huge number the console renders literally.
MAX_COMMITS_BEHIND = 100
#: Max concurrent plan-PR lookups (#727 P1-5): the capped plan set is fanned out through a
#: small thread pool rather than issued as serial blocking urllib calls (each 100-300ms, and
#: the route already holds a Starlette threadpool worker) — bounded so the reader never
#: opens a socket per plan.
PLAN_PR_LOOKUP_CONCURRENCY = 6
#: GitHub REST base and per-call timeout for the default reader.
_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_TIMEOUT_SECONDS = 10.0

#: The reachability vocabulary the surface degrades into (``partial.github``). Sibling of
#: :data:`typeflux.project.enforcement.LangfuseStatus` (the enforcement-events
#: triad); a shared reachability module is deferred until a third best-effort source appears
#: (#577) — until then the two shapes are kept parallel by convention, not a common type.
GithubStatus = Literal["ok", "unreachable", "not_configured", "rate_limited"]


class GithubRepo(NamedTuple):
    """A GitHub repository parsed from a recorded ``repo_url``: the ``owner``/``repo``
    the REST API is addressed by, plus the validated web ``url`` links derive from."""

    owner: str
    repo: str
    url: str


class ServedProvenance(NamedTuple):
    """The served side of the drift comparison: the GitHub repo, the branch/ref the
    control plane serves (always set — ``ProjectRepoSource.ref`` defaults to ``main``), and
    the checkout ``sha`` it recorded (``None`` when the source recorded a repo but no
    resolved sha — e.g. a not-yet-cloned git source)."""

    repo: GithubRepo
    branch: str
    served_sha: str | None


class PlanRef(NamedTuple):
    """One approved plan reduced to what the provenance surface needs: its id and the
    commit ``sha`` of its plan FILE's last commit in the served clone (``None`` when the
    plan file is untracked/uncommitted — then its ``pr`` is null-with-reason, never
    fabricated). This is the plan file's own merge history, NOT the checkout ``code.sha``
    the plan was resolved from (which predates the file)."""

    plan_id: str
    sha: str | None


class PullRequestRef(BaseModel):
    """The approving PR for a plan's commit (#727 §6): the review record every approved
    plan is one click from. ``merged_at`` is present for a merged PR (the audit case),
    absent for an open/unmerged PR that still touched the commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    url: str
    merged_at: str | None = None


class HeadProvenance(BaseModel):
    """The remote branch HEAD relative to what the control plane serves (#727 §1).
    ``sha`` is the remote HEAD commit; ``branch`` is the served ref (always set —
    ``ProjectRepoSource.ref`` defaults to ``main``); ``ahead_of_served`` is whether the
    remote HEAD differs from the served checkout, or ``null`` when the served checkout sha
    is unresolved (a not-yet-cloned source — drift is UNKNOWN, never a false "in sync");
    ``commits_behind`` is how many commits the served checkout is behind HEAD (bounded by
    :data:`MAX_COMMITS_BEHIND`), omitted when it cannot be computed (e.g. no served sha, or
    the compare read failed/was not attempted)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str
    sha: str
    ahead_of_served: bool | None = None
    commits_behind: int | None = None


class PlanProvenance(BaseModel):
    """One approved deployment plan linked to its plan-file commit and approving PR
    (#727 §6). ``sha`` is the plan FILE's last commit in the served clone (null when the
    plan file is untracked/uncommitted); ``pr`` is the merged/approving PR that merged that
    commit (null when unknown, unreachable, or the plan has no sha to resolve — never
    fabricated)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    sha: str | None = None
    pr: PullRequestRef | None = None


class GithubPartial(BaseModel):
    """Loud-degradation marker (#727): the reachability of the GitHub source. Plans are
    always listed from local plan files (their ``sha`` is local); only ``head`` and each
    plan's ``pr`` need the network, so a degraded status leaves those unpopulated rather
    than dropping the response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    github: GithubStatus


class GithubProvenance(BaseModel):
    """The github-provenance response envelope (#727). ``head`` is ``null`` when there is
    no served GitHub provenance to compare or the remote HEAD could not be read; ``plans``
    lists every approved plan (most-recent first) with its commit + PR link where known."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    head: HeadProvenance | None = None
    plans: tuple[PlanProvenance, ...] = ()
    partial: GithubPartial


class GithubReadResult(NamedTuple):
    """The transport seam's result: a reachability ``status`` (mapped straight into the
    ``partial`` marker), the remote HEAD ``sha`` (``None`` when the HEAD read itself did not
    succeed; set even under a degraded ``status`` when the HEAD was read before a later
    failure), the bounded ``commits_behind`` count (``None`` when not computed), and the
    resolved PRs keyed by the plan commit ``sha`` (only shas whose PR was found appear)."""

    status: GithubStatus
    head_sha: str | None = None
    commits_behind: int | None = None
    plan_prs: Mapping[str, PullRequestRef] = {}


# ---------------------------------------------------------------------------
# Pure helpers: repo-url host gate, served/plan extraction, feed assembly.
# ---------------------------------------------------------------------------


def parse_github_repo(repo_url: str | None) -> GithubRepo | None:
    """Validate a recorded ``repo_url`` to a github.com WEB repo and split its
    ``owner``/``repo``, or ``None``.

    Mirrors the console host gate (``clients/console/src/links.ts`` #718) and its TS
    control-plane twin (``packages/typescript/temporal-controlplane/src/github-provenance.ts``
    ``parseGithubRepo`` #727) — keep all three host gates in lockstep: the host must
    be exactly ``github.com`` or a ``*.github.com`` subdomain (anchoring the REGISTRABLE
    domain — a prefix match would pass ``github.com.evil.com``), the scheme must be
    http(s) (excludes ``git@github.com:...`` ssh shorthand and local paths), and the
    path must carry ``owner/repo``. Self-hosted GHE hosts are unverifiable and stay
    ungated (same posture as the console), so their repo provenance is treated as
    non-github (capability false)."""
    if not repo_url:
        return None
    try:
        parsed = urlparse(repo_url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if host is None or not (host == "github.com" or host.endswith(".github.com")):
        return None
    # owner/repo are the first two non-empty path segments; a trailing ``.git`` (rare on
    # a web url but possible on a recorded remote) is stripped.
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return None
    return GithubRepo(owner=owner, repo=repo, url=repo_url)


def served_provenance(
    *, repo_url: str | None, repo_ref: str, repo_sha: str | None
) -> ServedProvenance | None:
    """Build the served side of the drift comparison, or ``None`` when the recorded
    source is not a github repo (a local mount records no source; a non-github/GHE host
    fails the gate) — the caller reports that as ``not_configured`` with no network call,
    and the ``github_provenance`` capability is false for it."""
    repo = parse_github_repo(repo_url)
    if repo is None:
        return None
    return ServedProvenance(repo=repo, branch=repo_ref, served_sha=repo_sha)


def plan_refs(plans: Sequence[PlanRef], *, cap: int = PLAN_PR_LOOKUP_CAP) -> list[PlanRef]:
    """The plans whose PR is worth looking up: the input is assumed newest-first
    (the caller sorts by ``generated_at``); the first ``cap`` with a non-null sha are the
    lookup set. This is a pure selector over already-ordered refs; the assembler still
    lists EVERY plan, only the PR lookup is bounded."""
    selected: list[PlanRef] = []
    for ref in plans:
        if ref.sha is None:
            continue
        selected.append(ref)
        if len(selected) >= cap:
            break
    return selected


def build_github_provenance(
    *,
    served: ServedProvenance | None,
    plans: Sequence[PlanRef],
    read_result: GithubReadResult,
) -> GithubProvenance:
    """Compose the response from the served provenance, the ordered plans, and the
    reader's result — the one place the local and remote sources are merged.

    ``head`` is populated whenever the remote HEAD was actually READ (a head sha is
    present) — independent of a later degradation, so a transport failure on a ``compare``
    or plan-PR read AFTER HEAD succeeded keeps the fetched HEAD rather than discarding the
    endpoint's primary signal (the degradation still shows in ``partial.github``). It is
    ``null`` only when the HEAD read itself did not succeed (no served provenance, or the
    HEAD lookup failed). ``ahead_of_served`` is ``null`` when the served checkout sha is
    unresolved (drift UNKNOWN, never a false "in sync"). Every plan is listed with its local
    ``sha``; its ``pr`` comes from ``read_result.plan_prs`` when the sha resolved, else
    ``null`` (unknown/unreachable/no-sha — never fabricated)."""
    head: HeadProvenance | None = None
    if served is not None and read_result.head_sha is not None:
        head = HeadProvenance(
            branch=served.branch,
            sha=read_result.head_sha,
            ahead_of_served=(
                None if served.served_sha is None else read_result.head_sha != served.served_sha
            ),
            commits_behind=read_result.commits_behind,
        )
    plan_provenance = tuple(
        PlanProvenance(
            plan_id=ref.plan_id,
            sha=ref.sha,
            pr=read_result.plan_prs.get(ref.sha) if ref.sha is not None else None,
        )
        for ref in plans
    )
    return GithubProvenance(
        head=head,
        plans=plan_provenance,
        partial=GithubPartial(github=read_result.status),
    )


# ---------------------------------------------------------------------------
# The transport seam (the only impure function) — injected by the control plane.
# ---------------------------------------------------------------------------


def _github_token() -> str | None:
    """The server-side GitHub token: ``TYPEFLUX_GITHUB_TOKEN`` preferred, ``GITHUB_TOKEN``
    fallback. Read from the serving PROCESS environment (never the project env file),
    matching the observability/connections credential posture."""
    token = os.getenv("TYPEFLUX_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    return token.strip() if token and token.strip() else None


def _pull_request_ref(payload: object) -> PullRequestRef | None:
    """Normalize one GitHub pull-request JSON object into a :class:`PullRequestRef`.
    A merged PR (``merged_at`` present) is the audit case; an open PR that touched the
    commit is still linked (``merged_at`` null)."""
    if not isinstance(payload, dict):
        return None
    number = payload.get("number")
    url = payload.get("html_url")
    if not isinstance(number, int) or not isinstance(url, str):
        return None
    merged_at = payload.get("merged_at")
    return PullRequestRef(
        number=number,
        url=url,
        merged_at=merged_at if isinstance(merged_at, str) else None,
    )


def _select_pull_request(payloads: object) -> PullRequestRef | None:
    """Choose the approving PR from the ``/commits/{sha}/pulls`` list: prefer the merged
    one (the review record), else the first well-formed entry."""
    if not isinstance(payloads, list):
        return None
    refs = [ref for ref in (_pull_request_ref(item) for item in payloads) if ref is not None]
    if not refs:
        return None
    for ref in refs:
        if ref.merged_at is not None:
            return ref
    return refs[0]


class _GithubHttp:
    """A minimal authenticated GitHub REST reader (stdlib only — no new dependency).
    Stateless (only a token) and therefore safe to share across the plan-PR thread pool.
    Raises :class:`_RateLimited` on a rate-limit response (a 403 with the rate-limit signal,
    or any 429) so the seam can map it to the ``rate_limited`` partial; any other
    :class:`urllib.error.HTTPError` propagates for the caller to classify (a 404/422 is a
    routine per-resource miss, anything else a genuine transport failure)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def get(self, path: str) -> object:
        request = urllib.request.Request(
            f"{_GITHUB_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "typeflux-control-plane",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_GITHUB_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A 429 is always a (secondary) rate limit; a 403 is a rate limit only with the
            # primary/secondary signal (a bare 403 is a real auth/perm error → unreachable).
            if exc.code == 429 or (exc.code == 403 and _is_rate_limited(exc)):
                raise _RateLimited from exc
            raise


class _RateLimited(Exception):
    """GitHub answered with a rate-limit signal (a 403 primary/secondary limit, or a 429)."""


def _is_rate_limited(exc: urllib.error.HTTPError) -> bool:
    # Primary limit: `x-ratelimit-remaining: 0`. Secondary/abuse limit: a `retry-after`
    # header. Either is the rate-limited class (a 403 without them is a real auth/perm
    # error → unreachable, not rate_limited).
    headers = exc.headers
    if headers is None:
        return False
    if headers.get("retry-after") is not None:
        return True
    return headers.get("x-ratelimit-remaining") == "0"


def _is_absent(exc: BaseException) -> bool:
    """A 404/422 — a specific resource is absent or unprocessable (a commit with no PR, a
    ``compare`` with no common ancestor / a gone sha): routine drift, NOT a reachability
    outage, so it is isolated to the one read rather than degrading the whole response."""
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (404, 422)


def default_github_reader(
    *,
    repo: GithubRepo,
    branch: str,
    served_sha: str | None,
    plan_shas: Sequence[str],
) -> GithubReadResult:
    """The default GitHub transport seam (#727): a bounded, authenticated set of REST
    reads, degrading loudly and **per step**. No token → ``not_configured`` with NO network
    call (the Langfuse-seam posture). A rate-limit (403 signal / 429) → ``rate_limited``; a
    genuine transport failure → ``unreachable`` (never a silent empty ``ok``).

    Bounds: one HEAD lookup, at most one ``compare`` for the behind-count (only when the
    served sha differs from HEAD), and one PR lookup per plan sha (the caller has already
    capped the plan set at :data:`PLAN_PR_LOOKUP_CAP`), fanned out through a bounded thread
    pool (:data:`PLAN_PR_LOOKUP_CONCURRENCY`) rather than issued serially.

    Per-step isolation (P0-2): the HEAD read is the primary signal, so a failure THERE is
    the only path that yields no head at all. Once the HEAD is fetched it is always
    returned — a ``compare`` miss keeps the head with ``commits_behind=None`` (a 404/422 is
    a force-push / no-common-ancestor drift; a genuine transport failure additionally
    degrades ``partial`` to ``unreachable`` but still keeps the head), and a per-plan PR miss
    (404/422) leaves that plan's ``pr`` null without touching the others. Rate-limit
    anywhere wins the status (the actionable signal), transport failure otherwise."""
    token = _github_token()
    if token is None:
        return GithubReadResult("not_configured")
    http = _GithubHttp(token)  # stateless token holder — nothing to amortize across requests

    owner = quote(repo.owner, safe="")
    name = quote(repo.repo, safe="")

    # Step 1 — the HEAD lookup (the primary signal). A failure here yields no head at all.
    try:
        head = http.get(f"/repos/{owner}/{name}/commits/{quote(branch, safe='')}")
    except _RateLimited:
        return GithubReadResult("rate_limited")
    except Exception:  # noqa: BLE001 - degrade loudly, never 500 the surface.
        return GithubReadResult("unreachable")
    head_sha = head["sha"] if isinstance(head, dict) and isinstance(head.get("sha"), str) else None
    if head_sha is None:
        # Reached GitHub but the HEAD payload was malformed — the primary signal could not
        # be read, so this is NOT a healthy read: report `unreachable` rather than an `ok`
        # with no head (a client treating `ok` as healthy would get a false positive — Bugbot).
        return GithubReadResult("unreachable")

    # From here the HEAD is fetched and is always returned; later failures only mark
    # `partial` (rate_limited wins over unreachable — the more actionable signal).
    degraded: GithubStatus | None = None

    def _degrade(status: GithubStatus) -> None:
        nonlocal degraded
        if status == "rate_limited" or degraded is None:
            degraded = status

    # Step 2 — the behind-count, only when there is a divergence to measure (a call saved
    # when served == HEAD, the common healthy case).
    commits_behind: int | None = None
    if served_sha is not None and head_sha != served_sha:
        try:
            compare = http.get(
                f"/repos/{owner}/{name}/compare/"
                f"{quote(served_sha, safe='')}...{quote(head_sha, safe='')}"
            )
            if isinstance(compare, dict) and isinstance(compare.get("ahead_by"), int):
                # ahead_by = commits HEAD is ahead of the served base = how far served is
                # behind. Bound it so a long-diverged branch reports an honest ceiling.
                commits_behind = min(compare["ahead_by"], MAX_COMMITS_BEHIND)
        except _RateLimited:
            _degrade("rate_limited")
        except Exception as exc:  # noqa: BLE001
            # A 404/422 is a force-push / no-common-ancestor — exactly when drift matters:
            # keep the head, leave commits_behind null, and do NOT degrade the status.
            if not _is_absent(exc):
                _degrade("unreachable")

    # Step 3 — the plan-PR lookups, fanned out through a bounded pool; each future is
    # isolated (a 404/422 → that plan's pr null; a rate-limit/transport failure → degrade).
    plan_prs: dict[str, PullRequestRef] = {}
    unique_shas = list(dict.fromkeys(plan_shas))  # de-dupe, preserve order
    if unique_shas:
        with ThreadPoolExecutor(
            max_workers=min(PLAN_PR_LOOKUP_CONCURRENCY, len(unique_shas))
        ) as pool:
            for sha, result in zip(
                unique_shas,
                pool.map(lambda s: _lookup_plan_pr(http, owner, name, s), unique_shas),
            ):
                status, pr = result
                if status is not None:
                    _degrade(status)
                elif pr is not None:
                    plan_prs[sha] = pr

    return GithubReadResult(degraded or "ok", head_sha, commits_behind, plan_prs)


def _lookup_plan_pr(
    http: _GithubHttp, owner: str, name: str, sha: str
) -> tuple[GithubStatus | None, PullRequestRef | None]:
    """One plan-PR read, isolated for the fan-out: returns ``(None, pr)`` on success (``pr``
    may be ``None`` when the commit has no PR), or ``(status, None)`` when the read failed —
    ``rate_limited`` for a rate limit, ``unreachable`` for a genuine transport failure, and
    ``(None, None)`` for a routine 404/422 (a commit with no PR / an unknown sha) so it does
    NOT degrade the whole response."""
    try:
        pulls = http.get(f"/repos/{owner}/{name}/commits/{quote(sha, safe='')}/pulls")
    except _RateLimited:
        return "rate_limited", None
    except Exception as exc:  # noqa: BLE001
        return (None if _is_absent(exc) else "unreachable"), None
    return None, _select_pull_request(pulls)


__all__ = [
    "MAX_COMMITS_BEHIND",
    "PLAN_PR_LOOKUP_CAP",
    "PLAN_PR_LOOKUP_CONCURRENCY",
    "GithubPartial",
    "GithubProvenance",
    "GithubReadResult",
    "GithubRepo",
    "GithubStatus",
    "HeadProvenance",
    "PlanProvenance",
    "PlanRef",
    "PullRequestRef",
    "ServedProvenance",
    "build_github_provenance",
    "default_github_reader",
    "parse_github_repo",
    "plan_refs",
    "served_provenance",
]
