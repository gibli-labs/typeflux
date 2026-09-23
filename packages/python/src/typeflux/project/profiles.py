"""Project-local component profiles.

Profiles are reusable, file-referenced runtime fragments — the first step of
the control-plane composability arc (#214). Three kinds exist, each owning a
disjoint subtree of the workflow runtime config:

- ``provider``  -> ``runtime.provider``
- ``registry``  -> ``runtime.registry``
- ``runtime``   -> ``runtime.temporal``, ``runtime.observability``,
  ``runtime.provider_retry``, ``runtime.provider_limits``

A workflow/environment selection picks at most one profile per kind. Profiles
apply through the same override-wins deep merge and the same
``validate_yaml_overrides`` allowlist as environment overrides, at lower
precedence: ``workflow YAML < profiles < environment overrides``. Because
kinds own disjoint subtrees, cross-profile conflicts are structurally
impossible; incoherent merges surface through the existing downstream
validation (spec models, ``supported_provider_params`` preflight, policy
admission) rather than silently.

Each loaded profile carries a content hash (canonical-JSON sha256, the policy
recipe) so resolved bundles and manifests can record safe component
provenance (#215).
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from typeflux.core.errors import TypefluxError
from typeflux.manifests._common import canonical_json
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.loader import strict_safe_load
from typeflux.yaml.overrides import validate_yaml_overrides, yaml_override_paths

ProfileKind = Literal["provider", "registry", "runtime"]

PROFILE_KINDS: tuple[ProfileKind, ...] = ("provider", "registry", "runtime")

#: runtime.* keys each profile kind may set. Disjoint by construction so two
#: selected profiles can never both own a value.
_OWNED_RUNTIME_KEYS: dict[ProfileKind, frozenset[str]] = {
    "provider": frozenset({"provider"}),
    "registry": frozenset({"registry"}),
    "runtime": frozenset({"temporal", "observability", "provider_retry", "provider_limits"}),
}


class ProjectProfileError(TypefluxError, ValueError):
    """Raised when a component profile cannot be loaded or applied safely."""


class ProjectProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: Literal["1"] = "1"
    name: str
    kind: ProfileKind
    runtime: dict[str, Any] = Field(default_factory=dict)
    profile_path: Path = Field(default=Path("profile.yaml"), exclude=True)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("profile name must be non-empty")
        return value

    @field_validator("profile_path", mode="before")
    @classmethod
    def _validate_profile_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    def overrides(self) -> dict[str, Any]:
        return {"runtime": dict(self.runtime)} if self.runtime else {}

    @property
    def content_hash(self) -> str:
        payload = {
            "version": self.version,
            "name": self.name,
            "kind": self.kind,
            "runtime": self.runtime,
        }
        return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class AppliedComponentProfile(BaseModel):
    """Safe provenance record for one applied component profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProfileKind
    id: str
    name: str
    content_hash: str
    source_path: str
    override_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_project_profile(
    project: TypefluxProjectSpec,
    *,
    kind: ProfileKind,
    profile_id: str,
) -> ProjectProfileSpec:
    declared = getattr(project.profiles, kind, {}) if project.profiles is not None else {}
    raw_path = declared.get(profile_id)
    if raw_path is None:
        raise ProjectProfileError(f"unknown project {kind} profile: {profile_id}")
    profile_path = _resolve_project_path(project, raw_path)
    raw = strict_safe_load(profile_path.read_text(encoding="utf-8"))
    if raw is None:
        raise ProjectProfileError(f"empty component profile: {profile_path}")
    if not isinstance(raw, dict):
        raise ProjectProfileError(f"component profile must be a YAML mapping: {profile_path}")
    profile = ProjectProfileSpec.model_validate(raw).model_copy(
        update={"profile_path": profile_path}
    )
    if profile.kind != kind:
        raise ProjectProfileError(
            f"profile {profile_id!r} is referenced under profiles.{kind} but declares "
            f"kind: {profile.kind}"
        )
    _validate_profile_subtree(profile)
    return profile


def resolve_selected_profiles(
    project: TypefluxProjectSpec,
    *,
    workflow_selection: dict[str, str],
    environment_selection: dict[str, str],
) -> tuple[tuple[str, ProjectProfileSpec], ...]:
    """Resolve the effective profile per kind.

    Environment selection replaces the workflow-level selection per kind
    (whole-reference replacement — no partial profile mixing).
    """
    effective = dict(workflow_selection)
    effective.update(environment_selection)
    selected: list[tuple[str, ProjectProfileSpec]] = []
    for kind in PROFILE_KINDS:
        profile_id = effective.get(kind)
        if profile_id is None:
            continue
        selected.append(
            (profile_id, load_project_profile(project, kind=kind, profile_id=profile_id))
        )
    return tuple(selected)


def profile_overrides_and_provenance(
    selected: tuple[tuple[str, ProjectProfileSpec], ...],
) -> tuple[dict[str, Any], tuple[AppliedComponentProfile, ...]]:
    """Merge selected profiles into one override payload plus provenance.

    Kinds own disjoint subtrees, so this merge can never conflict; it simply
    assembles the per-kind fragments into one ``runtime`` mapping.
    """
    overrides: dict[str, Any] = {}
    provenance: list[AppliedComponentProfile] = []
    for profile_id, profile in selected:
        fragment = profile.overrides()
        if fragment:
            runtime = overrides.setdefault("runtime", {})
            runtime.update(fragment["runtime"])
        provenance.append(
            AppliedComponentProfile(
                kind=profile.kind,
                id=profile_id,
                name=profile.name,
                content_hash=profile.content_hash,
                source_path=str(profile.profile_path),
                override_paths=yaml_override_paths(fragment),
            )
        )
    return overrides, tuple(provenance)


def validate_profile_selection(selection: dict[str, str], *, context: str) -> None:
    for kind in selection:
        if kind not in PROFILE_KINDS:
            raise ProjectProfileError(
                f"{context} selects unknown profile kind {kind!r}; "
                f"valid kinds: {', '.join(PROFILE_KINDS)}"
            )


def _validate_profile_subtree(profile: ProjectProfileSpec) -> None:
    owned = _OWNED_RUNTIME_KEYS[profile.kind]
    outside = sorted(set(profile.runtime) - owned)
    if outside:
        raise ProjectProfileError(
            f"profile {profile.name!r} (kind: {profile.kind}) sets runtime keys outside "
            f"its owned subtree: {', '.join(outside)}; owned keys: {', '.join(sorted(owned))}"
        )
    overrides = profile.overrides()
    if overrides:
        # Profiles ride the same override allowlist as environments.
        validate_yaml_overrides(overrides, prefix=f"profiles.{profile.kind}.{profile.name}")


def _resolve_project_path(project: TypefluxProjectSpec, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project.project_dir / path).resolve()


__all__ = [
    "PROFILE_KINDS",
    "AppliedComponentProfile",
    "ProfileKind",
    "ProjectProfileError",
    "ProjectProfileSpec",
    "load_project_profile",
    "profile_overrides_and_provenance",
    "resolve_selected_profiles",
    "validate_profile_selection",
]
