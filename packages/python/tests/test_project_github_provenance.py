"""Unit tests for the github-provenance surface helpers (#727 §1): the repo-url host
gate, served/plan extraction, the pure feed assembler, and the default GitHub transport
seam (with urllib stubbed — no network)."""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from typeflux.project.github_provenance import (
    MAX_COMMITS_BEHIND,
    GithubReadResult,
    GithubRepo,
    PlanRef,
    PullRequestRef,
    build_github_provenance,
    default_github_reader,
    parse_github_repo,
    plan_refs,
    served_provenance,
)

# ---------------------------------------------------------------------------
# parse_github_repo — the host gate (mirrors the console #718 gate).
# ---------------------------------------------------------------------------


def test_parse_github_repo_accepts_github_https_and_splits_owner_repo() -> None:
    assert parse_github_repo("https://github.com/acme/flows") == GithubRepo(
        owner="acme", repo="flows", url="https://github.com/acme/flows"
    )


def test_parse_github_repo_strips_trailing_git_and_extra_path() -> None:
    assert parse_github_repo("https://github.com/acme/flows.git") == GithubRepo(
        owner="acme", repo="flows", url="https://github.com/acme/flows.git"
    )
    # Extra path segments (deep links) still yield owner/repo from the first two.
    assert parse_github_repo("https://github.com/acme/flows/tree/main") == GithubRepo(
        owner="acme", repo="flows", url="https://github.com/acme/flows/tree/main"
    )


def test_parse_github_repo_allows_github_subdomain() -> None:
    got = parse_github_repo("https://www.github.com/acme/flows")
    assert got is not None and (got.owner, got.repo) == ("acme", "flows")


@pytest.mark.parametrize(
    "repo_url",
    [
        None,
        "",
        "git@github.com:acme/flows.git",  # ssh shorthand: no http(s) scheme
        "https://github.com.evil.com/acme/flows",  # prefix-bypass: registrable domain anchored
        "https://github.enterprise.example/acme/flows",  # self-hosted GHE: unverifiable
        "https://gitlab.com/acme/flows",  # non-github host
        "https://github.com/acme",  # missing repo segment
        "https://github.com/",  # no owner/repo
    ],
)
def test_parse_github_repo_rejects_non_github_or_incomplete(repo_url: str | None) -> None:
    assert parse_github_repo(repo_url) is None


# ---------------------------------------------------------------------------
# served_provenance / plan_refs — pure selectors.
# ---------------------------------------------------------------------------


def test_served_provenance_present_for_github_source() -> None:
    served = served_provenance(
        repo_url="https://github.com/acme/flows", repo_ref="main", repo_sha="abc123"
    )
    assert served is not None
    assert served.repo == GithubRepo("acme", "flows", "https://github.com/acme/flows")
    assert served.branch == "main"
    assert served.served_sha == "abc123"


def test_served_provenance_none_for_non_github_source() -> None:
    assert served_provenance(repo_url=None, repo_ref="main", repo_sha=None) is None
    assert (
        served_provenance(repo_url="https://gitlab.com/a/b", repo_ref="main", repo_sha=None) is None
    )


def test_plan_refs_caps_and_skips_shaless_plans() -> None:
    refs = [
        PlanRef("p0", "s0"),
        PlanRef("p1", None),  # no recorded provenance → skipped from PR lookup
        PlanRef("p2", "s2"),
        PlanRef("p3", "s3"),
    ]
    assert plan_refs(refs, cap=2) == [PlanRef("p0", "s0"), PlanRef("p2", "s2")]


# ---------------------------------------------------------------------------
# build_github_provenance — the pure assembler.
# ---------------------------------------------------------------------------

_SERVED = served_provenance(
    repo_url="https://github.com/acme/flows", repo_ref="main", repo_sha="served-sha"
)


def test_build_no_served_provenance_yields_null_head() -> None:
    result = build_github_provenance(
        served=None,
        plans=[PlanRef("p0", "s0")],
        read_result=GithubReadResult("not_configured"),
    )
    assert result.head is None
    assert result.partial.github == "not_configured"
    # Plans are still listed from local data; the PR is null (no network).
    assert result.plans == (type(result.plans[0])(plan_id="p0", sha="s0", pr=None),)


def test_build_head_behind_when_remote_differs() -> None:
    result = build_github_provenance(
        served=_SERVED,
        plans=[],
        read_result=GithubReadResult("ok", head_sha="remote-sha", commits_behind=3),
    )
    assert result.head is not None
    assert result.head.sha == "remote-sha"
    assert result.head.branch == "main"
    assert result.head.ahead_of_served is True
    assert result.head.commits_behind == 3


