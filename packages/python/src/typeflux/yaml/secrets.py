from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class SecretValueFromSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str | None = None
    file: str | None = None
    required: bool = True

    @field_validator("env", "file")
    @classmethod
    def _validate_source(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("secret reference source values must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_single_source(self) -> SecretValueFromSpec:
        if (self.env is None) == (self.file is None):
            raise ValueError("secret value_from must configure exactly one of env or file")
        return self


class SecretValueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_from: SecretValueFromSpec


@dataclass(frozen=True)
class SecretReferenceRecord:
    runtime_path: str
    source_kind: str
    source_name: str
    configured: bool


SecretTextValue = str | SecretValueSpec | None


def resolve_optional_secret_text(value: SecretTextValue, *, runtime_path: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    return _resolve_secret_text(value.value_from, runtime_path=runtime_path)


def resolve_optional_secret_bytes(
    value: SecretValueSpec | None,
    *,
    runtime_path: str,
    env: Mapping[str, str] | None = None,
) -> bytes | None:
    if value is None:
        return None
    return _resolve_secret_bytes(value.value_from, runtime_path=runtime_path, env=env)


def secret_value_configured(value: SecretTextValue) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    return _secret_source_configured(value.value_from)


#: The fixed secret-slot allowlist, as dotted paths from the spec root — the ONE list the
#: reference walker and the control plane's profile-credential redaction both consume, so a
#: slot cannot be reported by one and missed by the other (the TS SDK exports the same list
#: as SECRET_SLOT_PATHS). A boolean ``tls`` simply has no leaf at the tls paths, matching
#: the walker's old explicit block check.
SECRET_SLOT_PATHS: tuple[str, ...] = (
    "runtime.temporal.api_key",
    "runtime.temporal.tls.server_root_ca_cert",
    "runtime.temporal.tls.client_cert",
    "runtime.temporal.tls.client_private_key",
    # AES-256-GCM payload codec key material (#188). A wildcard slot: one record per
    # declared key, keyed by the key id, so bundles/plans mask the key VALUE to
    # source_kind/source_name (the standing "never credentials" contract).
    "runtime.temporal.payload_codec.keys[*].value_from",
    "runtime.provider.api_key",
    # Custom-extension config maps (#792). Map wildcards: one record per config entry,
    # keyed by the entry's key, so a custom class's declared credentials join the same
    # inventory as every built-in slot. The TS SDK stub-rejects config at spec load, but
    # its control plane still redacts these slots in raw profile fragments, so the TS
    # list carries them too.
    "runtime.provider.config{*}",
    "runtime.registry.config{*}",
    "runtime.observability.config{*}",
    # Spec-declared observability credentials (#793): the audit's "one credential
    # contract" gap — the backends' env-var reads were invisible to this inventory.
    "runtime.observability.langfuse.public_key",
    "runtime.observability.langfuse.secret_key",
    "runtime.observability.langsmith.api_key",
)

#: Marker for the array-wildcard segment inside a slot path.
_WILDCARD = "[*]"

#: Marker for a map-wildcard slot: the path prefix names a flat string map whose every
#: entry is a secret slot (the custom-extension ``config`` blocks, #792).
_MAP_WILDCARD = "{*}"


def secret_slot_value(root: Any, path: str) -> Any:
    """Walk a dotted slot path from the spec root, tolerating models AND plain
    mappings (a profile fragment is a bare ``runtime`` dict); None when any
    segment is absent or a non-container (e.g. a boolean ``tls``)."""
    node = root
    for segment in path.split("."):
        if isinstance(node, Mapping):
            node = node.get(segment)
        else:
            node = getattr(node, segment, None)
        if node is None:
            return None
    return node


def secret_reference_records(spec: Any) -> tuple[SecretReferenceRecord, ...]:
    records: list[SecretReferenceRecord] = []
    for path in SECRET_SLOT_PATHS:
        if _MAP_WILDCARD in path:
            _append_map_references(records, path, spec)
        elif _WILDCARD in path:
            _append_wildcard_references(records, path, spec)
        else:
            _append_secret_reference(records, path, secret_slot_value(spec, path))
    return tuple(records)


def _append_map_references(
    records: list[SecretReferenceRecord],
    path: str,
    root: Any,
) -> None:
    """Expand a ``prefix{*}`` slot over the flat map at ``prefix`` (a custom-extension
    ``config`` block): one record per entry, its ``runtime_path`` keyed by the entry's key.
    Values may be literal strings or ``value_from`` references (also as bare mappings, since
    a profile fragment is a plain dict) — either way only kind/name is recorded, never the
    value."""
    prefix = path[: -len(_MAP_WILDCARD)].rstrip(".")
    mapping = secret_slot_value(root, prefix)
    if not isinstance(mapping, Mapping):
        return
    for key, value in mapping.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, Mapping) and not isinstance(value, SecretValueSpec):
            try:
                value = SecretValueSpec.model_validate(value)
            except Exception:  # noqa: BLE001 - a malformed entry yields no record.
                continue
        _append_secret_reference(records, f"{prefix}[{key}]", value)


def _append_wildcard_references(
    records: list[SecretReferenceRecord],
    path: str,
    root: Any,
) -> None:
    """Expand a ``prefix[*].leaf`` slot over the array at ``prefix`` (the payload-codec
    keys): one record per element, its ``runtime_path`` keyed by the element's ``id`` so
    every key's provenance surfaces individually — the value itself is never recorded."""
    prefix, _, rest = path.partition(_WILDCARD)
    array = secret_slot_value(root, prefix.rstrip("."))
    if not isinstance(array, (list, tuple)):
        return
    leaf = rest.lstrip(".")
    for index, item in enumerate(array):
        key_id = _item_attr(item, "id")
        source = _item_attr(item, leaf)
        # The leaf (`value_from`) is itself the source; wrap it so the shared appender
        # (which expects a `{value_from: ...}` reference) records env/file, not the value.
        wrapped: Any
        if isinstance(source, SecretValueFromSpec):
            wrapped = SecretValueSpec(value_from=source)
        elif isinstance(source, Mapping):
            try:
                wrapped = SecretValueSpec(value_from=SecretValueFromSpec.model_validate(source))
            except Exception:  # noqa: BLE001 - a malformed source yields no record.
                continue
        else:
            continue
        slot_key = key_id if isinstance(key_id, str) and key_id else str(index)
        _append_secret_reference(records, f"{prefix.rstrip('.')}[{slot_key}].{leaf}", wrapped)


def _item_attr(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _append_secret_reference(
    records: list[SecretReferenceRecord],
    runtime_path: str,
    value: Any,
) -> None:
    if isinstance(value, str) and value:
        # Literal credentials are recorded by kind only — never the value —
        # so manifests and control-plane bundles can surface them.
        records.append(
            SecretReferenceRecord(
                runtime_path=runtime_path,
                source_kind="literal",
                source_name="",
                configured=True,
            )
        )
        return
    if not isinstance(value, SecretValueSpec):
        return
    source = value.value_from
    if source.env is not None:
        records.append(
            SecretReferenceRecord(
                runtime_path=runtime_path,
                source_kind="env",
                source_name=source.env,
                configured=_secret_source_configured(source),
            )
        )
        return
    if source.file is not None:
        records.append(
            SecretReferenceRecord(
                runtime_path=runtime_path,
                source_kind="file",
                source_name=source.file,
                configured=_secret_source_configured(source),
            )
        )


def custom_extension_config_sources(
    spec: Any,
) -> tuple[tuple[str, SecretValueSpec], ...]:
    """Every ``value_from`` entry in the custom-extension config maps as
    ``(runtime_path, SecretValueSpec)`` — the deployment renderer's injection view of the
    ``config{*}`` slots, derived from the SAME ``SECRET_SLOT_PATHS`` walk the inventory
    uses so a slot cannot be inventoried yet missing from generated manifests (#792).
    Literal entries need no injection (they live in the spec itself) and are skipped."""
    sources: list[tuple[str, SecretValueSpec]] = []
    for path in SECRET_SLOT_PATHS:
        if not path.endswith(_MAP_WILDCARD):
            continue
        prefix = path[: -len(_MAP_WILDCARD)].rstrip(".")
        mapping = secret_slot_value(spec, prefix)
        if not isinstance(mapping, Mapping):
            continue
        for key, value in mapping.items():
            if isinstance(value, SecretValueSpec):
                sources.append((f"{prefix}[{key}]", value))
    return tuple(sources)


def resolve_custom_extension_config(
    config: Mapping[str, Any] | None, *, kind: str
) -> dict[str, str] | None:
    """Resolve a custom-extension ``config`` block (#792) to the plain string map handed to
    the class as ``cls(config=...)``. Literals pass through; ``value_from`` references
    resolve like every other secret slot (required-by-default → a missing source raises).
    A declared-optional entry whose source is absent is omitted, so the class sees only
    what actually resolved. ``None`` in → ``None`` out (undeclared keeps the zero-arg
    constructor path)."""
    if config is None:
        return None
    resolved: dict[str, str] = {}
    for key, value in config.items():
        text = resolve_optional_secret_text(value, runtime_path=f"runtime.{kind}.config[{key}]")
        if text is not None:
            resolved[key] = text
    return resolved


def _resolve_secret_text(source: SecretValueFromSpec, *, runtime_path: str) -> str | None:
    if source.env is not None:
        value = os.getenv(source.env)
        return _normalize_secret_text(
            value,
            runtime_path=runtime_path,
            source=f"env {source.env}",
            required=source.required,
        )
    if source.file is not None:
        path = Path(source.file)
        if not path.exists():
            if source.required:
                raise ValueError(
                    f"missing required secret for {runtime_path}: file {source.file} does not exist"
                )
            return None
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"could not read secret for {runtime_path}: file {source.file}"
            ) from exc
        return _normalize_secret_text(
            value,
            runtime_path=runtime_path,
            source=f"file {source.file}",
            required=source.required,
        )
    raise ValueError(f"secret value_from for {runtime_path} must configure env or file")


def _resolve_secret_bytes(
    source: SecretValueFromSpec,
    *,
    runtime_path: str,
    env: Mapping[str, str] | None = None,
) -> bytes | None:
    # ``env`` overrides the process environment for the env-source lookup (default
    # os.environ) so callers can thread a project environment's ``variables`` layer in
    # without mutating the process env (binding_ts). File sources are unaffected.
    environ: Mapping[str, str] = os.environ if env is None else env
    if source.env is not None:
        value = environ.get(source.env)
        text = _normalize_secret_text(
            value,
            runtime_path=runtime_path,
            source=f"env {source.env}",
            required=source.required,
        )
        return None if text is None else text.encode("utf-8")
    if source.file is not None:
        path = Path(source.file)
        if not path.exists():
            if source.required:
                raise ValueError(
                    f"missing required secret for {runtime_path}: file {source.file} does not exist"
                )
            return None
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"could not read secret for {runtime_path}: file {source.file}"
            ) from exc
        if not raw_bytes:
            if source.required:
                raise ValueError(f"secret for {runtime_path} from file {source.file} is empty")
            return None
        return raw_bytes
    raise ValueError(f"secret value_from for {runtime_path} must configure env or file")


def _normalize_secret_text(
    value: str | None,
    *,
    runtime_path: str,
    source: str,
    required: bool,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"missing required secret for {runtime_path}: {source} is not set")
        return None
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValueError(f"secret for {runtime_path} from {source} is empty")
        return None
    return normalized


def _secret_source_configured(source: SecretValueFromSpec) -> bool:
    if source.env is not None:
        value = os.getenv(source.env)
        return bool(value and value.strip())
    if source.file is not None:
        try:
            path = Path(source.file)
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False
    return False


__all__ = [
    "SecretReferenceRecord",
    "SecretTextValue",
    "SecretValueFromSpec",
    "SecretValueSpec",
    "resolve_optional_secret_bytes",
    "custom_extension_config_sources",
    "resolve_custom_extension_config",
    "resolve_optional_secret_text",
    "secret_reference_records",
    "secret_value_configured",
]
