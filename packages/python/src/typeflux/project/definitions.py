"""Safe definition projections for the console's explorer (#257).

Environments, policies, and component profiles are first-class declarative
objects; these projections render them read-only with a ``used_by`` reverse
index. Environment variable *values* are never serialized — names only;
policies are config by construction; profile subtrees carry ``value_from``
references only (#194/#214).
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from typeflux.project.environment import load_project_environment
from typeflux.project.policy import compose_project_policies, load_project_policy
from typeflux.project.profiles import load_project_profile
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.secrets import (
    SECRET_SLOT_PATHS,
    SecretValueFromSpec,
    SecretValueSpec,
)


class _Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class EnvironmentDefinition(_Definition):
    id: str
    name: str
    profile_path: str
    env_files: tuple[dict[str, Any], ...] = ()
    #: Names only — values may be sensitive and never serialize.
    variable_names: tuple[str, ...] = ()
    overrides: dict[str, Any] = {}
    #: Per-workflow profile selections declared in this environment.
    workflow_profiles: dict[str, dict[str, str]] = {}
    used_by: tuple[str, ...] = ()


class PolicySummary(_Definition):
    id: str
    name: str
    description: str | None = None
    path: str


class PolicyDefinition(_Definition):
    id: str
    name: str
    description: str | None = None
    extends: tuple[str, ...] = ()
    policy_hash: str | None = None
    #: Full rule rendering — policy rules are config, not secrets.
    rules: dict[str, Any] = {}
    used_by: tuple[str, ...] = ()


class ProfileSummary(_Definition):
    kind: str
    id: str
    name: str
    content_hash: str
    path: str


class ProfileDefinition(_Definition):
    kind: str
    id: str
    name: str
    content_hash: str
    path: str
    #: Owned runtime subtree; secrets appear as value_from references only.
    runtime: dict[str, Any] = {}
    used_by: tuple[str, ...] = ()


def environment_definition(
    project: TypefluxProjectSpec, environment_id: str
) -> EnvironmentDefinition:
    environment = load_project_environment(project, environment_id)
    used: set[str] = set(environment.workflows)
    for target in project.validation.targets.values():
        if target.environment == environment_id:
            used.update(target.workflows)
    return EnvironmentDefinition(
        id=environment_id,
        name=environment.name,
        profile_path=str(environment.profile_path),
        env_files=tuple(
            entry.model_dump(mode="json", exclude={"resolved_path"})
            for entry in environment.env_files
        ),
        variable_names=tuple(sorted(environment.variables)),
        overrides=environment.overrides,
        workflow_profiles={
            workflow_id: dict(getattr(spec, "profiles", {}) or {})
            for workflow_id, spec in sorted(environment.workflows.items())
        },
        used_by=tuple(sorted(used)),
    )


def policy_definitions(project: TypefluxProjectSpec) -> tuple[PolicySummary, ...]:
    summaries = []
    for policy_id in sorted(project.policies):
        spec = load_project_policy(project, policy_id)
        summaries.append(
            PolicySummary(
                id=policy_id,
                name=spec.name,
                description=spec.description,
                path=project.policies[policy_id],
            )
        )
    return tuple(summaries)


def policy_definition(project: TypefluxProjectSpec, policy_id: str) -> PolicyDefinition:
    spec = load_project_policy(project, policy_id)
    rules = spec.model_dump(mode="json", exclude={"version", "name", "description", "extends"})
    try:
        policy_hash: str | None = compose_project_policies(project, (policy_id,)).policy_hash
    except Exception:  # noqa: BLE001 - composition conflicts surface in validate.
        policy_hash = None
    used: set[str] = set()
    for target in project.validation.targets.values():
        if policy_id in target.policies:
            used.update(target.workflows)
    return PolicyDefinition(
        id=policy_id,
        name=spec.name,
        description=spec.description,
        extends=tuple(spec.extends),
        policy_hash=policy_hash,
        rules=rules,
        used_by=tuple(sorted(used)),
    )


def profile_definitions(project: TypefluxProjectSpec) -> tuple[ProfileSummary, ...]:
    summaries = []
    declared = project.profiles
    if declared is None:
        return ()
    for kind in ("provider", "registry", "runtime"):
        for profile_id, path in sorted((getattr(declared, kind, {}) or {}).items()):
            spec = load_project_profile(project, kind=kind, profile_id=profile_id)
            summaries.append(
                ProfileSummary(
                    kind=kind,
                    id=profile_id,
                    name=spec.name,
                    content_hash=spec.content_hash,
                    path=path,
                )
            )
    return tuple(summaries)


def _walk_fragment(node: Any, segments: list[str]) -> Any:
    """Walk dotted segments into a profile-fragment dict, tolerating any non-dict as absent —
    the ONE traversal all three slot shapes (flat, ``[*]`` array, ``{*}`` map) share, so a
    walk fix cannot land in one redaction path and miss another."""
    for segment in segments:
        node = node.get(segment) if isinstance(node, dict) else None
    return node


def _redact_wildcard_slot(runtime: dict[str, Any], slot: str) -> None:
    """Redact a ``prefix[*].leaf`` slot (the payload-codec keys) inside a profile runtime.

    Each key's ``value_from`` leaf is provenance when it is an exact ``{env|file}`` source
    (a ``SecretValueFromSpec``); anything else in that slot is masked whole, mirroring the
    flat-slot rule so a malformed profile fragment cannot smuggle raw key material."""
    prefix, _, rest = slot.partition("[*]")
    # prefix e.g. "runtime.temporal.payload_codec.keys" — drop the leading "runtime".
    node = _walk_fragment(runtime, prefix.split(".")[1:])
    if not isinstance(node, list):
        return
    leaf = rest.lstrip(".")
    for item in node:
        if not isinstance(item, dict) or leaf not in item:
            continue
        value = item[leaf]
        if value is not None and not _is_exact_value_from(value):
            item[leaf] = "***"


def _redact_map_slot(redacted: dict[str, Any], slot: str) -> None:
    """Redact a ``prefix{*}`` map slot (a custom-extension ``config`` block, #792) in a
    profile runtime fragment: exact ``value_from`` references stay — they name a source, not
    a credential — and every other entry value is masked whole, mirroring the flat-slot rule
    so a malformed fragment cannot smuggle a raw value."""
    # fragment IS the runtime subtree — drop the leading "runtime".
    segments = slot[: -len("{*}")].rstrip(".").split(".")[1:]
    parent = _walk_fragment(redacted, segments[:-1])
    leaf = segments[-1]
    if not isinstance(parent, dict) or leaf not in parent:
        return
    node = parent[leaf]
    if not isinstance(node, dict):
        # A raw fragment can put ANYTHING at the map slot (`config: sk-live`, a list);
        # the full spec would reject it, but profiles never pass full validation — mask
        # the whole value rather than letting a non-map literal through (codex).
        if node is not None:
            parent[leaf] = "***"
        return
    for key, value in node.items():
        if value is not None and not _is_exact_secret_reference(value):
            node[key] = "***"


def _is_exact_value_from(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        SecretValueFromSpec.model_validate(value)
    except Exception:  # noqa: BLE001 - any validation failure means "not a source reference".
        return False
    return True


def _is_exact_secret_reference(value: Any) -> bool:
    # Only the STRICT reference shape (a lone `value_from` with exactly one of env/file,
    # no extra keys) is provenance; a malformed shape can smuggle credential material
    # (`{value_from: ..., fallback: sk-...}`) and is masked whole.
    if not isinstance(value, dict):
        return False
    try:
        SecretValueSpec.model_validate(value)
    except Exception:  # noqa: BLE001 - any validation failure means "not a reference"
        return False
    return True


def _sanitize_profile_host(host: str) -> str:
    # Strip credential-bearing parts from a URL-shaped registry host before it leaves the
    # API (the TS control plane's sanitizeHost treatment): cut at the first whitespace (a
    # valid host has none), drop query/fragment, and strip userinfo. For an absolute URL
    # the authority is rebuilt credential-free; relative/opaque forms strip greedily to
    # the LAST `@` before the path so an embedded `@` in the credential can't leave a
    # fragment behind.
    single_line = re.split(r"\s", host, maxsplit=1)[0]
    # WHATWG-normalize the authority marker: the TS side uses the real URL parser, which
    # treats ANY run of slashes/backslashes after a scheme as `//` (`https:////u:p@h`,
    # `http:/u:p@h`). urllib does not, leaving netloc empty and the credential in the
    # path — normalize first so those forms hit the netloc-stripping branch (codex).
    normalized = re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*:)[/\\]+", r"\1//", single_line)
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        # urlsplit rejects some malformed authorities (e.g. `https://[bad`) — the TS
        # sanitizer's parser does too; both fall back to the greedy regex strip (codex).
        parsed = None
    if parsed is not None and parsed.netloc:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    stripped = re.sub(r"[?#].*$", "", single_line)
    stripped = re.sub(r"^([/\\]*)[^/\\]*@", r"\1", stripped)
    # Unparseable AND still credential-suspect: mask whole rather than guess at the
    # authority (fail closed — same policy as the secret slots; TS matches).
    return "***" if "@" in stripped else stripped


def _redact_profile_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    """Redact credential material from a profile's runtime before it leaves the API.

    A ``value_from`` reference is provenance and renders as-is; ANYTHING else occupying a
    fixed secret slot (string, number, malformed reference) is replaced with ``"***"`` —
    the slot list is ``SECRET_SLOT_PATHS``, the same allowlist ``secret_reference_records``
    walks, so a slot cannot be reported there yet leak here. URL-shaped registry hosts are
    stripped of userinfo/query. Deep-copied: the loaded spec is never mutated. Mirrors the
    TS control plane's ``redactLiteralSecrets`` (marker and slot list kept consistent).
    """
    redacted = deepcopy(runtime)
    for slot in SECRET_SLOT_PATHS:
        if "{*}" in slot:
            _redact_map_slot(redacted, slot)
            continue
        if "[*]" in slot:
            _redact_wildcard_slot(redacted, slot)
            continue
        segments = slot.split(".")[1:]  # the fragment IS the subtree under "runtime"
        node = _walk_fragment(redacted, segments[:-1])
        leaf = segments[-1]
        if not isinstance(node, dict) or leaf not in node:
            continue
        value = node[leaf]
        if value is not None and not _is_exact_secret_reference(value):
            node[leaf] = "***"
    registry = redacted.get("registry")
    if isinstance(registry, dict) and isinstance(registry.get("host"), str):
        registry["host"] = _sanitize_profile_host(registry["host"])
    return redacted


def profile_definition(
    project: TypefluxProjectSpec, *, kind: str, profile_id: str
) -> ProfileDefinition:
    spec = load_project_profile(project, kind=kind, profile_id=profile_id)
    used: set[str] = set()
    for workflow in project.workflows:
        if workflow.profiles.get(kind) == profile_id:
            used.add(workflow.id)
    for environment_id in project.environments:
        environment = load_project_environment(project, environment_id)
        for workflow_id, workflow_spec in environment.workflows.items():
            if (getattr(workflow_spec, "profiles", {}) or {}).get(kind) == profile_id:
                used.add(f"{workflow_id} ({environment_id})")
    declared = getattr(project.profiles, kind, {}) if project.profiles is not None else {}
    return ProfileDefinition(
        kind=kind,
        id=profile_id,
        name=spec.name,
        content_hash=spec.content_hash,
        path=declared.get(profile_id, ""),
        runtime=_redact_profile_runtime(spec.runtime),
        used_by=tuple(sorted(used)),
    )


__all__ = [
    "EnvironmentDefinition",
    "PolicyDefinition",
    "PolicySummary",
    "ProfileDefinition",
    "ProfileSummary",
    "environment_definition",
    "policy_definition",
    "policy_definitions",
    "profile_definition",
    "profile_definitions",
]