def test_build_head_in_sync_when_remote_equals_served() -> None:
    result = build_github_provenance(
        served=_SERVED,
        plans=[],
        read_result=GithubReadResult("ok", head_sha="served-sha", commits_behind=None),
    )
    assert result.head is not None
    assert result.head.ahead_of_served is False
    assert result.head.commits_behind is None


def test_build_head_ahead_unknown_when_served_sha_unresolved() -> None:
    # A not-yet-cloned source has no served sha: drift is UNKNOWN, never a false "in sync".
    served_no_sha = served_provenance(
        repo_url="https://github.com/acme/flows", repo_ref="main", repo_sha=None
    )
    result = build_github_provenance(
        served=served_no_sha,
        plans=[],
        read_result=GithubReadResult("ok", head_sha="remote-sha"),
    )
    assert result.head is not None
    assert result.head.ahead_of_served is None


def test_build_head_null_when_read_degraded_without_head() -> None:
    # A degradation that never fetched the HEAD (the head sha is absent) yields a null head.
    for status in ("unreachable", "rate_limited", "not_configured"):
        result = build_github_provenance(
            served=_SERVED,
            plans=[],
            read_result=GithubReadResult(status),  # type: ignore[arg-type]
        )
        assert result.head is None, status
        assert result.partial.github == status


def test_build_head_survives_degradation_once_fetched() -> None:
    # HONEST SHAPE (#727 P0-2c): the HEAD was read before a later transport failure — keep it
    # alongside the degraded marker rather than discarding the endpoint's primary signal.
    result = build_github_provenance(
        served=_SERVED,
        plans=[],
        read_result=GithubReadResult("unreachable", head_sha="remote-sha"),
    )
    assert result.head is not None
    assert result.head.sha == "remote-sha"
    assert result.partial.github == "unreachable"


def test_build_plan_pr_linked_when_resolved_else_null() -> None:
    pr = PullRequestRef(
        number=7, url="https://github.com/acme/flows/pull/7", merged_at="2026-07-01T00:00:00Z"
    )
    result = build_github_provenance(
        served=_SERVED,
        plans=[PlanRef("p-has-pr", "s1"), PlanRef("p-no-pr", "s2"), PlanRef("p-no-sha", None)],
        read_result=GithubReadResult("ok", head_sha="served-sha", plan_prs={"s1": pr}),
    )
    by_id = {plan.plan_id: plan for plan in result.plans}
    assert by_id["p-has-pr"].pr == pr
    assert by_id["p-no-pr"].pr is None and by_id["p-no-pr"].sha == "s2"
    assert by_id["p-no-sha"].pr is None and by_id["p-no-sha"].sha is None


# ---------------------------------------------------------------------------
# default_github_reader — the transport seam, urllib stubbed.
# ---------------------------------------------------------------------------

_REPO = GithubRepo("acme", "flows", "https://github.com/acme/flows")


