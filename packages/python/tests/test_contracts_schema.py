"""Tests for the provider-safe JSON Schema profile + Pydantic mapper (#389)."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from typeflux.contracts import (
    ProviderSchemaError,
    lint_provider_safe,
    pydantic_provider_schema,
    to_provider_safe,
)

GOLDEN_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schema-profile" / "golden"


class Urgency(StrEnum):
    low = "low"
    high = "high"


class Reference(BaseModel):
    source: str
    page: int | None = None


class ReviewPacket(BaseModel):
    """Representative model: nested objects, optional fields, enum, list, default."""

    subject: str = Field(description="the ticket subject")
    urgency: Urgency
    references: list[Reference]
    summary: str | None = None
    escalated: bool = False


def test_objects_are_closed_and_all_properties_required() -> None:
    schema = pydantic_provider_schema(ReviewPacket)

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    # OpenAI strict: every property is required, in declared order.
    assert schema["required"] == ["subject", "urgency", "references", "summary", "escalated"]


def test_optional_fields_become_required_but_nullable() -> None:
    schema = pydantic_provider_schema(ReviewPacket)

    # `summary` (Optional[str]) and `escalated` (default) were not originally
    # required → wrapped as a nullable union so the provider always emits them.
    summary = schema["properties"]["summary"]
    assert {"type": "null"} in summary["anyOf"]
    escalated = schema["properties"]["escalated"]
    assert {"type": "null"} in escalated["anyOf"]


def test_refs_are_inlined_and_defs_stripped() -> None:
    schema = pydantic_provider_schema(ReviewPacket)

    assert "$defs" not in schema and "definitions" not in schema
    assert json.dumps(schema).find("$ref") == -1
    # The nested Reference object is inlined under references.items.
    item = schema["properties"]["references"]["items"]
    assert item["type"] == "object"
    assert set(item["properties"]) == {"source", "page"}
    assert item["additionalProperties"] is False


def test_enum_is_preserved() -> None:
    schema = pydantic_provider_schema(ReviewPacket)
    assert schema["properties"]["urgency"]["enum"] == ["low", "high"]


def test_annotation_keys_are_stripped() -> None:
    schema = pydantic_provider_schema(ReviewPacket)
    blob = json.dumps(schema)
    for key in ("$schema", "default", "examples"):
        assert f'"{key}"' not in blob


def test_lint_flags_open_objects() -> None:
    violations = lint_provider_safe(
        {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}
    )
    assert any(v.rule == "open-object" for v in violations)


def test_lint_flags_unsupported_keywords() -> None:
    violations = lint_provider_safe(
        {
            "type": "object",
            "properties": {
                "a": {"not": {"type": "string"}},
                "b": {"if": {"type": "string"}, "then": {"type": "string"}},
                "c": {"allOf": [{"type": "object"}, {"type": "object"}]},
            },
        }
    )
    rules = {v.rule for v in violations}
    assert "not" in rules
    assert "conditional" in rules
    assert "allOf-unsupported" in rules


def test_const_becomes_single_member_enum() -> None:
    class Tagged(BaseModel):
        kind: Literal["only"]

    schema = pydantic_provider_schema(Tagged)
    assert "const" not in json.dumps(schema)
    assert schema["properties"]["kind"]["enum"] == ["only"]


def test_discriminated_union_normalizes_oneof_to_anyof() -> None:
    class Cat(BaseModel):
        kind: Literal["cat"]
        meows: int

    class Dog(BaseModel):
        kind: Literal["dog"]
        barks: int

    class Pet(BaseModel):
        animal: Cat | Dog = Field(discriminator="kind")

    schema = pydantic_provider_schema(Pet)
    blob = json.dumps(schema)
    assert "oneOf" not in blob and "discriminator" not in blob
    assert "anyOf" in schema["properties"]["animal"]


def test_recursive_model_raises() -> None:
    class Node(BaseModel):
        value: int
        children: list[Node] = []

    Node.model_rebuild()
    with pytest.raises(ProviderSchemaError) as excinfo:
        pydantic_provider_schema(Node)
    assert any(v.rule == "recursive-ref" for v in excinfo.value.violations)


def test_fields_named_like_schema_keywords_are_preserved() -> None:
    class Keywordish(BaseModel):
        format: str
        default: int
        examples: list[str]

    schema = pydantic_provider_schema(Keywordish)
    assert set(schema["properties"]) == {"format", "default", "examples"}
    assert schema["properties"]["format"]["type"] == "string"
    assert schema["properties"]["default"]["type"] == "integer"


def test_open_map_is_rejected() -> None:
    class HasMap(BaseModel):
        labels: dict[str, int]

    with pytest.raises(ProviderSchemaError) as excinfo:
        pydantic_provider_schema(HasMap)
    assert any(v.rule == "open-map" for v in excinfo.value.violations)


def test_open_object_with_properties_is_rejected_not_silently_closed() -> None:
    # additionalProperties: true alongside properties must raise, not be quietly
    # closed before the linter runs.
    with pytest.raises(ProviderSchemaError) as excinfo:
        to_provider_safe(
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "additionalProperties": True,
            }
        )
    assert any(v.rule == "open-object" for v in excinfo.value.violations)


def test_empty_properties_open_map_is_rejected() -> None:
    with pytest.raises(ProviderSchemaError) as excinfo:
        to_provider_safe(
            {"type": "object", "properties": {}, "additionalProperties": {"type": "integer"}}
        )
    assert any(v.rule == "open-map" for v in excinfo.value.violations)


def test_to_provider_safe_is_idempotent() -> None:
    once = pydantic_provider_schema(ReviewPacket)
    twice = to_provider_safe(once)
    assert once == twice


def test_review_packet_matches_golden() -> None:
    schema = pydantic_provider_schema(ReviewPacket)
    golden_path = GOLDEN_DIR / "review_packet.json"
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    assert schema == expected


def test_nullable_object_input_normalizes_to_golden() -> None:
    # A hand-written/raw `type: ["object", "null"]` (Pydantic emits anyOf, not a
    # nullable object type-array). The profile must still close it: the top-level
    # and the nested nullable object both get additionalProperties:false + full
    # required, optionals wrap `anyOf [_, null]`, and the already-nullable nested
    # object is NOT double-wrapped. The TS SDK reproduces this same golden.
    input_golden = json.loads(
        (GOLDEN_DIR / "nullable_object_input.json").read_text(encoding="utf-8")
    )
    output_golden = json.loads((GOLDEN_DIR / "nullable_object.json").read_text(encoding="utf-8"))
    assert to_provider_safe(input_golden) == output_golden


def test_input_fixture_matches_model_and_normalizes_to_golden() -> None:
    # The committed raw-schema fixture (the `to_provider_safe` INPUT) matches the
    # model; the TS SDK consumes this fixture and must reproduce the output golden.
    input_golden = json.loads((GOLDEN_DIR / "review_packet_input.json").read_text(encoding="utf-8"))
    assert ReviewPacket.model_json_schema() == input_golden

    # Normalizing the raw input reproduces the provider-safe golden — the exact
    # transformation the cross-SDK conformance test pins on both sides.
    output_golden = json.loads((GOLDEN_DIR / "review_packet.json").read_text(encoding="utf-8"))
    assert to_provider_safe(input_golden) == output_golden
