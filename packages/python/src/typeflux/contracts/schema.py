"""Provider-safe JSON Schema profile + mappers (#389).

The neutral cross-SDK form for structured output is JSON Schema (draft
2020-12). The **provider-safe profile** is the subset accepted by every
supported structured-output mode (Gemini ``response_schema``, OpenAI strict
``json_schema``, Anthropic ``json_schema``). Gemini is the binding constraint:
it does not support ``$ref``/``$defs`` or open objects, so the profile inlines
refs and closes every object.

``to_provider_safe`` normalizes an arbitrary schema to the profile;
``lint_provider_safe`` reports constructs that cannot be normalized. Both SDKs
(Pydantic here, Zod in #400) map their native schema to draft-2020-12 and then
share this normalizer, so the same typed shape yields equivalent provider-safe
output everywhere.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

__all__ = [
    "ProviderSchemaError",
    "Violation",
    "lint_provider_safe",
    "pydantic_provider_schema",
    "to_provider_safe",
]

# Annotation/metadata keys dropped from the wire schema. They are not part of
# the structured-output contract and some providers reject unknown ones.
_STRIP_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "$anchor",
        "$comment",
        "$defs",
        "definitions",
        "default",
        "examples",
        # `discriminator` annotates a oneOf union; we normalize oneOf -> anyOf
        # (see _normalize), so the discriminator hint is dropped with it.
        "discriminator",
    }
)

# ``format`` values kept; anything else is dropped (providers reject unknowns).
_ALLOWED_FORMATS = frozenset(
    {"date-time", "date", "time", "duration", "uri", "uri-reference", "email", "uuid"}
)

# Keywords with no portable structured-output expression. ``rule`` is the stable
# id surfaced in a Violation.
_DISALLOWED_KEYS: dict[str, str] = {
    "patternProperties": "pattern-properties",
    "dependentSchemas": "dependent-schemas",
    "dependentRequired": "dependent-required",
    "dependencies": "dependencies",
    "if": "conditional",
    "then": "conditional",
    "else": "conditional",
    "not": "not",
    # Single-branch allOf is flattened in _normalize; a multi-branch allOf that
    # survives normalization has no portable structured-output form.
    "allOf": "allOf-unsupported",
    "prefixItems": "tuple-items",
}

# Keys whose value is itself a schema (recursed by the linter).
_SCHEMA_VALUE_KEYS = ("items", "additionalProperties", "contains")
# Keys whose value is a list of schemas.
_SCHEMA_LIST_KEYS = ("anyOf", "allOf", "oneOf", "prefixItems")
# Keys whose value maps names to schemas.
_SCHEMA_MAP_KEYS = ("properties", "$defs", "definitions", "patternProperties")


@dataclass(frozen=True)
class Violation:
    """A single reason a schema is not provider-safe."""

    pointer: str
    rule: str
    detail: str


class ProviderSchemaError(ValueError):
    """Raised by ``to_provider_safe`` when normalization cannot make a schema
    provider-safe (e.g. a recursive model or an unsupported keyword)."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = list(violations)
        joined = "; ".join(f"{v.pointer} [{v.rule}]: {v.detail}" for v in self.violations)
        super().__init__(f"schema is not provider-safe: {joined}")


def pydantic_provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return the provider-safe JSON Schema for a Pydantic model."""

    return to_provider_safe(model.model_json_schema())


def to_provider_safe(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a draft-2020-12 schema to the provider-safe profile.

    Inlines ``$ref``/``$defs``, closes every object (``additionalProperties:
    false``), makes every property required (OpenAI strict) by turning
    originally-optional properties into nullable unions, flattens
    single-branch ``allOf``, and strips annotation-only keywords.

    Raises ``ProviderSchemaError`` if the result still contains a construct
    that no provider-safe schema can express.
    """

    defs: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        value = schema.get(key)
        if isinstance(value, dict):
            defs.update(value)
    inlined = _inline(copy.deepcopy(schema), defs, ())
    normalized = _normalize(inlined)
    violations = lint_provider_safe(normalized)
    if violations:
        raise ProviderSchemaError(violations)
    return normalized


def lint_provider_safe(schema: dict[str, Any]) -> list[Violation]:
    """Return the constructs in ``schema`` that are not provider-safe.

    Operates on an already-inlined/normalized schema; an empty list means the
    schema conforms to the profile.
    """

    violations: list[Violation] = []
    _lint(schema, "#", violations)
    return violations


# --- internals ---------------------------------------------------------------


def _ref_name(ref: str) -> str | None:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return None


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    """Resolve ``$ref`` against ``defs`` in-place. Recursive refs (a name already
    being expanded) are replaced with a marker the linter rejects, since no
    provider-safe schema can express recursion."""

    if isinstance(node, list):
        return [_inline(item, defs, stack) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str):
        name = _ref_name(ref)
        if name is None or name not in defs:
            return {key: _inline(value, defs, stack) for key, value in node.items()}
        if name in stack:
            return {"__recursive_ref__": name}
        target = _inline(copy.deepcopy(defs[name]), defs, (*stack, name))
        if not isinstance(target, dict):
            return target
        merged = dict(target)
        for key, value in node.items():
            if key == "$ref":
                continue
            merged[key] = _inline(value, defs, stack)
        return merged

    return {key: _inline(value, defs, stack) for key, value in node.items()}


