from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from typeflux.manifests._common import drop_none


@dataclass(frozen=True)
class CodeProvenance:
    available: bool
    source: str
    repo_url: str | None = None
    git_ref: str | None = None
    git_sha: str | None = None
    dirty: bool | None = None
    dirty_hash: str | None = None
    deployment_id: str | None = None
    environment: str | None = None
    package_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return drop_none(asdict(self))


_CODE_PROVENANCE_CACHE: dict[str, CodeProvenance] = {}
_SCP_LIKE_REMOTE_RE = re.compile(
    r"^(?P<userinfo>[^/@\s]+)@(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<path>.+)$"
)


def collect_code_provenance(cwd: str | Path | None = None) -> CodeProvenance:
    package_version = package_version_or_none()
    env_provenance = _env_code_provenance(package_version)
    if env_provenance is not None:
        return env_provenance

    # The cache holds CODE identity only; deployment identity (environment /
    # deployment id) is overlaid fresh on every return so a cached entry never
    # serves stale deployment vars (#829).
    working_dir = Path(cwd or os.getcwd())
    cache_key = str(working_dir.resolve())
    cached = _CODE_PROVENANCE_CACHE.get(cache_key)
    if cached is not None:
        return _with_deployment_identity(cached)
    top_level = _git(["rev-parse", "--show-toplevel"], working_dir)
    if top_level is None:
        provenance = CodeProvenance(
            available=False,
            source="unavailable",
            package_version=package_version,
        )
        _CODE_PROVENANCE_CACHE[cache_key] = provenance
        return _with_deployment_identity(provenance)

    repo = Path(top_level)
    status = _git(["status", "--porcelain"], repo) or ""
    provenance = CodeProvenance(
        available=True,
        source="git",
        repo_url=_sanitize_repo_url(_git(["remote", "get-url", "origin"], repo)),
        git_ref=_git(["rev-parse", "--abbrev-ref", "HEAD"], repo),
        git_sha=_git(["rev-parse", "HEAD"], repo),
        dirty=bool(status),
        dirty_hash=sha256(status.encode("utf-8")).hexdigest() if status else None,
        package_version=package_version,
    )
    _CODE_PROVENANCE_CACHE[cache_key] = provenance
    return _with_deployment_identity(provenance)


def _with_deployment_identity(provenance: CodeProvenance) -> CodeProvenance:
    return replace(
        provenance,
        deployment_id=os.getenv("TYPEFLUX_DEPLOYMENT_ID"),
        environment=os.getenv("TYPEFLUX_ENVIRONMENT"),
    )


def _clear_code_provenance_cache() -> None:
    _CODE_PROVENANCE_CACHE.clear()


def package_version_or_none() -> str | None:
    try:
        return version("typeflux")
    except PackageNotFoundError:
        return None


def _env_code_provenance(package_version: str | None) -> CodeProvenance | None:
    # Env-sourced CODE identity is gated on the TYPEFLUX_GIT_* triplet alone (#829).
    # TYPEFLUX_ENVIRONMENT / TYPEFLUX_DEPLOYMENT_ID are deployment identity — the
    # documented default .env sets them, and they must not suppress git detection;
    # they merge onto whichever code-identity source wins (env, git, or unavailable).
    # Whitespace-only values must not pass the gate (repo_url gets the same
    # treatment inside _sanitize_repo_url).
    git_sha = (os.getenv("TYPEFLUX_GIT_SHA") or "").strip() or None
    git_ref = (os.getenv("TYPEFLUX_GIT_REF") or "").strip() or None
    repo_url = _sanitize_repo_url(os.getenv("TYPEFLUX_REPO_URL"))
    if not any((git_sha, git_ref, repo_url)):
        return None
    return _with_deployment_identity(
        CodeProvenance(
            available=True,
            source="env",
            repo_url=repo_url,
            git_ref=git_ref,
            git_sha=git_sha,
            package_version=package_version,
        )
    )


def _sanitize_repo_url(repo_url: str | None) -> str | None:
    if repo_url is None:
        return None

    value = repo_url.strip()
    if not value:
        return None

    parsed = urlsplit(value)
    if parsed.netloc:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    scp_like = _SCP_LIKE_REMOTE_RE.match(value)
    if scp_like is not None:
        return f"{scp_like.group('host')}:{scp_like.group('path')}"

    return value


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


__all__ = ["CodeProvenance", "collect_code_provenance"]
