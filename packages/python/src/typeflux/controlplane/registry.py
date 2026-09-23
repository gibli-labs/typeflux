"""Project registry: serve one or several Typeflux projects (#256).

The single-manifest server is the degenerate case — a registry of one whose
sole entry is the default alias. Multi-project serving lists several projects
in a ``typeflux.projects.yaml`` file; routes gain a ``/projects/{project}``
dimension that delegates to the same handlers (see ``api.py``).

Phase 1 (this module) is local checkout paths only: each entry points at an
already-cloned ``typeflux.project.yaml``. Phase 2 (issue #256 PR 3) adds
``repo: {url, ref}`` entries the server clones into a managed cache with an
operator-triggered refresh.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict
from yaml import YAMLError

from typeflux.controlplane.git_source import (
    ProjectRefreshResult,
    ProjectRepoSource,
    ProjectSourceError,
    current_sha,
    ensure_repo_manifest,
    file_commit_sha,
    refresh_repo,
    scrub_git_text,
    scrub_git_url,
)
from typeflux.core.errors import TypefluxError
from typeflux.project import TypefluxProjectSpec, load_project_spec
from typeflux.yaml.loader import strict_safe_load

REGISTRY_VERSION: Literal["1"] = "1"
DEFAULT_CLONE_DIR_NAME = ".typeflux-clones"


def _ensure_on_sys_path(directory: Path) -> None:
    """Put a project root on sys.path (idempotent) so its package imports."""
    resolved = str(directory.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


class ProjectRegistryError(TypefluxError, ValueError):
    """A project registry is malformed or references an unknown project."""


#: Runtimes this control plane can resolve in-process (#619). Resolution —
#: importing a project's schema/activity modules — is language-bound; a
#: project declaring any other runtime stays inspectable (pure-YAML reads)
#: but resolution-dependent operations fail closed with UnsupportedRuntime.
SUPPORTED_RESOLVER_RUNTIMES: frozenset[str] = frozenset({"python"})


class UnsupportedProjectRuntimeError(TypefluxError):
    """A resolution operation was addressed at a project runtime this server
    has no resolver for (#619). Deliberately not a ValueError: this is not a
    config mistake but a served-by-construction limitation — the API maps it
    to 501 with the ``UnsupportedRuntime`` discriminant."""


class ProjectRegistryEntry(BaseModel):
    """One registered project: a stable id and exactly one source.

    A ``manifest_path`` is a local checkout; a ``repo`` is Git-sourced and the
    server clones it into the registry's cache on first use. ``runtime``
    declares the language runtime that can resolve the project's modules
    (#619); every pre-existing project is Python, hence the default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    manifest_path: Path | None = None
    repo: ProjectRepoSource | None = None
    runtime: Literal["python", "typescript"] = "python"


class ProjectSummary(BaseModel):
    """Read projection of a registered project for the console switcher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    manifest_path: str
    default: bool
    #: The project's declared language runtime (#619).
    runtime: Literal["python", "typescript"]
    #: Whether THIS server can resolve the project (its runtime is among the
    #: server's supported resolver runtimes). An unresolvable project stays
    #: inspectable; resolution-dependent operations answer 501.
    resolvable: bool
    #: Where the project is sourced from, for the console's per-project card.
    source: Literal["local", "git"]
    repo_url: str | None = None
    repo_ref: str | None = None
    #: Current clone HEAD sha (Git-sourced only; None when not yet cloned).
    repo_sha: str | None = None
    #: The manifest's path WITHIN the repository (Git-sourced only) — with
    #: repo_url + repo_sha this lets the console build source links for any
    #: manifest-relative definition path (#606).
    manifest_repo_path: str | None = None
    #: The latest refresh attempt for this project, if any (this process).
    last_refresh: ProjectRefreshResult | None = None
    #: False when the project could not be cloned/loaded (e.g. an unreachable
    #: Git source). The listing degrades per-project so one bad entry never
    #: fails the whole switcher; `detail` carries the error.
    available: bool = True
    detail: str | None = None


class ProjectRegistry:
    """An ordered set of registered projects with a default alias.

    Manifests are re-read on every ``resolve`` so the console reflects the
    YAML on disk, matching the single-project read tier's freshness contract.
    """

    def __init__(
        self,
        entries: tuple[ProjectRegistryEntry, ...],
        *,
        default_id: str,
        cache_dir: Path,
    ) -> None:
        if not entries:
            raise ProjectRegistryError("project registry must list at least one project")
        ids = [entry.id for entry in entries]
        duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
        if duplicates:
            raise ProjectRegistryError(
                f"project registry has duplicate ids: {', '.join(duplicates)}"
            )
        if default_id not in ids:
            raise ProjectRegistryError(f"default project {default_id!r} is not a registered id")
        for entry in entries:
            if (entry.manifest_path is None) == (entry.repo is None):
                raise ProjectRegistryError(
                    f"project {entry.id!r} must set exactly one of 'manifest' or 'repo'"
                )
        self._entries = entries
        self._by_id = {entry.id: entry for entry in entries}
        self._default_id = default_id
        self._cache_dir = cache_dir
        # In-memory latest-refresh record per project (this process only).
        self._last_refresh: dict[str, ProjectRefreshResult] = {}

    @property
    def default_id(self) -> str:
        return self._default_id

    @property
    def entries(self) -> tuple[ProjectRegistryEntry, ...]:
        return self._entries

    def entry(self, project_id: str | None) -> ProjectRegistryEntry:
        """The entry for ``project_id``; the default when ``project_id`` is None."""
        target = project_id if project_id is not None else self._default_id
        entry = self._by_id.get(target)
        if entry is None:
            raise KeyError(target)
        return entry

    def entry_runtime_resolvable(
        self,
        project_id: str | None,
        *,
        supported: frozenset[str] = SUPPORTED_RESOLVER_RUNTIMES,
    ) -> bool:
        """Whether the serving resolver covers the project's declared runtime."""
        return self.entry(project_id).runtime in supported

    def require_resolvable(
        self,
        project_id: str | None,
        *,
        supported: frozenset[str] = SUPPORTED_RESOLVER_RUNTIMES,
    ) -> None:
        """Fail closed before a resolution-dependent operation (#619)."""
        entry = self.entry(project_id)
        if entry.runtime not in supported:
            raise UnsupportedProjectRuntimeError(
                f"project {entry.id!r} declares runtime {entry.runtime!r}, which "
                f"this server cannot resolve (supported: "
                f"{', '.join(sorted(supported))}); "
                "resolution-dependent operations are unavailable for it"
            )

    def _manifest_path(self, entry: ProjectRegistryEntry) -> Path:
        """Resolve the manifest, cloning a Git-sourced project on first use."""
        if entry.repo is not None:
            manifest_path = ensure_repo_manifest(
                entry.repo, cache_dir=self._cache_dir, project_id=entry.id
            )
        else:
            assert entry.manifest_path is not None  # guaranteed by the xor check
            manifest_path = entry.manifest_path
        # Make the project's Python package importable. A served project's root
        # is the manifest directory; for a Git-sourced clone (and any project
        # served from a different cwd) that directory is not otherwise on the
        # import path, so `import_module(project.<...>)` would fail. Flat layout
        # only — the project's top-level package must sit at the manifest dir.
        # Caveat: imports are process-global, so two served projects must not
        # expose the same top-level package name (use a unique/namespaced one).
        # Only Python-runtime projects have an importable package here.
        if entry.runtime == "python":
            _ensure_on_sys_path(manifest_path.parent)
        return manifest_path

    def repo_source(self, project_id: str | None) -> ProjectRepoSource | None:
        """The declared Git source for the project (``None`` for a local checkout) —
        the recorded ``repo_url``/``ref`` provenance the github-provenance surface reads
        (#727), never a resolved value."""
        return self.entry(project_id).repo

    def repo_head_sha(self, project_id: str | None) -> str | None:
        """The current clone HEAD sha for a Git-sourced project (``None`` for a local
        checkout or a not-yet-cloned source) — the served checkout sha the
        github-provenance surface compares against the remote branch HEAD (#727), same
        source as ``ProjectSummary.repo_sha``."""
        entry = self.entry(project_id)
        if entry.repo is None:
            return None
        return current_sha(self._cache_dir, entry.id)

    def plan_file_sha(self, project_id: str | None, manifest_relative_path: str) -> str | None:
        """The last commit that touched a deployment plan FILE in the project's clone —
        the commit the github-provenance surface (#727) attributes the plan's approving
        PR to. ``manifest_relative_path`` is the plan's path relative to the project
        manifest (e.g. ``deployments/<plan_id>.yaml``); it is rebased onto the manifest's
        location WITHIN the repo (``ProjectRepoSource.manifest``) to a repo-root-relative
        path for ``git log``. ``None`` for a local checkout, a not-yet-cloned source, or an
        untracked/uncommitted plan file (never a fabricated sha)."""
        entry = self.entry(project_id)
        if entry.repo is None:
            return None
        repo_relative = str(PurePosixPath(entry.repo.manifest).parent / manifest_relative_path)
        return file_commit_sha(self._cache_dir, entry.id, repo_relative)

    def resolve(self, project_id: str | None) -> TypefluxProjectSpec:
        return load_project_spec(self._manifest_path(self.entry(project_id)))

    def refresh(
        self, project_id: str | None, *, now: datetime | None = None
    ) -> ProjectRefreshResult:
        """Refresh a Git-sourced project's clone; a no-op for local checkouts.

        Never raises on a Git failure: returns a `refreshed=False` result with
        the error detail and records it. A failed fetch/reset leaves the
        previous good clone intact, so reads are not poisoned.
        """
        entry = self.entry(project_id)
        stamp = (now or datetime.now(tz=UTC)).isoformat()
        if entry.repo is None:
            result = ProjectRefreshResult(
                id=entry.id,
                source="local",
                refreshed=False,
                refreshed_at=stamp,
                detail="local checkout — nothing to refresh",
            )
        else:
            try:
                result = refresh_repo(
                    entry.repo, cache_dir=self._cache_dir, project_id=entry.id
                ).model_copy(update={"refreshed_at": stamp})
            except ProjectSourceError as exc:
                result = ProjectRefreshResult(
                    id=entry.id,
                    source="git",
                    refreshed=False,
                    ref=entry.repo.ref,
                    refreshed_at=stamp,
                    detail=scrub_git_text(str(exc)),
                )
        self._last_refresh[entry.id] = result
        return result

    def summaries(
        self,
        *,
        supported: frozenset[str] = SUPPORTED_RESOLVER_RUNTIMES,
    ) -> tuple[ProjectSummary, ...]:
        out: list[ProjectSummary] = []
        for entry in self._entries:
            is_git = entry.repo is not None
            # Scrub userinfo before the URL leaves the process (summaries,
            # manifest_path fallback, console). The clone still uses the real URL.
            repo_url = scrub_git_url(entry.repo.url) if entry.repo is not None else None
            repo_ref = entry.repo.ref if entry.repo is not None else None
            try:
                manifest_path = self._manifest_path(entry)
                spec = load_project_spec(manifest_path)
            except (ProjectSourceError, ValueError) as exc:
                # One unreachable Git source (or an unloadable manifest) must not
                # fail the whole listing — degrade just this entry so the
                # switcher keeps working for the healthy projects.
                out.append(
                    ProjectSummary(
                        id=entry.id,
                        name=entry.id,
                        manifest_path=repo_url or "",
                        default=entry.id == self._default_id,
                        runtime=entry.runtime,
                        resolvable=entry.runtime in supported,
                        source="git" if is_git else "local",
                        repo_url=repo_url,
                        repo_ref=repo_ref,
                        repo_sha=current_sha(self._cache_dir, entry.id) if is_git else None,
                        manifest_repo_path=entry.repo.manifest if entry.repo is not None else None,
                        last_refresh=self._last_refresh.get(entry.id),
                        available=False,
                        detail=scrub_git_text(str(exc)),
                    )
                )
                continue
            out.append(
                ProjectSummary(
                    id=entry.id,
                    name=spec.name,
                    manifest_path=str(manifest_path),
                    default=entry.id == self._default_id,
                    runtime=entry.runtime,
                    resolvable=entry.runtime in supported,
                    source="git" if is_git else "local",
                    repo_url=repo_url,
                    repo_ref=repo_ref,
                    repo_sha=current_sha(self._cache_dir, entry.id) if is_git else None,
                    manifest_repo_path=entry.repo.manifest if entry.repo is not None else None,
                    last_refresh=self._last_refresh.get(entry.id),
                )
            )
        return tuple(out)

    @classmethod
    def single(cls, manifest_path: str | Path, *, project_id: str = "default") -> ProjectRegistry:
        """A registry of one — the single-manifest server as a degenerate case."""
        entry = ProjectRegistryEntry(id=project_id, manifest_path=Path(manifest_path))
        # A single local manifest never clones, so the cache dir is unused.
        return cls((entry,), default_id=project_id, cache_dir=Path(manifest_path).parent)


def load_project_registry(
    path: str | Path,
    *,
    cache_dir: str | Path | None = None,
) -> ProjectRegistry:
    """Load a ``typeflux.projects.yaml`` registry file.

    Shape::

        version: "1"
        default: <project-id>     # optional; first entry otherwise
        projects:
          - id: <project-id>
            manifest: <path to typeflux.project.yaml>   # local checkout
          - id: <project-id>
            repo:                                        # Git-sourced
              url: https://github.com/org/repo
              ref: main
              manifest: typeflux.project.yaml            # path within the repo

    Each entry sets exactly one of ``manifest`` (resolved relative to this
    file) or ``repo``. Git-sourced projects clone into ``cache_dir`` (default
    ``.typeflux-clones`` next to the registry file).
    """
    registry_path = Path(path)
    try:
        raw = strict_safe_load(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectRegistryError(f"project registry not found: {registry_path}") from exc
    except (ValueError, YAMLError) as exc:
        # Wrap the strict loader's duplicate-key/size/alias failures like other registry
        # defects — a malformed registry is a config error, not an unhandled 500.
        raise ProjectRegistryError(
            f"project registry {registry_path} failed to parse: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ProjectRegistryError(f"project registry {registry_path} is not a YAML mapping")
    projects = raw.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ProjectRegistryError(
            f"project registry {registry_path} must list a non-empty 'projects'"
        )

    base = registry_path.parent
    entries: list[ProjectRegistryEntry] = []
    for index, item in enumerate(projects):
        if not isinstance(item, dict):
            raise ProjectRegistryError(f"project registry entry #{index} is not a mapping")
        pid = item.get("id")
        if not isinstance(pid, str) or not pid:
            raise ProjectRegistryError(f"project registry entry #{index} is missing a string 'id'")
        manifest = item.get("manifest")
        repo = item.get("repo")
        if (manifest is None) == (repo is None):
            raise ProjectRegistryError(
                f"project registry entry {pid!r} must set exactly one of 'manifest' or 'repo'"
            )
        runtime = item.get("runtime", "python")
        if runtime not in ("python", "typescript"):
            # Fail closed on anything else — including the empty string: a
            # blank runtime must never silently mean the default.
            raise ProjectRegistryError(
                f"project registry entry {pid!r} 'runtime' must be 'python' or "
                f"'typescript', got {runtime!r}"
            )
        if repo is not None:
            if not isinstance(repo, dict):
                raise ProjectRegistryError(
                    f"project registry entry {pid!r} 'repo' is not a mapping"
                )
            try:
                repo_source = ProjectRepoSource.model_validate(repo)
            except ValueError as exc:
                raise ProjectRegistryError(
                    f"project registry entry {pid!r} has an invalid 'repo': {exc}"
                ) from exc
            entries.append(ProjectRegistryEntry(id=pid, repo=repo_source, runtime=runtime))
        else:
            if not isinstance(manifest, str) or not manifest:
                raise ProjectRegistryError(
                    f"project registry entry {pid!r} 'manifest' must be a non-empty string"
                )
            entries.append(
                ProjectRegistryEntry(
                    id=pid, manifest_path=(base / manifest).resolve(), runtime=runtime
                )
            )

    default_id = raw.get("default", entries[0].id)
    if not isinstance(default_id, str):
        raise ProjectRegistryError(
            f"project registry {registry_path} 'default' must be a string id"
        )
    resolved_cache = Path(cache_dir).resolve() if cache_dir else base / DEFAULT_CLONE_DIR_NAME
    return ProjectRegistry(tuple(entries), default_id=default_id, cache_dir=resolved_cache)


__all__ = [
    "REGISTRY_VERSION",
    "SUPPORTED_RESOLVER_RUNTIMES",
    "ProjectRefreshResult",
    "ProjectRegistry",
    "ProjectRegistryEntry",
    "ProjectRegistryError",
    "ProjectRepoSource",
    "ProjectSummary",
    "UnsupportedProjectRuntimeError",
    "load_project_registry",
]
