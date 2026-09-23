from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

import typeflux.observability.semantic as semantic_module
from typeflux.observability.langfuse import (
    LangfuseObservabilityBackend,
    LangfuseTraceWriter,
)
from typeflux.observability.redaction import (
    NoOpRedactor,
    RegexPIIRedactor,
    RegexRedactionRule,
)
from typeflux.observability.semantic import (
    LangfuseAIActivityObserver,
    configure_temporal_langfuse_tracing,
)


class Payload(BaseModel):
    body: str
    nested: dict[str, Any]


class TypedPayload(BaseModel):
    created_at: datetime
    created_on: date
    nested: Payload


def test_default_regex_redactor_masks_common_pii() -> None:
    redactor = RegexPIIRedactor.default()

    result = redactor.redact(
        "email jane.doe@example.com phone 555-123-4567 ssn 123-45-6789 card 4242 4242 4242 4242"
    )

    assert result == (
        "email [REDACTED_EMAIL] phone [REDACTED_PHONE] ssn [REDACTED_SSN] card [REDACTED_CARD]"
    )


def test_default_card_redactor_masks_luhn_valid_cards() -> None:
    redactor = RegexPIIRedactor.default()

    result = redactor.redact(
        "visa 4242 4242 4242 4242 other 4111-1111-1111-1111 amex 378282246310005"
    )

    assert result == "visa [REDACTED_CARD] other [REDACTED_CARD] amex [REDACTED_CARD]"


def test_default_card_redactor_does_not_mask_non_luhn_numeric_ids() -> None:
    redactor = RegexPIIRedactor.default()

    result = redactor.redact(
        "order O-1234567890123 account 123456789012345 trace 20260528000012345"
    )

    assert result == "order O-1234567890123 account 123456789012345 trace 20260528000012345"


def test_card_redactor_respects_excluded_paths() -> None:
    redactor = RegexPIIRedactor.default(exclude_paths=("metadata.order_id",))
    payload = {
        "metadata": {
            "order_id": "4242 4242 4242 4242",
            "billing_card": "4242 4242 4242 4242",
        }
    }

    result = redactor.redact(payload)

    assert result["metadata"]["order_id"] == "4242 4242 4242 4242"
    assert result["metadata"]["billing_card"] == "[REDACTED_CARD]"


def test_default_phone_redactor_does_not_mask_bare_numeric_ids() -> None:
    redactor = RegexPIIRedactor.default()

    result = redactor.redact("workflow_id support-triage-yaml-manifest-1778708218")

    assert result == "workflow_id support-triage-yaml-manifest-1778708218"


