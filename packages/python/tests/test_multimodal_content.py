"""Portability + safety guarantees for the multimodal content-part model (#115)."""

from __future__ import annotations

from pathlib import Path

from typeflux.core.artifacts import (
    ArtifactGroupPart,
    ArtifactPart,
    ArtifactRef,
    ProviderExtensionPart,
    ResolvedArtifact,
    TextPart,
    content_part_payload,
    render_content_parts,
)
from typeflux.providers import AnthropicProvider, GeminiProvider, OpenAIProvider


def _artifact() -> ResolvedArtifact:
    return ResolvedArtifact(
        group="contracts",
        index=0,
        ref=ArtifactRef(
            source={"type": "local_path", "path": "/secret/home/user/contract.pdf"},
            kind="document",
            media_type="application/pdf",
        ),
        source_kind="local_path",
        kind="document",
        media_type="application/pdf",
        role="contracts",
        sha256="abc123",
        size_bytes=2048,
        local_path=Path("/secret/home/user/contract.pdf"),
    )


def test_safe_summary_carries_provenance_not_payload() -> None:
    # The manifest representation must expose integrity/provenance but never the
    # raw local path, URL, or bytes.
    summary = _artifact().safe_summary()
    assert summary == {
        "group": "contracts",
        "index": 0,
        "source_kind": "local_path",
        "kind": "document",
        "media_type": "application/pdf",
        "role": "contracts",
        "sha256": "abc123",
        "size_bytes": 2048,
    }
    blob = repr(summary)
    assert "/secret/" not in blob
    assert "contract.pdf" not in blob


def test_content_part_payload_references_by_name_no_raw_content() -> None:
    content = (
        TextPart("Engagement {{id}}"),
        ArtifactGroupPart(group="contracts", text="Contracts:"),
        ArtifactPart(artifact="cover", text="Cover page:"),
        ProviderExtensionPart(provider="openai", payload={"type": "input_audio"}),
    )
    payload = content_part_payload(content)
    assert payload == [
        {"type": "text", "text": "Engagement {{id}}"},
        {"type": "artifact_group", "group": "contracts", "text": "Contracts:"},
        {"type": "artifact", "artifact": "cover", "text": "Cover page:"},
        {"type": "provider_extension", "provider": "openai", "payload": {"type": "input_audio"}},
    ]
    # The payload carries logical names + text only — no bytes, paths, or URLs.
    assert "path" not in repr(payload)


def test_render_applies_to_text_parts_only() -> None:
    content = (
        TextPart("Hello {{name}}"),
        ArtifactGroupPart(group="docs", text="For {{name}}:"),
        ProviderExtensionPart(provider="openai", payload={"raw": "{{name}}"}),
    )
    rendered = render_content_parts(content, lambda text: text.replace("{{name}}", "Ada"))
    assert isinstance(rendered, tuple)
    assert rendered[0].text == "Hello Ada"
    assert rendered[1].text == "For Ada:"
    assert rendered[1].group == "docs"
    # Provider-extension payloads are opaque and pass through untouched.
    assert rendered[2].payload == {"raw": "{{name}}"}


def test_string_content_renders_as_plain_text() -> None:
    assert render_content_parts("Hello {{n}}", lambda t: t.replace("{{n}}", "x")) == "Hello x"
    assert content_part_payload("plain") == "plain"


def test_provider_artifact_kind_matrix() -> None:
    # Honest capability sets so preflight rejects what an adapter can't attach.
    assert "image" in OpenAIProvider.supported_artifact_kinds
    assert "provider_file" in OpenAIProvider.supported_artifact_kinds
    assert "provider_file" in AnthropicProvider.supported_artifact_kinds
    # Gemini's messages path attaches images, PDFs, and audio/video inline (#336).
    assert GeminiProvider.supported_artifact_kinds == frozenset(
        {"image", "document", "audio", "video"}
    )
    # OpenAI/Anthropic ingest neither audio nor video; no provider ingests an
    # archive through the messages path (provider-gated divergence, #176).
    for provider in (OpenAIProvider, AnthropicProvider):
        assert {"audio", "video", "archive"}.isdisjoint(provider.supported_artifact_kinds)
    assert "archive" not in GeminiProvider.supported_artifact_kinds
