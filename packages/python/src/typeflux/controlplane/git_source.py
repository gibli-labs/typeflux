"""GitHub-sourced project clones for the registry (#256, phase 2).

A registry entry may point at a Git repository instead of a local checkout.
The server clones it into a managed cache on first use and refreshes it on an
operator-triggered ``POST /projects/{id}/refresh``. The registry file is
operator-authored (trusted config), so cloning its URLs is in scope — there is
no user-supplied URL path here.

Subprocess git only; no network library. Failures degrade with a clear message.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict

from typeflux.core.errors import TypefluxError

# A registry remote may be a credential-bearing HTTPS URL
# (https://user:token@host/repo). The clone uses it as-is, but it must never
# reach a control-plane response, refresh detail, console tooltip, or error
# string. These scrub userinfo at the output boundary. Case-insensitive scheme
# so an uppercase HTTPS:// can't slip a token through.
_USERINFO_IN_URL = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")


def scrub_git_url(url: str | None) -> str | None:
    """Strip ``user:token@`` userinfo from a single URL, preserving the rest."""
    if not url:
        return url
    parsed = urlparse(url)
    # Strip userinfo from any scheme that has it (not a whitelist — git+https,
    # file, or an unexpected scheme must not leak a token). The ssh shorthand
    # ``git@host:path`` has no scheme and carries no token, so it is left as-is.
    if parsed.scheme and "@" in parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=host))
    return url


def scrub_git_text(text: str) -> str:
    """Strip userinfo from any ``scheme://user:token@host`` in free text/errors."""
    return _USERINFO_IN_URL.sub(r"\1", text)


class ProjectSourceError(TypefluxError, ValueError):
    """A Git-sourced project could not be cloned, refreshed, or located."""


class ProjectRepoSource(BaseModel):
    """A Git repository a project is sourced from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    ref: str = "main"
    #: Path to the project manifest *within* the repository.
    manifest: str = "typeflux.project.yaml"


class ProjectRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source: Literal["local", "git"]
    refreshed: bool
    ref: str | None = None
    sha: str | None = None
    #: ISO-8601 time the refresh was attempted (set by the registry layer).
    refreshed_at: str | None = None
    detail: str | None = None


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git missing is environmental.
        raise ProjectSourceError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        # Both the args (a clone/fetch URL) and stderr can echo embedded creds.
        message = scrub_git_text(f"git {' '.join(args)} failed: {detail}")
        raise ProjectSourceError(message) from exc
    return result.stdout.strip()


def _clone_dir(cache_dir: Path, project_id: str) -> Path:
    return cache_dir / project_id


def current_sha(cache_dir: Path, project_id: str) -> str | None:
    """The clone's current HEAD sha, or None if not cloned / git unavailable.

    Read-only and degrades safely — provenance must never break a read.
    """
    clone_dir = _clone_dir(cache_dir, project_id)
    if not (clone_dir / ".git").exists():
        return None
    try:
        return _git(["rev-parse", "HEAD"], cwd=clone_dir)
    except ProjectSourceError:
        return None


def file_commit_sha(cache_dir: Path, project_id: str, relative_path: str) -> str | None:
    """The last commit that touched ``relative_path`` (repo-root-relative) in the
    project's clone, or ``None`` when the clone is absent, git is unavailable, or the
    path is untracked/uncommitted (``git log`` prints nothing and exits 0).

    This is the commit the github-provenance surface (#727) attributes a plan's
    approving PR to: a deployment plan FILE is authored, committed, and merged via a
    review PR *after* the code checkout it pins, so the plan's provenance is its own
    file's last commit — not the checkout HEAD the plan was resolved from. Read-only and
    degrades safely — provenance must never break a read.

    Note the managed clone is shallow (``--depth 1``): only the fetched tip's history is
    present, so a plan file last touched before the shallow boundary resolves to ``None``
    (an honest null, never a fabricated attribution)."""
    clone_dir = _clone_dir(cache_dir, project_id)
    if not (clone_dir / ".git").exists():
        return None
    try:
        sha = _git(["log", "-1", "--format=%H", "--", relative_path], cwd=clone_dir)
    except ProjectSourceError:
        return None
    return sha or None


def _manifest_within(clone_dir: Path, manifest: str) -> Path:
    """Resolve the manifest inside the clone, rejecting escapes (``..``)."""
    resolved = (clone_dir / manifest).resolve()
    clone_root = clone_dir.resolve()
    if not resolved.is_relative_to(clone_root):
        raise ProjectSourceError(f"manifest path {manifest!r} escapes the project clone directory")
    return resolved


def ensure_repo_manifest(
    repo: ProjectRepoSource,
    *,
    cache_dir: Path,
    project_id: str,
) -> Path:
    """Clone the repo into the cache on first use; return the manifest path."""
    clone_dir = _clone_dir(cache_dir, project_id)
    if not (clone_dir / ".git").exists():
        # A leftover non-empty directory without a .git (an interrupted clone)
        # would make `git clone` fail "destination already exists"; clear it so
        # the clone can self-heal.
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        _git(
            [
                "clone",
                "--depth",
                "1",
                "--branch",
                repo.ref,
                repo.url,
                str(clone_dir),
            ]
        )
    return _manifest_within(clone_dir, repo.manifest)


def refresh_repo(
    repo: ProjectRepoSource,
    *,
    cache_dir: Path,
    project_id: str,
) -> ProjectRefreshResult:
    """Fetch and hard-reset the clone to the tip of its ref."""
    clone_dir = _clone_dir(cache_dir, project_id)
    if not (clone_dir / ".git").exists():
        # Not cloned yet — first ensure pulls it; treat as a fresh clone.
        ensure_repo_manifest(repo, cache_dir=cache_dir, project_id=project_id)
    else:
        # Reset to FETCH_HEAD (what we just fetched), not origin/<ref>: a shallow
        # `fetch origin <ref>` reliably updates FETCH_HEAD but not the
        # remote-tracking ref, and tag refs have no `origin/<tag>` at all — so
        # `reset --hard origin/<ref>` is fragile for tags and force-pushes. This
        # leaves HEAD detached at the fetched tip, which is fine for read-only
        # resolution.
        _git(["fetch", "--depth", "1", "origin", repo.ref], cwd=clone_dir)
        _git(["reset", "--hard", "FETCH_HEAD"], cwd=clone_dir)
    sha = _git(["rev-parse", "HEAD"], cwd=clone_dir)
    return ProjectRefreshResult(
        id=project_id,
        source="git",
        refreshed=True,
        ref=repo.ref,
        sha=sha,
        detail=f"refreshed {scrub_git_url(repo.url)} @ {repo.ref}",
    )


__all__ = [
    "ProjectRefreshResult",
    "ProjectRepoSource",
    "ProjectSourceError",
    "current_sha",
    "ensure_repo_manifest",
    "file_commit_sha",
    "refresh_repo",
]