def test_default_redactor_preserves_typeflux_metadata_paths() -> None:
    redactor = RegexPIIRedactor.default()
    payload = {
        "typeflux": {
            "execution_manifest": {
                "workflow_id": "support-triage-yaml-manifest-555-123-4567",
                "workflow_contract_hash": "workflow-contract-555-123-4567",
                "manifest_hash": "123-45-6789",
                "activities": [
                    {
                        "activity_manifest_hash": "4242 4242 4242 4242",
                        "definition_source": {
                            "module": "support.555-123-4567.activities",
                        },
                        "input_schema": {
                            "hash": "cb4e2958fc6d9d856f440ef1e42499ad172a82894bc062345678901234"
                        },
                        "output_schema": {"hash": "1234567890123"},
                        "start_to_close_timeout_seconds": 600.0,
                    }
                ],
            },
            "activity_execution_manifest": {
                "prompt_messages_hash": "555-123-4567",
                "rendered_messages_hash": "4242 4242 4242 4242",
                "start_to_close_timeout_seconds": 600.0,
                "definition_source": {
                    "module": "support.555-123-4567.activities",
                },
            },
            "runtime_placement": {
                "platform": "kubernetes",
                "kubernetes": {
                    "namespace": "typeflux-smoke",
                    "pod_name": "worker-555-123-4567",
                    "pod_uid": "pod-uid-123-45-6789",
                    "node_name": "node-555-123-4567",
                    "service_account": "typeflux-worker",
                    "deployment_name": "worker-555-123-4567",
                    "worker_name": "worker-555-123-4567",
                    "labels": {"owner": "ops-555-123-4567"},
                },
                "container_image": "typeflux-worker:555-123-4567",
                "service_account_token": "token-555-123-4567",
            },
            "lifecycle": {
                "state": "waiting_for_review",
                "current_step": "package_for_review",
                "completed_units": 2,
                "total_units": 3,
                "cancellation_requested": False,
                "waiting_checkpoint": "package_for_review",
                "terminal_status": None,
            },
            "registry": {
                "langfuse": {
                    "prompt_config": {
                        "typeflux": {
                            "contracts": {
                                "input_schema_hash": "cb4e2958fc6d9d856f440ef1e42499ad172a82894bc062345678901234",
                                "output_schema_hash": "1234567890123",
                            }
                        }
                    }
                }
            },
        },
        "langfuse": {
            "prompt_config": {
                "typeflux": {
                    "contracts": {
                        "input_schema_hash": "cb4e2958fc6d9d856f440ef1e42499ad172a82894bc062345678901234",
                        "output_schema_hash": "1234567890123",
                    }
                }
            }
        },
        "customer": {
            "phone": "555-123-4567",
            "email": "jane.doe@example.com",
        },
    }

    result = redactor.redact(payload)

    assert result["typeflux"]["execution_manifest"]["workflow_id"] == (
        "support-triage-yaml-manifest-555-123-4567"
    )
    assert result["typeflux"]["execution_manifest"]["workflow_contract_hash"] == (
        "workflow-contract-555-123-4567"
    )
    assert result["typeflux"]["execution_manifest"]["manifest_hash"] == "123-45-6789"
    assert result["typeflux"]["execution_manifest"]["activities"][0]["activity_manifest_hash"] == (
        "4242 4242 4242 4242"
    )
    assert result["typeflux"]["execution_manifest"]["activities"][0]["input_schema"]["hash"] == (
        "cb4e2958fc6d9d856f440ef1e42499ad172a82894bc062345678901234"
    )
    assert result["typeflux"]["execution_manifest"]["activities"][0]["output_schema"]["hash"] == (
        "1234567890123"
    )
    assert result["typeflux"]["execution_manifest"]["activities"][0]["definition_source"][
        "module"
    ] == ("support.555-123-4567.activities")
    assert (
        result["typeflux"]["execution_manifest"]["activities"][0]["start_to_close_timeout_seconds"]
        == 600.0
    )
    assert (
        result["typeflux"]["activity_execution_manifest"]["prompt_messages_hash"] == "555-123-4567"
    )
    assert result["typeflux"]["activity_execution_manifest"]["rendered_messages_hash"] == (
        "4242 4242 4242 4242"
    )
    assert result["typeflux"]["activity_execution_manifest"]["definition_source"]["module"] == (
        "support.555-123-4567.activities"
    )
    assert (
        result["typeflux"]["activity_execution_manifest"]["start_to_close_timeout_seconds"] == 600.0
    )
    placement = result["typeflux"]["runtime_placement"]
    assert placement["platform"] == "kubernetes"
    assert placement["kubernetes"]["namespace"] == "typeflux-smoke"
    assert placement["kubernetes"]["pod_name"] == "worker-555-123-4567"
    assert placement["kubernetes"]["pod_uid"] == "pod-uid-123-45-6789"
    assert placement["kubernetes"]["node_name"] == "node-555-123-4567"
    assert placement["kubernetes"]["service_account"] == "typeflux-worker"
    assert placement["kubernetes"]["deployment_name"] == "worker-555-123-4567"
    assert placement["kubernetes"]["worker_name"] == "worker-555-123-4567"
    assert placement["container_image"] == "typeflux-worker:555-123-4567"
    assert placement["kubernetes"]["labels"]["owner"] == "ops-[REDACTED_PHONE]"
    assert placement["service_account_token"] == "token-[REDACTED_PHONE]"
    assert result["typeflux"]["lifecycle"]["waiting_checkpoint"] == "package_for_review"
    assert result["typeflux"]["registry"]["langfuse"]["prompt_config"]["typeflux"]["contracts"] == {
        "input_schema_hash": "cb4e2958fc6d9d856f440ef1e42499ad172a82894bc062345678901234",
        "output_schema_hash": "1234567890123",
    }
    assert result["langfuse"]["prompt_config"]["typeflux"]["contracts"] == {
        "input_schema_hash": "cb4e2958fc6d9d856f440ef1e42499ad172a82894bc062345678901234",
        "output_schema_hash": "1234567890123",
    }
    assert result["customer"] == {
        "phone": "[REDACTED_PHONE]",
        "email": "[REDACTED_EMAIL]",
    }


