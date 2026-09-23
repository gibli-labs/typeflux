from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_ALLOWED_OVERRIDE_KEYS = {"task_queue", "runtime"}
_ALLOWED_RUNTIME_OVERRIDE_KEYS = {
    "temporal",
    "registry",
    "provider",
    "provider_limits",
    "provider_retry",
    "observability",
}


@dataclass(frozen=True)
class YamlOverrideProvenance:
    source: str
    override_paths: tuple[str, ...]
    project_name: str | None = None
    environment_id: str | None = None
    environment_name: str | None = None
    workflow_id: str | None = None


def validate_yaml_overrides(
    overrides: Mapping[str, Any],
    *,
    prefix: str = "overrides",
) -> None:
    for key in overrides:
        if key not in _ALLOWED_OVERRIDE_KEYS:
            raise ValueError(f"{prefix}.{key} is not an allowed environment override")
    if "runtime" in overrides:
        runtime = overrides["runtime"]
        if not isinstance(runtime, Mapping):
            raise TypeError(f"{prefix}.runtime must be a mapping")
        for key in runtime:
            if key not in _ALLOWED_RUNTIME_OVERRIDE_KEYS:
                raise ValueError(f"{prefix}.runtime.{key} is not an allowed runtime override")


def yaml_override_paths(overrides: Mapping[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key, value in overrides.items():
        _append_override_paths(paths, str(key), value)
    return tuple(sorted(paths))


def _append_override_paths(paths: list[str], path: str, value: Any) -> None:
    if not isinstance(value, Mapping) or not value:
        paths.append(path)
        return
    for key, child in value.items():
        _append_override_paths(paths, f"{path}.{key}", child)


__all__ = [
    "YamlOverrideProvenance",
    "validate_yaml_overrides",
    "yaml_override_paths",
]
