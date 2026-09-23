from __future__ import annotations

from typeflux.execution.provider_identity import provider_identifier


class ExplicitNameProvider:
    provider_name = "custom-provider"


class EmptyNameProvider:
    provider_name = ""


class NamelessProvider:
    pass


class OpenAIProvider:
    pass


class GPT4Provider:
    pass


class Provider:
    pass


def test_provider_identifier_uses_explicit_provider_name() -> None:
    assert provider_identifier(ExplicitNameProvider()) == "custom-provider"


def test_provider_identifier_falls_back_for_empty_provider_name() -> None:
    assert provider_identifier(EmptyNameProvider()) == "empty-name"


def test_provider_identifier_falls_back_for_missing_provider_name() -> None:
    assert provider_identifier(NamelessProvider()) == "nameless"


def test_provider_identifier_preserves_current_acronym_derivation() -> None:
    assert provider_identifier(OpenAIProvider()) == "open-a-i"
    assert provider_identifier(GPT4Provider()) == "g-p-t4"


def test_provider_identifier_falls_back_for_provider_class_name() -> None:
    assert provider_identifier(Provider()) == "provider"