def test_redactor_supports_custom_excluded_paths() -> None:
    redactor = RegexPIIRedactor.default(exclude_paths=("metadata.ticket_id", "metadata.custom.*"))
    payload = {
        "metadata": {
            "ticket_id": "ticket-555-123-4567",
            "custom": {"case_id": "case-123-45-6789"},
            "owner_email": "jane.doe@example.com",
        }
    }

    result = redactor.redact(payload)

    assert result["metadata"]["ticket_id"] == "ticket-555-123-4567"
    assert result["metadata"]["custom"]["case_id"] == "case-123-45-6789"
    assert result["metadata"]["owner_email"] == "[REDACTED_EMAIL]"


def test_redactor_can_disable_typeflux_metadata_preservation() -> None:
    redactor = RegexPIIRedactor.default(preserve_typeflux_metadata=False)

    result = redactor.redact(
        {"typeflux": {"execution_manifest": {"workflow_id": "support-555-123-4567"}}}
    )

    assert result["typeflux"]["execution_manifest"]["workflow_id"] == "support-[REDACTED_PHONE]"


def test_default_regex_redactor_can_disable_rule_groups() -> None:
    redactor = RegexPIIRedactor.default(
        emails=False,
        phones=True,
        ssn=False,
        credit_cards=False,
    )

    result = redactor.redact(
        "email jane.doe@example.com phone 555-123-4567 ssn 123-45-6789 card 4242 4242 4242 4242"
    )

    assert result == (
        "email jane.doe@example.com phone [REDACTED_PHONE] ssn 123-45-6789 card 4242 4242 4242 4242"
    )


def _custom_only_redactor(rule: RegexRedactionRule) -> RegexPIIRedactor:
    return RegexPIIRedactor.default(
        emails=False, phones=False, ssn=False, credit_cards=False, custom_rules=(rule,)
    )


def test_custom_rule_replacement_is_literal_not_backreference() -> None:
    # A YAML custom rule's `replacement` must be LITERAL: `re.sub` must not interpret `\1` as
    # a backreference, which would re-inject the captured PII into the "redacted" output.
    import re as _re

    rule = RegexRedactionRule(
        name="case", pattern=_re.compile(r"CASE-(\d{6})"), replacement=r"[CASE \1]"
    )
    result = _custom_only_redactor(rule).redact("ref CASE-123456 end")

    assert result == r"ref [CASE \1] end"  # `\1` stays verbatim
    assert "123456" not in result  # captured PII is NOT re-injected


def test_custom_rule_replacement_with_backslash_does_not_crash() -> None:
    # A stray backslash (`\C`) in a STRING repl raises re.error at trace emission; the literal
    # function repl treats it verbatim so redaction never crashes mid-emit.
    import re as _re

    rule = RegexRedactionRule(name="c", pattern=_re.compile(r"secret"), replacement=r"\C[X]")
    result = _custom_only_redactor(rule).redact("a secret b")

    assert result == r"a \C[X] b"


def test_custom_rule_replacement_cross_edition_parity_literal() -> None:
    # PARITY with the TS suite (redaction.test.ts, "treats a custom replacement LITERALLY"):
    # identical fixture, identical output. A replacement containing BOTH `\1` and `$1` appears
    # VERBATIM in both editions (Python literal re.sub function repl; TS replacer function,
    # never `$`-interpreted). Keep the fixture values in sync across editions.
    import re as _re

    rule = RegexRedactionRule(
        name="case", pattern=_re.compile(r"CASE-(\d{6})"), replacement=r"[C \1 $1]"
    )
    result = _custom_only_redactor(rule).redact("ref CASE-123456 end")

    assert result == r"ref [C \1 $1] end"
    assert "123456" not in result


def test_redactor_walks_nested_payloads_without_mutating_original() -> None:
    redactor = RegexPIIRedactor.default()
    payload = Payload(
        body="Contact jane.doe@example.com",
        nested={
            "phones": ["555-123-4567"],
            "tuple": ("123-45-6789",),
            "count": 2,
        },
    )

    result = redactor.redact(payload)

    assert result == {
        "body": "Contact [REDACTED_EMAIL]",
        "nested": {
            "phones": ["[REDACTED_PHONE]"],
            "tuple": ("[REDACTED_SSN]",),
            "count": 2,
        },
    }
    assert payload.body == "Contact jane.doe@example.com"
    assert payload.nested["phones"] == ["555-123-4567"]
    assert payload.nested["tuple"] == ("123-45-6789",)


