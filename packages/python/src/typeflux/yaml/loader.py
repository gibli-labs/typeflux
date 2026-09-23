from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from typeflux.env import load_env, read_env_file_values
from typeflux.yaml.overrides import (
    YamlOverrideProvenance,
    validate_yaml_overrides,
    yaml_override_paths,
)
from typeflux.yaml.spec import TypefluxYamlSpec

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$(\$)?\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_YamlPath = tuple[str | int, ...]


def load_yaml_spec(
    path: str | Path,
    *,
    overrides: Mapping[str, Any] | None = None,
    runtime_defaults: Mapping[str, Any] | None = None,
    load_dotenv: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> TypefluxYamlSpec:
    # `env` is the base environment `${VAR}` references interpolate against (#760;
    # TS parity with `loadYamlSpec`'s `env` option). It defaults to `None`, meaning
    # the process `os.environ` — so an omitted `env` is a zero-behavior-change no-op.
    # A caller emitting COMMITTED, machine-independent artifacts (deployment renders)
    # passes a fixed mapping so the operator's shell never leaks into the resolved
    # bytes, and interpolation is then COMPLETELY hermetic: a `${VAR}` whose name is
    # absent from the mapping errors exactly as an unset shell variable would (no
    # silent `os.environ` fallback). This replaces the fragile `os.environ`-mutation
    # dance (`project_environment_context`) for the hermetic path.
    #
    # `load_dotenv=None` means "default for the mode": dotenv loading stays ON for the
    # ambient path (no `env`) and is OFF under an injected `env` — a hermetic call must
    # not mutate `os.environ` from a cwd `.env` / `TYPEFLUX_ENV_FILE` as a side effect.
    # An EXPLICIT `load_dotenv=True` with `env` supplied is honored, but the dotenv
    # values join the interpolation map only (injected keys win, mirroring how the
    # ambient path lets existing `os.environ` keys win over `.env`); `os.environ` is
    # never touched.
    if load_dotenv is None:
        load_dotenv = env is None
    if load_dotenv:
        if env is None:
            load_env()
        else:
            env = {**read_env_file_values(), **env}
    yaml_path = Path(path)
    raw = strict_safe_load(yaml_path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"empty YAML spec: {yaml_path}")
    if not isinstance(raw, dict):
        raise TypeError("Typeflux YAML spec must be a mapping")
    if runtime_defaults:
        # Project-level defaults sit BENEATH the workflow YAML: the YAML
        # merges over them, and profiles/environment overrides still win
        # over both. Same allowlist as every other layer.
        defaults_layer = {"runtime": dict(runtime_defaults)}
        validate_yaml_overrides(defaults_layer)
        raw = _deep_merge(defaults_layer, raw)
    if overrides:
        validate_yaml_overrides(overrides)
        raw = _deep_merge(raw, dict(overrides))
    spec = TypefluxYamlSpec.model_validate(_interpolate_env(raw, source_path=yaml_path, env=env))
    spec._source_path = yaml_path.resolve()
    if overrides:
        spec._override_provenance = YamlOverrideProvenance(
            source="load_yaml_spec",
            override_paths=yaml_override_paths(overrides),
        )
    _warn_literal_credentials(spec, source_path=yaml_path)
    return spec


# Bounds for operator-trusted config files; hosted/multi-tenant deployments
# must treat tenant YAML as untrusted input beyond these (see docs).
MAX_YAML_BYTES = 1024 * 1024
MAX_YAML_ALIASES = 1000
MAX_YAML_DEPTH = 200


def strict_safe_load(text: str) -> Any:
    """safe_load that rejects duplicate mapping keys instead of last-wins.

    Silent last-wins lets a YAML file display one value to a reader while the
    runtime (and the control-plane bundle) resolves another. Input size and
    alias counts are bounded to keep pathological documents from amplifying
    through downstream traversal.
    """
    try:
        import yaml as pyyaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard.
        raise RuntimeError("pyyaml is required to load Typeflux YAML specs") from exc
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise ValueError(f"YAML document exceeds the {MAX_YAML_BYTES} byte limit")
    return pyyaml.load(text, Loader=_strict_yaml_loader_cls())


@cache
def _strict_yaml_loader_cls() -> type:
    import yaml as pyyaml

    class _StrictYamlLoader(pyyaml.SafeLoader):
        _typeflux_alias_count = 0
        _typeflux_depth = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(pyyaml.events.AliasEvent):
                self._typeflux_alias_count += 1
                if self._typeflux_alias_count > MAX_YAML_ALIASES:
                    raise pyyaml.YAMLError(
                        f"YAML document exceeds the {MAX_YAML_ALIASES} alias limit"
                    )
            # Depth is bounded like bytes and aliases: PyYAML composes recursively, so a
            # deeply nested document raises RecursionError — a RuntimeError no caller's
            # YAMLError/ValueError handling covers. No legitimate spec nests remotely
            # this deep; fail as a normal parse error instead.
            self._typeflux_depth += 1
            try:
                if self._typeflux_depth > MAX_YAML_DEPTH:
                    raise pyyaml.YAMLError(
                        f"YAML document exceeds the {MAX_YAML_DEPTH} nesting-depth limit"
                    )
                return super().compose_node(parent, index)
            finally:
                self._typeflux_depth -= 1

        def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
            # Scan before flattening: YAML merge keys (<<) deliberately
            # produce overlapping keys where the explicit key wins, so merge
            # nodes are skipped and only author-written duplicates fail.
            seen: set[Any] = set()
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                if isinstance(key, list):
                    key = tuple(key)
                if key in seen:
                    raise pyyaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
            return super().construct_mapping(node, deep)

        def construct_yaml_int(self, node: Any) -> int:
            # The core-schema resolver below only sends decimal / 0o / 0x forms, but
            # SafeConstructor's 1.1 constructor treats ANY leading zero as octal —
            # int("019", 8) raises where YAML 1.2 (and the TS SDK) reads decimal 19.
            value = self.construct_scalar(node)
            sign = -1 if value.startswith("-") else 1
            digits = value.lstrip("+-")
            if digits.startswith("0x"):
                return sign * int(digits[2:], 16)
            if digits.startswith("0o"):
                return sign * int(digits[2:], 8)
            return sign * int(digits, 10)

    # YAML 1.2 CORE SCHEMA scalars (#602): PyYAML's inherited resolvers are YAML 1.1 —
    # `on`/`yes` resolve to booleans, `1:30` to sexagesimal 90, dates to datetime
    # objects — while the TypeScript SDK's `yaml` package parses 1.2, so the same file
    # carried different VALUES per SDK (observable through the cross-SDK profile
    # content_hash, #570). A FRESH resolver table (not the inherited one) registers
    # exactly the 1.2 core set; the 1.1-only forms fall through to plain strings, and
    # merge keys (`<<`) stay supported. Write booleans as true/false.
    _StrictYamlLoader.yaml_implicit_resolvers = {}
    _core = [
        (
            "tag:yaml.org,2002:null",
            re.compile(r"^(?:~|null|Null|NULL|)$"),
            ["~", "n", "N", None],
        ),
        (
            "tag:yaml.org,2002:bool",
            re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
            list("tTfF"),
        ),
        (
            "tag:yaml.org,2002:int",
            re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
            list("-+0123456789"),
        ),
        (
            "tag:yaml.org,2002:float",
            re.compile(
                r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
                r"|[-+]?\.(?:inf|Inf|INF)"
                r"|\.(?:nan|NaN|NAN))$"
            ),
            list("-+0123456789."),
        ),
        ("tag:yaml.org,2002:merge", re.compile(r"^(?:<<)$"), ["<"]),
    ]
    for _tag, _regexp, _first in _core:
        _StrictYamlLoader.add_implicit_resolver(_tag, _regexp, _first)
    _StrictYamlLoader.add_constructor("tag:yaml.org,2002:int", _StrictYamlLoader.construct_yaml_int)

    return _StrictYamlLoader


_LITERAL_CREDENTIAL_WARNING = (
    "%s in %s carries a literal credential value; use a value_from "
    "secret reference (value_from.env or value_from.file) instead"
)


def _is_literal_credential(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _warn_literal_credentials(spec: TypefluxYamlSpec, *, source_path: Path) -> None:
    # Interpolated and hardcoded values are indistinguishable after loading;
    # both put the credential text into the resolved spec, which deployment
    # generation and require_secret_references policies reject. Only the
    # field path is logged — credential values never reach the log call.
    if _is_literal_credential(spec.runtime.temporal.api_key):
        logger.warning(_LITERAL_CREDENTIAL_WARNING, "runtime.temporal.api_key", source_path)
    # Observability credentials joined the secret contract in #793 — a literal here earns
    # the same warning as a literal api_key.
    langfuse = spec.runtime.observability.langfuse
    langsmith = spec.runtime.observability.langsmith
    for runtime_path, value in (
        ("runtime.observability.langfuse.public_key", langfuse.public_key if langfuse else None),
        ("runtime.observability.langfuse.secret_key", langfuse.secret_key if langfuse else None),
        ("runtime.observability.langsmith.api_key", langsmith.api_key if langsmith else None),
    ):
        if _is_literal_credential(value):
            logger.warning(_LITERAL_CREDENTIAL_WARNING, runtime_path, source_path)
    if _is_literal_credential(spec.runtime.provider.api_key):
        logger.warning(_LITERAL_CREDENTIAL_WARNING, "runtime.provider.api_key", source_path)


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(merged[key], dict(value))
            continue
        merged[key] = value
    return merged


def _interpolate_env(
    value: Any,
    *,
    source_path: Path,
    value_path: _YamlPath = (),
    env: Mapping[str, str] | None = None,
) -> Any:
    if isinstance(value, dict):
        return {
            key: _interpolate_env(
                item,
                source_path=source_path,
                value_path=(*value_path, str(key)),
                env=env,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _interpolate_env(
                item, source_path=source_path, value_path=(*value_path, index), env=env
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if _is_prompt_text_path(value_path):
            # Prompt bodies are model-facing content, not configuration. A
            # literal ${NAME} in prompt text must reach the model verbatim,
            # never inject worker environment values.
            return value
        return _ENV_PATTERN.sub(
            lambda match: _replace_env(
                match, source_path=source_path, value_path=value_path, env=env
            ),
            value,
        )
    return value


def _is_prompt_text_path(path: _YamlPath) -> bool:
    if path[:3] == ("runtime", "registry", "prompts") and len(path) >= 4:
        # runtime.registry.prompts.<name> as a plain string prompt body.
        if len(path) == 4:
            return True
        rest = path[4:]
        # runtime.registry.prompts.<name>.messages[i].content (string content)
        # runtime.registry.prompts.<name>.messages[i].content[j].text
        if rest[0] == "messages" and len(rest) >= 3 and rest[2] == "content":
            return len(rest) == 3 or (len(rest) == 5 and rest[4] == "text")
        return False
    # activities.definitions[i].artifacts[j].attach.text is injected into
    # prompt messages alongside artifact parts.
    return (
        len(path) == 7
        and path[0] == "activities"
        and path[1] == "definitions"
        and path[3] == "artifacts"
        and path[5] == "attach"
        and path[6] == "text"
    )


def _replace_env(
    match: re.Match[str],
    *,
    source_path: Path,
    value_path: _YamlPath,
    env: Mapping[str, str] | None = None,
) -> str:
    if match.group(1) is not None:
        # $${NAME} escapes interpolation and renders a literal ${NAME}.
        return match.group(0)[1:]
    name = match.group(2)
    default = match.group(3)
    # `env is None` -> read the live process environment (`os.environ`, the
    # default). An injected mapping is read EXCLUSIVELY (no `os.environ` fallback),
    # so hermetic resolution can never leak the operator's shell. `.get` (not a
    # truthiness test) keeps an empty-string value SET: `${VAR}` with VAR="" renders
    # "", and only a genuinely absent name falls through to the default / error.
    source: Mapping[str, str] = os.environ if env is None else env
    value = source.get(name)
    if value is not None:
        return value
    if default is not None:
        return default
    raise KeyError(
        f"missing environment variable: {name} in {source_path} at {_format_yaml_path(value_path)}"
    )


def _format_yaml_path(path: _YamlPath) -> str:
    formatted = "$"
    for item in path:
        if isinstance(item, int):
            formatted += f"[{item}]"
        elif item.isidentifier():
            formatted += f".{item}"
        else:
            formatted += f"[{item!r}]"
    return formatted


__all__ = ["load_yaml_spec"]