def _clear_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPEFLUX_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_reader_no_token_is_not_configured_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_tokens(monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> Any:  # any network attempt is a failure
        raise AssertionError("no network call may be made when unconfigured")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    result = default_github_reader(
        repo=_REPO, branch="main", served_sha="served", plan_shas=("s1",)
    )
    assert result == GithubReadResult("not_configured")


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()


def _routed_urlopen(routes: dict[str, Any]):  # noqa: ANN202 - test helper
    def _urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:  # noqa: ARG001
        url = request.full_url
        for fragment, payload in routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(json.dumps(payload).encode("utf-8"))
        raise AssertionError(f"unexpected GitHub call: {url}")

    return _urlopen


def test_reader_ok_reads_head_compare_and_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPEFLUX_GITHUB_TOKEN", "tok")
    routes = {
        "/commits/main": {"sha": "remote-sha"},
        "/compare/served...remote-sha": {"ahead_by": 4},
        "/commits/s1/pulls": [
            {"number": 3, "html_url": "https://github.com/acme/flows/pull/3", "merged_at": None},
            {
                "number": 4,
                "html_url": "https://github.com/acme/flows/pull/4",
                "merged_at": "2026-07-02T00:00:00Z",
            },
        ],
    }
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(
        repo=_REPO, branch="main", served_sha="served", plan_shas=("s1", "s1")
    )
    assert result.status == "ok"
    assert result.head_sha == "remote-sha"
    assert result.commits_behind == 4
    # The merged PR wins over the open one.
    assert result.plan_prs["s1"].number == 4
    assert result.plan_prs["s1"].merged_at == "2026-07-02T00:00:00Z"


def test_reader_skips_compare_when_head_equals_served(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    # No /compare route registered: if the reader called it, the helper would raise.
    routes = {"/commits/main": {"sha": "served"}, "/commits/s1/pulls": []}
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(
        repo=_REPO, branch="main", served_sha="served", plan_shas=("s1",)
    )
    assert result.status == "ok"
    assert result.head_sha == "served"
    assert result.commits_behind is None
    assert result.plan_prs == {}


def test_reader_bounds_commits_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {
        "/commits/main": {"sha": "remote-sha"},
        "/compare/served...remote-sha": {"ahead_by": 10_000},
    }
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result.commits_behind == MAX_COMMITS_BEHIND


def _http_error(code: int, headers: dict[str, str]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.github.com/x",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(b""),  # type: ignore[arg-type]
    )


def test_reader_rate_limited_on_403_with_ratelimit_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {"/commits/main": _http_error(403, {"x-ratelimit-remaining": "0"})}
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result == GithubReadResult("rate_limited")


def test_reader_rate_limited_on_secondary_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {"/commits/main": _http_error(403, {"retry-after": "60"})}
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result == GithubReadResult("rate_limited")


def test_reader_unreachable_on_other_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    # 404 (repo/branch gone) or a 403 that is NOT rate-limit → unreachable, never a silent ok.
    routes = {"/commits/main": _http_error(404, {})}
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result == GithubReadResult("unreachable")


def test_reader_unreachable_on_malformed_head_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 200 whose body lacks a usable `sha` cannot yield the primary signal — an `ok` with
    # no head would be a false-healthy read (Bugbot); it must degrade to `unreachable`.
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {"/commits/main": {"not_sha": True}}
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result == GithubReadResult("unreachable")


def test_reader_unreachable_on_403_without_ratelimit_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {"/commits/main": _http_error(403, {})}  # a permission 403, not a rate limit
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result == GithubReadResult("unreachable")


def test_reader_rate_limited_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 429 is always a (secondary) rate limit, even without an explicit ratelimit header.
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {"/commits/main": _http_error(429, {})}
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result == GithubReadResult("rate_limited")


def test_reader_one_bad_plan_sha_keeps_head_and_other_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    # PER-PLAN ISOLATION (#727 P0-2a): a routine 404 on ONE plan's /pulls leaves that plan's
    # pr null and keeps the fetched HEAD + the other plans' PRs — never nukes the response.
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {
        "/commits/main": {"sha": "served"},  # HEAD == served → no compare call
        "/commits/good/pulls": [
            {"number": 5, "html_url": "https://github.com/acme/flows/pull/5", "merged_at": None}
        ],
        "/commits/bad/pulls": _http_error(404, {}),  # commit not found → isolated, pr null
    }
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(
        repo=_REPO, branch="main", served_sha="served", plan_shas=("good", "bad")
    )
    assert result.status == "ok"  # a 404 on one plan is routine, not a degradation
    assert result.head_sha == "served"
    assert result.plan_prs["good"].number == 5
    assert "bad" not in result.plan_prs


def test_reader_compare_failure_keeps_head_with_null_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # COMPARE ISOLATION (#727 P0-2b): a 404 compare (force-push / no common ancestor — exactly
    # when drift matters) keeps the fetched HEAD with commits_behind null, status still ok.
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {
        "/commits/main": {"sha": "remote-sha"},
        "/compare/served...remote-sha": _http_error(404, {}),
    }
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(repo=_REPO, branch="main", served_sha="served", plan_shas=())
    assert result.status == "ok"
    assert result.head_sha == "remote-sha"
    assert result.commits_behind is None


def test_reader_transport_failure_after_head_keeps_head_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HONEST SHAPE (#727 P0-2c): a genuine transport failure on a plan-PR read AFTER HEAD
    # succeeded keeps the fetched HEAD and degrades partial to unreachable (not discarded).
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    routes = {
        "/commits/main": {"sha": "served"},  # HEAD == served → no compare
        "/commits/s1/pulls": _http_error(500, {}),  # a real transport failure, not a 404/422
    }
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen(routes))
    result = default_github_reader(
        repo=_REPO, branch="main", served_sha="served", plan_shas=("s1",)
    )
    assert result.status == "unreachable"
    assert result.head_sha == "served"  # the primary signal survives the later failure
    assert result.plan_prs == {}