def test_redactor_preserves_python_container_and_value_types() -> None:
    redactor = RegexPIIRedactor.default()
    created_at = datetime(2026, 5, 29, 12, 30, tzinfo=UTC)
    created_on = date(2026, 5, 29)
    payload = TypedPayload(
        created_at=created_at,
        created_on=created_on,
        nested=Payload(
            body="Contact jane.doe@example.com",
            nested={
                "tuple": ("555-123-4567", "plain"),
                "set": {"jane.doe@example.com", "plain"},
                "frozenset": frozenset({"123-45-6789", "plain"}),
            },
        ),
    )

    result = redactor.redact(payload)

    assert isinstance(result, dict)
    assert result["created_at"] is created_at
    assert result["created_on"] is created_on
    assert result["nested"]["body"] == "Contact [REDACTED_EMAIL]"

    redacted_tuple = result["nested"]["nested"]["tuple"]
    assert isinstance(redacted_tuple, tuple)
    assert redacted_tuple == ("[REDACTED_PHONE]", "plain")

    redacted_set = result["nested"]["nested"]["set"]
    assert isinstance(redacted_set, set)
    assert redacted_set == {"[REDACTED_EMAIL]", "plain"}

    redacted_frozenset = result["nested"]["nested"]["frozenset"]
    assert isinstance(redacted_frozenset, frozenset)
    assert redacted_frozenset == frozenset({"[REDACTED_SSN]", "plain"})


def test_noop_redactor_leaves_payload_unchanged() -> None:
    payload = {"email": "jane.doe@example.com"}

    assert NoOpRedactor().redact(payload) is payload


def test_langfuse_activity_observer_passes_default_mask(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=FakeLangfuse))

    LangfuseAIActivityObserver(host="https://cloud.langfuse.com")

    mask = calls[0]["mask"]
    assert mask("Email jane.doe@example.com") == "Email [REDACTED_EMAIL]"


def test_langfuse_mask_skips_already_redacted_provider_metadata() -> None:
    class CountingRedactor:
        def __init__(self) -> None:
            self.calls = 0

        def redact(self, data: Any, **kwargs: Any) -> Any:
            self.calls += 1
            if isinstance(data, dict):
                return {**data, "owner": f"{data['owner']}#{self.calls}"}
            return f"{data}#{self.calls}"

    redactor = CountingRedactor()
    observer = LangfuseAIActivityObserver(client=_FakeLangfuseClient(), redactor=redactor)
    provider_metadata = observer.redact_metadata({"owner": "jane"})

    assert provider_metadata == {"owner": "jane#1"}
    assert redactor.calls == 1

    mask = semantic_module._mask(redactor)

    assert mask(provider_metadata) == {"owner": "jane#1"}
    assert redactor.calls == 1
    assert mask({"owner": "jane"}) == {"owner": "jane#2"}
    assert redactor.calls == 2


def test_trace_writer_marks_redacted_provider_metadata_for_langfuse_mask() -> None:
    class CountingRedactor:
        def __init__(self) -> None:
            self.calls = 0

        def redact(self, data: Any, **kwargs: Any) -> Any:
            self.calls += 1
            if isinstance(data, dict):
                return {**data, "owner": f"{data['owner']}#{self.calls}"}
            return data

    redactor = CountingRedactor()
    writer = LangfuseTraceWriter(client=_FakeLangfuseClient(), redactor=redactor)
    provider_metadata = writer.redact_metadata({"owner": "jane"})

    assert provider_metadata == {"owner": "jane#1"}
    assert semantic_module._mask(redactor)(provider_metadata) == {"owner": "jane#1"}
    assert redactor.calls == 1


def test_non_dict_redacted_metadata_fallback_is_not_marked_as_redacted() -> None:
    class NonDictThenCountingRedactor:
        def __init__(self) -> None:
            self.calls = 0

        def redact(self, data: Any, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                return "not metadata"
            if isinstance(data, dict):
                return {**data, "owner": f"{data['owner']}#{self.calls}"}
            return data

    redactor = NonDictThenCountingRedactor()
    observer = LangfuseAIActivityObserver(client=_FakeLangfuseClient(), redactor=redactor)
    provider_metadata = observer.redact_metadata({"owner": "jane"})

    assert provider_metadata == {"owner": "jane"}
    assert semantic_module._mask(redactor)(provider_metadata) == {"owner": "jane#2"}
    assert redactor.calls == 2


def test_configure_temporal_langfuse_tracing_passes_default_mask(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    tracer_provider = _FakeTracerProvider()
    plugin_calls: list[dict[str, Any]] = []

    class FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    _install_fake_tracing_modules(
        monkeypatch, tracer_provider=tracer_provider, plugin_calls=plugin_calls
    )
    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=FakeLangfuse))
    monkeypatch.setitem(
        sys.modules,
        "langfuse.span_filter",
        types.SimpleNamespace(is_default_export_span=lambda span: True),
    )

    tracing = configure_temporal_langfuse_tracing()
    plugin, client = tracing

    mask = calls[0]["mask"]
    assert mask({"phone": "555-123-4567"}) == {"phone": "[REDACTED_PHONE]"}
    assert calls[0]["tracer_provider"] is tracer_provider
    assert tracing.tracer_provider is tracer_provider
    assert tracing.plugin is plugin
    assert tracing.client is client
    assert plugin_calls == [{"add_temporal_spans": True}]


