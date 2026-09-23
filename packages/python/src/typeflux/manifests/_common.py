from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

_UNSTABLE_SCHEMA_MODULES = frozenset({"", "__main__", "__mp_main__", "builtins"})
_UNSTABLE_SCHEMA_MODULE_PREFIXES = ("pydantic.",)
_UNSTABLE_SCHEMA_MODULE_STATUS = "unstable"
_UNSTABLE_SCHEMA_MODULE_WARNING = "schema module omitted because it is not a stable import path"


def _neutralize_numbers(value: object) -> object:
    """Normalize numbers to a language-neutral JSON form so hashes reproduce
    across SDKs. JSON has one number type: ``JSON.stringify(0.0)`` is ``0`` in
    JavaScript but ``json.dumps(0.0)`` is ``0.0`` in Python, so an integral
    float (a temperature of ``0.0``, etc.) would otherwise hash differently in
    the TypeScript SDK. Integral, finite floats are emitted as integers; other
    numbers are unchanged (Python and JS agree on non-integral float repr)."""

    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {key: _neutralize_numbers(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_neutralize_numbers(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        _neutralize_numbers(value), sort_keys=True, separators=(",", ":"), default=str
    )


@dataclass(frozen=True)
class SchemaIdentity:
    name: str
    hash: str
    module: str | None = None
    module_status: str | None = None
    module_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "module": self.module,
                "name": self.name,
                "hash": self.hash,
                "module_status": self.module_status,
                "module_warning": self.module_warning,
            }
        )

    def to_flat_dict(self, prefix: str) -> dict[str, Any]:
        return drop_none(
            {
                f"{prefix}_module": self.module,
                f"{prefix}_name": self.name,
                f"{prefix}_hash": self.hash,
                f"{prefix}_module_status": self.module_status,
                f"{prefix}_module_warning": self.module_warning,
            }
        )


def schema_identity(model: type[BaseModel]) -> SchemaIdentity:
    module = getattr(model, "__module__", None)
    schema = model.model_json_schema()
    raw_title = schema.get("title")
    name = raw_title if isinstance(raw_title, str) and raw_title else model.__name__
    model_hash = sha256(canonical_json(schema).encode("utf-8")).hexdigest()
    if isinstance(module, str) and _is_stable_schema_module(module):
        return SchemaIdentity(module=module, name=name, hash=model_hash)
    return SchemaIdentity(
        name=name,
        hash=model_hash,
        module_status=_UNSTABLE_SCHEMA_MODULE_STATUS,
        module_warning=_UNSTABLE_SCHEMA_MODULE_WARNING,
    )


def drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _is_stable_schema_module(module: str) -> bool:
    if module in _UNSTABLE_SCHEMA_MODULES:
        return False
    return not module.startswith(_UNSTABLE_SCHEMA_MODULE_PREFIXES)