def _normalize(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalize(item) for item in node]
    if not isinstance(node, dict):
        return node

    node = _flatten_single_all_of(node)

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRIP_KEYS:
            continue
        if key == "format":
            # The format *keyword* (a string). A user property named "format"
            # never reaches here — it is a key inside the properties map below.
            if isinstance(value, str) and value not in _ALLOWED_FORMATS:
                continue
            out[key] = value
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            # name -> schema maps (properties, patternProperties): recurse into
            # the child schemas; never treat the property names as keywords.
            out[key] = {name: _normalize(child) for name, child in value.items()}
        elif key in _SCHEMA_LIST_KEYS or key in _SCHEMA_VALUE_KEYS:
            out[key] = _normalize(value)
        else:
            # Scalar/other keyword (type, enum, required, title, minimum, ...):
            # the value is not a schema, so keep it verbatim.
            out[key] = value

    # Single-value Literal -> single-member enum (Gemini has no `const`).
    if "const" in out:
        out["enum"] = [out.pop("const")]
    # Tagged/discriminated unions emit oneOf; anyOf is the provider-safe form
    # and is generation-equivalent for structured output.
    if "oneOf" in out:
        out.setdefault("anyOf", []).extend(out.pop("oneOf"))

    if _is_object(out):
        _close_object(out)
    return out


def _flatten_single_all_of(node: dict[str, Any]) -> dict[str, Any]:
    """Pydantic emits ``{"allOf": [ref]}`` (often plus a description) for a
    single nested schema. After inlining the ref that is a single-branch allOf;
    merge it into the parent so the object rules below apply cleanly."""

    all_of = node.get("allOf")
    if not (isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], dict)):
        return node
    merged = dict(all_of[0])
    for key, value in node.items():
        if key == "allOf":
            continue
        merged[key] = value
    return merged


def _is_object(schema: dict[str, Any]) -> bool:
    type_ = schema.get("type")
    if type_ == "object":
        return True
    # A nullable object declares ``type: ["object", "null"]`` (hand-written/raw
    # schemas; Pydantic uses anyOf). Treat any type-array containing "object" as
    # an object so it gets closed; ``_close_object`` leaves the array intact.
    if isinstance(type_, list) and "object" in type_:
        return True
    return "properties" in schema and "type" not in schema


def _close_object(schema: dict[str, Any]) -> None:
    schema.setdefault("type", "object")
    additional = schema.get("additionalProperties")
    if additional is True or isinstance(additional, dict):
        # Explicitly open object/map (additionalProperties: true, or a schema
        # value such as dict[str, X]). Not provider-safe and not normalizable:
        # leave it untouched so the linter rejects it, rather than silently
        # closing it and hiding the violation. Checked before the properties
        # branch so an open object that also declares properties still raises.
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        schema["additionalProperties"] = False
        required = set(schema.get("required", ()))
        for name, prop in props.items():
            if name not in required and isinstance(prop, dict):
                props[name] = _make_nullable(prop)
        # OpenAI strict requires every property in ``required``; order-stable.
        schema["required"] = list(props.keys())
        return
    # Empty / implicitly-closed object.
    schema["additionalProperties"] = False
    schema.setdefault("required", [])


def _make_nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if _is_nullable(schema):
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _is_nullable(schema: dict[str, Any]) -> bool:
    type_ = schema.get("type")
    if type_ == "null" or (isinstance(type_, list) and "null" in type_):
        return True
    for variant in schema.get("anyOf", ()):
        if isinstance(variant, dict) and variant.get("type") == "null":
            return True
    return False


def _lint(node: Any, pointer: str, out: list[Violation]) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _lint(item, f"{pointer}/{index}", out)
        return
    if not isinstance(node, dict):
        return

    if "__recursive_ref__" in node:
        name = node["__recursive_ref__"]
        out.append(
            Violation(
                pointer,
                "recursive-ref",
                f"recursive model {name!r} cannot be expressed in a provider-safe schema",
            )
        )
        return
    if "$ref" in node:
        out.append(
            Violation(f"{pointer}/$ref", "unresolved-ref", f"unresolved $ref {node['$ref']!r}")
        )
    for key, rule in _DISALLOWED_KEYS.items():
        if key in node:
            out.append(Violation(f"{pointer}/{key}", rule, f"{key} is not provider-safe"))
    additional = node.get("additionalProperties")
    if additional is True:
        out.append(
            Violation(
                f"{pointer}/additionalProperties",
                "open-object",
                "additionalProperties: true is not provider-safe; use an explicit object",
            )
        )
    elif isinstance(additional, dict):
        out.append(
            Violation(
                f"{pointer}/additionalProperties",
                "open-map",
                "an open map (additionalProperties is a schema, e.g. dict[str, X]) is not "
                "provider-safe; use an explicit object or a list of {key, value} entries",
            )
        )

    for key in _SCHEMA_MAP_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            for name, child in value.items():
                _lint(child, f"{pointer}/{key}/{name}", out)
    for key in _SCHEMA_LIST_KEYS:
        value = node.get(key)
        if isinstance(value, list):
            _lint(value, f"{pointer}/{key}", out)
    for key in _SCHEMA_VALUE_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            _lint(value, f"{pointer}/{key}", out)