def test_configure_temporal_langfuse_tracing_accepts_custom_redactor(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    tracer_provider = _FakeTracerProvider()
    plugin_calls: list[dict[str, Any]] = []

    class FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    class CustomRedactor:
        def redact(self, data: Any, **kwargs: Any) -> Any:
            return "CUSTOM"

    _install_fake_tracing_modules(
        monkeypatch, tracer_provider=tracer_provider, plugin_calls=plugin_calls
    )
    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=FakeLangfuse))
    monkeypatch.setitem(
        sys.modules,
        "langfuse.span_filter",
        types.SimpleNamespace(is_default_export_span=lambda span: True),
    )

    configure_temporal_langfuse_tracing(redactor=CustomRedactor())

    assert calls[0]["mask"]("anything") == "CUSTOM"
    assert calls[0]["tracer_provider"] is tracer_provider


def test_registered_langfuse_client_shares_redactor_without_client_attribute(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    tracer_provider = _FakeTracerProvider()
    plugin_calls: list[dict[str, Any]] = []

    class CustomRedactor:
        def redact(self, data: Any, **kwargs: Any) -> Any:
            return "CUSTOM"

    class StrictLangfuseClient:
        __slots__ = ("kwargs", "__weakref__")

        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)
            self.kwargs = kwargs

    redactor = CustomRedactor()
    _install_fake_tracing_modules(
        monkeypatch, tracer_provider=tracer_provider, plugin_calls=plugin_calls
    )
    monkeypatch.setitem(
        sys.modules,
        "langfuse",
        types.SimpleNamespace(Langfuse=StrictLangfuseClient),
    )
    monkeypatch.setitem(
        sys.modules,
        "langfuse.span_filter",
        types.SimpleNamespace(is_default_export_span=lambda span: True),
    )

    tracing = configure_temporal_langfuse_tracing(redactor=redactor)
    observer = LangfuseAIActivityObserver(client=tracing.client)
    writer = LangfuseTraceWriter(client=tracing.client)

    assert observer.redactor is redactor
    assert writer.redactor is redactor
    assert calls[0]["mask"]("anything") == "CUSTOM"
    assert not hasattr(tracing.client, "_typeflux_redactor")


def test_byo_langfuse_backend_uses_custom_redactor_for_writer_and_observer() -> None:
    class CustomRedactor:
        def redact(self, data: Any, **kwargs: Any) -> Any:
            if isinstance(data, dict):
                return {**data, "owner": "CUSTOM"}
            return "CUSTOM"

    backend = LangfuseObservabilityBackend.from_client(
        _FakeLangfuseClient(),
        redactor=CustomRedactor(),
    )
    observer = backend.writer.create_activity_observer()

    assert backend.writer.redact_metadata({"owner": "jane.doe@example.com"}) == {"owner": "CUSTOM"}
    assert observer.redact_metadata({"owner": "jane.doe@example.com"}) == {"owner": "CUSTOM"}


class _FakeTracerProvider:
    def force_flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _FakeLangfuseClient:
    def flush(self) -> None:
        return None


def _install_fake_tracing_modules(
    monkeypatch,
    *,
    tracer_provider: _FakeTracerProvider,
    plugin_calls: list[dict[str, Any]],
) -> None:
    class FakeOpenTelemetryPlugin:
        def __init__(self, **kwargs: Any) -> None:
            plugin_calls.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "opentelemetry",
        types.SimpleNamespace(
            trace=types.SimpleNamespace(set_tracer_provider=lambda provider: None)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "temporalio.contrib.opentelemetry",
        types.SimpleNamespace(
            OpenTelemetryPlugin=FakeOpenTelemetryPlugin,
            create_tracer_provider=lambda: tracer_provider,
        ),
    )
