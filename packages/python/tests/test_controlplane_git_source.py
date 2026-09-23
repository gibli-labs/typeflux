"""Direct unit tests for typeflux.controlplane.git_source.

Registry-level clone/refresh flows live in tests/test_controlplane_registry.py;
this drives the module surface itself: source models, HEAD provenance,
the refresh-before-clone path, and credential scrubbing on git failures.
All git operations run against local repositories only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from typeflux.controlplane.git_source import (
    ProjectRefreshResult,
    ProjectRepoSource,
    ProjectSourceError,
    _git,
    current_sha,
    ensure_repo_manifest,
    file_commit_sha,
    refresh_repo,
)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _make_remote(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _run_git(["init", "-b", "main"], work)
    (work / "typeflux.project.yaml").write_text(
        'version: "1"\nname: git-unit-demo\nworkflows: []\nenvironments: {}\n',
        encoding="utf-8",
    )
    _run_git(["add", "typeflux.project.yaml"], work)
    _run_git(["commit", "-m", "init"], work)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True
    )
    return bare


def test_repo_source_defaults_and_strictness() -> None:
    source = ProjectRepoSource(url="https://example.com/acme/demo.git")
    assert source.ref == "main"
    assert source.manifest == "typeflux.project.yaml"

    with pytest.raises(ValidationError):
        ProjectRepoSource.model_validate({"url": "x", "branch": "main"})
    with pytest.raises(ValidationError):
        source.ref = "other"


def test_refresh_result_model_round_trips() -> None:
    result = ProjectRefreshResult(id="p", source="git", refreshed=True, ref="main", sha="a" * 40)
    payload = result.model_dump(mode="json")
    assert ProjectRefreshResult.model_validate(payload) == result

    with pytest.raises(ValidationError):
        ProjectRefreshResult.model_validate({**payload, "source": "svn"})


def test_current_sha_degrades_to_none_without_a_usable_clone(tmp_path: Path) -> None:
    cache = tmp_path / "clones"

    # Never cloned: nothing to report.
    assert current_sha(cache, "missing") is None

    # A .git marker that is not a real repository degrades instead of raising.
    broken = cache / "broken" / ".git"
    broken.mkdir(parents=True)
    assert current_sha(cache, "broken") is None


def test_file_commit_sha_returns_plan_file_last_commit(tmp_path: Path) -> None:
    # #727 P0-1: a plan file's provenance is its OWN last commit in the clone, resolved via
    # `git log -1 -- <path>` — not the checkout HEAD the plan was resolved from.
    cache = tmp_path / "clones"
    clone = cache / "demo"
    clone.mkdir(parents=True)
    _run_git(["init", "-b", "main"], clone)
    (clone / "deployments").mkdir()
    (clone / "deployments" / "plan.yaml").write_text("plan: 1\n", encoding="utf-8")
    _run_git(["add", "deployments/plan.yaml"], clone)
    _run_git(["commit", "-m", "add plan"], clone)
    plan_commit = _git(["rev-parse", "HEAD"], cwd=clone)
    # A later, unrelated commit advances HEAD — the plan file's sha must stay the plan commit.
    (clone / "other.txt").write_text("x\n", encoding="utf-8")
    _run_git(["add", "other.txt"], clone)
    _run_git(["commit", "-m", "unrelated"], clone)

    assert file_commit_sha(cache, "demo", "deployments/plan.yaml") == plan_commit
    assert _git(["rev-parse", "HEAD"], cwd=clone) != plan_commit  # HEAD has moved past it


def test_file_commit_sha_none_for_untracked_or_missing_clone(tmp_path: Path) -> None:
    cache = tmp_path / "clones"
    # No clone at all → None (degrades safely, never raises).
    assert file_commit_sha(cache, "missing", "deployments/plan.yaml") is None
    # A real clone, but the plan file is untracked/uncommitted → None (never a fabricated sha).
    clone = cache / "demo"
    clone.mkdir(parents=True)
    _run_git(["init", "-b", "main"], clone)
    (clone / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "seed.txt"], clone)
    _run_git(["commit", "-m", "seed"], clone)
    (clone / "deployments").mkdir()
    (clone / "deployments" / "plan.yaml").write_text("plan: 1\n", encoding="utf-8")  # not added
    assert file_commit_sha(cache, "demo", "deployments/plan.yaml") is None


def test_refresh_repo_clones_fresh_and_reports_provenance(tmp_path: Path) -> None:
    bare = _make_remote(tmp_path)
    cache = tmp_path / "clones"
    repo = ProjectRepoSource(url=str(bare))

    # Refresh before any clone exists takes the fresh-clone path.
    result = refresh_repo(repo, cache_dir=cache, project_id="demo")

    assert result.source == "git"
    assert result.refreshed is True
    assert result.ref == "main"
    assert result.sha is not None
    assert len(result.sha) == 40
    assert str(bare) in (result.detail or "")
    # The clone's HEAD provenance matches what the refresh reported.
    assert current_sha(cache, "demo") == result.sha
    # And the manifest resolves inside the managed clone.
    manifest = ensure_repo_manifest(repo, cache_dir=cache, project_id="demo")
    assert manifest == (cache / "demo" / "typeflux.project.yaml").resolve()
    assert manifest.is_file()


def test_git_failures_raise_scrubbed_source_errors(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run_git(["init", "-b", "main"], repo_dir)

    # git echoes its arguments into stderr; a credential-bearing URL in either
    # place must never survive into the raised error.
    with pytest.raises(ProjectSourceError) as excinfo:
        _git(["checkout", "https://user:s3cr3t-token@example.com/acme/demo"], cwd=repo_dir)

    message = str(excinfo.value)
    assert "s3cr3t-token" not in message
    assert "https://example.com/acme/demo" in message
    assert message.startswith("git checkout")
    assert "failed" in message
