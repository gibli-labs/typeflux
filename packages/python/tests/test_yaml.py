from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from textwrap import dedent, indent
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactGroupPart, TextPart
from typeflux.core.contracts import (
    AIActivity,
    ChatMessage,
    PromptRef,
    ResolvedPrompt,
    ReviewCommand,
    TemporalActivityDescriptor,
    WorkflowLifecycleStatus,
)
from typeflux.core.errors import LifecycleBindingError
from typeflux.execution.executor import execute_ai_activity
from typeflux.execution.observer import NoOpObserver
from typeflux.lifecycle import (
    export_workflow_lifecycle_audit,
    workflow_lifecycle_audit_event_from_history_event,
)
from typeflux.metadata import RuntimePlacementContributor
from typeflux.observability.redaction import RegexPIIRedactor
from typeflux.observability.semantic import observe_workflow_invocation
from typeflux.project.policy import ComposedProjectPolicy
from typeflux.project.policy_enforcement import RuntimePolicyGuard
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider
from typeflux.yaml import (
    build_runtime,
    collect_activities,
    create_workflow,
    load_yaml_spec,
    validate_unique_yaml_workflow_names,
)
from typeflux.yaml import run as yaml_run
from typeflux.yaml import submit as yaml_submit
from typeflux.yaml import workflow as workflow_module
from typeflux.yaml.imports import import_object, import_type_ref
from typeflux.yaml.runtime import (
    ObservabilityCompositionError,
    TypefluxYamlRuntime,
    _build_artifact_policy,
    _build_observability,
    _build_provider,
    _build_registry,
    _build_temporal_tls_config,
    _connect_client,
    _LabelOverrideRegistry,
)
from typeflux.yaml.spec import TemporalTLSConfigSpec


def test_yaml_env_interpolation_supports_defaults_and_required_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: demo
        task_queue: ${TASK_QUEUE:-default-queue}
        runtime:
          temporal:
            address: ${TEMPORAL_ADDRESS}
          registry:
            type: inline
            prompts: {}
          provider:
            type: fake
        activities:
          modules: [activities]
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

    spec = load_yaml_spec(path)

    assert spec.task_queue == "default-queue"
    assert spec.runtime.temporal.address == "temporal:7233"


def _hermetic_yaml(tmp_path: Path, queue_expr: str) -> Path:
    return _write_yaml(
        tmp_path,
        f"""
        project: demo
        name: demo
        task_queue: {queue_expr}
        runtime:
          temporal: {{}}
          registry: {{ type: inline, prompts: {{}} }}
          provider: {{ type: fake }}
        activities:
          modules: [activities]
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )


def test_load_yaml_spec_env_injection_is_hermetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #760: an injected `env` mapping is the ONLY interpolation source — the poison in
    # the real process environment must never reach the resolved bytes, and a var present
    # only in the injected map resolves.
    monkeypatch.setenv("TF760_QUEUE", "POISON")
    environ_before = dict(os.environ)

    path = _hermetic_yaml(tmp_path, "${TF760_QUEUE}")
    spec = load_yaml_spec(path, load_dotenv=False, env={"TF760_QUEUE": "hermetic"})

    assert spec.task_queue == "hermetic"
    # STRICT no-mutation: the ENTIRE process environment (key set + values) is
    # byte-identical — a hermetic load may not add, remove, or rewrite anything.
    assert dict(os.environ) == environ_before


def test_load_yaml_spec_env_injection_skips_dotenv_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #760 fix round: a hermetic call must not let a cwd `.env` mutate os.environ as a
    # side effect (load_dotenv defaults OFF when `env` is injected), and the dotenv
    # value must not reach interpolation either.
    monkeypatch.delenv("TYPEFLUX_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TF760_DOTENV_POISON=from-dotenv\n", encoding="utf-8")
    environ_before = dict(os.environ)

    path = _hermetic_yaml(tmp_path, "${TF760_DOTENV_POISON}")
    with pytest.raises(KeyError, match="missing environment variable: TF760_DOTENV_POISON"):
        load_yaml_spec(path, env={})

    assert "TF760_DOTENV_POISON" not in os.environ
    assert dict(os.environ) == environ_before


def test_load_yaml_spec_explicit_dotenv_with_env_joins_the_map_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An EXPLICIT load_dotenv=True with `env` supplied is honored: dotenv values join
    # the interpolation map (injected keys win), and os.environ is never touched.
    monkeypatch.delenv("TYPEFLUX_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TF760_DOTENV_ONLY=from-dotenv\nTF760_QUEUE=dotenv-loses\n", encoding="utf-8"
    )
    environ_before = dict(os.environ)

    path = _hermetic_yaml(tmp_path, "${TF760_QUEUE}-${TF760_DOTENV_ONLY}")
    spec = load_yaml_spec(path, load_dotenv=True, env={"TF760_QUEUE": "injected-wins"})

    assert spec.task_queue == "injected-wins-from-dotenv"
    assert dict(os.environ) == environ_before


def test_load_yaml_spec_env_injection_has_no_process_env_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A key present in the shell but ABSENT from the injected map, with no default, must
    # error exactly as an unset shell variable would — no silent os.environ fallback.
    monkeypatch.setenv("TF760_MISSING", "POISON")
    path = _hermetic_yaml(tmp_path, "${TF760_MISSING}")

    with pytest.raises(KeyError, match="missing environment variable: TF760_MISSING"):
        load_yaml_spec(path, load_dotenv=False, env={})


def test_load_yaml_spec_env_injection_treats_empty_string_as_set(
    tmp_path: Path,
) -> None:
    # An injected empty-string value is SET, not missing: `${VAR:-fallback}` with VAR=""
    # renders "" (the `x or default` trap — a truthiness test would wrongly pick fallback).
    path = _hermetic_yaml(tmp_path, "${TF760_QUEUE:-fallback}")

    spec = load_yaml_spec(path, load_dotenv=False, env={"TF760_QUEUE": ""})

    assert spec.task_queue == ""


def test_yaml_temporal_env_interpolation_supports_tls_and_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TEMPORAL_ADDRESS", "namespace.tmprl.cloud:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "namespace")
    monkeypatch.setenv("TEMPORAL_TLS", "true")
    monkeypatch.setenv("TEMPORAL_API_KEY", "cloud-api-key")
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: ${TEMPORAL_ADDRESS:-localhost:7233}
          namespace: ${TEMPORAL_NAMESPACE:-default}
          tls: ${TEMPORAL_TLS:-false}
          api_key: ${TEMPORAL_API_KEY:-}
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )

    spec = load_yaml_spec(path)

    assert spec.runtime.temporal.address == "namespace.tmprl.cloud:7233"
    assert spec.runtime.temporal.namespace == "namespace"
    assert spec.runtime.temporal.tls is True
    assert spec.runtime.temporal.api_key == "cloud-api-key"


def test_yaml_temporal_blank_api_key_normalizes_to_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          tls: false
          api_key: ${TEMPORAL_API_KEY:-}
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )

    spec = load_yaml_spec(path)

    assert spec.runtime.temporal.api_key is None


def test_yaml_temporal_api_key_requires_tls_enabled(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          tls: false
          api_key: cloud-api-key
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )

    with pytest.raises(ValueError, match="api_key requires runtime.temporal.tls"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_temporal_api_key_tls_guard_is_load_time_for_literals_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A value_from reference may be unconfigured in local profiles, so it
    # loads with TLS disabled; the configured case fails at client connect.
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    reference_path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          tls: false
          api_key:
            value_from:
              env: TEMPORAL_API_KEY
              required: false
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )
    spec = load_yaml_spec(reference_path)
    assert spec.runtime.temporal.api_key is not None

    # A literal key with TLS disabled is statically wrong and fails at load.
    literal_path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          tls: false
          api_key: literal-temporal-key
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )
    with pytest.raises(ValueError, match="api_key requires runtime.temporal.tls"):
        load_yaml_spec(literal_path)


def test_yaml_load_rejects_duplicate_mapping_keys(tmp_path: Path) -> None:
    path = tmp_path / "typeflux.yaml"
    path.write_text(
        dedent(
            """
            project: yaml_demo_project
            name: demo_yaml
            name: shadowed_yaml
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts: {}
              provider:
                type: fake
            activities:
              modules:
                - activities
            workflow:
              name: DemoYamlWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="duplicate key 'name'"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_load_rejects_nested_duplicate_keys(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
          address: shadow:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        """,
    )

    with pytest.raises(Exception, match="duplicate key 'address'"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_load_bounds_nesting_depth() -> None:
    """A deeply nested document is a normal parse error (YAMLError), never a
    RecursionError escaping every caller's error handling (finder, #602)."""
    import yaml as pyyaml

    from typeflux.yaml.loader import strict_safe_load

    deep = "[" * 5000 + "]" * 5000
    with pytest.raises(pyyaml.YAMLError, match="nesting-depth limit"):
        strict_safe_load(deep)


def test_yaml_scalars_resolve_as_12_core_schema() -> None:
    """#602: scalar resolution is the YAML 1.2 CORE schema, matching the TS SDK.

    PyYAML's inherited 1.1 resolvers turned `on`/`yes` into booleans, `1:30`
    into sexagesimal 90, and dates into datetime objects — values the TS
    parser reads as strings, so the same file meant different things per SDK
    (observable through the cross-SDK profile content_hash, #570).
    """
    from typeflux.yaml.loader import strict_safe_load

    doc = strict_safe_load(
        """
        one_one_bools: [on, yes, off, no, y, n]
        core_bools: [true, True, FALSE]
        sexagesimal: 1:30
        date: 2024-01-01
        underscored: 1_000
        octal_new: 0o17
        octal_leading_zero: 019
        hexadecimal: 0x1A
        floats: [3.14, .inf, 2.]
        nulls: [~, null, Null]
        plain: hello
        """
    )
    # The 1.1-only spellings are STRINGS now — write true/false for booleans.
    assert doc["one_one_bools"] == ["on", "yes", "off", "no", "y", "n"]
    assert doc["core_bools"] == [True, True, False]
    assert doc["sexagesimal"] == "1:30"
    assert doc["date"] == "2024-01-01"
    assert doc["underscored"] == "1_000"
    # 1.2 core gains 0o octals; a leading-zero decimal is DECIMAL (not 1.1 octal).
    assert doc["octal_new"] == 15
    assert doc["octal_leading_zero"] == 19
    assert doc["hexadecimal"] == 26
    assert doc["floats"][0] == 3.14
    assert doc["floats"][1] == float("inf")
    assert doc["floats"][2] == 2.0
    assert doc["nulls"] == [None, None, None]
    assert doc["plain"] == "hello"


def test_yaml_merge_keys_are_not_false_positived_as_duplicates() -> None:
    from typeflux.yaml.loader import strict_safe_load

    loaded = strict_safe_load(
        dedent(
            """
            defaults: &defaults
              timeout: 30
              retries: 2
            service:
              <<: *defaults
              retries: 5
            """
        )
    )

    assert loaded["service"] == {"timeout": 30, "retries": 5}


def test_yaml_load_warns_on_literal_credentials(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
          tls: true
          api_key: literal-temporal-key
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
          api_key: sk-literal-provider-key
        """,
    )

    with caplog.at_level(logging.WARNING, logger="typeflux.yaml.loader"):
        load_yaml_spec(path, load_dotenv=False)

    warnings = [record.getMessage() for record in caplog.records]
    assert any("runtime.temporal.api_key" in message for message in warnings)
    assert any("runtime.provider.api_key" in message for message in warnings)
    # The warning names the field, never the credential value.
    assert all("sk-literal-provider-key" not in message for message in warnings)


def test_yaml_load_is_silent_for_references_and_empty_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
          api_key: ${TEMPORAL_API_KEY:-}
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
          api_key:
            value_from:
              env: FAKE_API_KEY
              required: false
        """,
    )

    with caplog.at_level(logging.WARNING, logger="typeflux.yaml.loader"):
        load_yaml_spec(path, load_dotenv=False)

    assert not [record for record in caplog.records if "literal credential" in record.getMessage()]


def test_yaml_load_warns_on_literal_observability_credentials(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """codex round 3: a literal observability credential earns the same loader warning as a
    literal api_key — the secret contract's warning surface covers every credential slot."""
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        observability:
          type: langfuse
          langfuse:
            public_key: pk-literal
            secret_key: sk-live-hardcoded
        """,
    )

    with caplog.at_level(logging.WARNING, logger="typeflux.yaml.loader"):
        load_yaml_spec(path, load_dotenv=False)

    warned = [r.getMessage() for r in caplog.records if "literal credential" in r.getMessage()]
    assert any("runtime.observability.langfuse.public_key" in m for m in warned)
    assert any("runtime.observability.langfuse.secret_key" in m for m in warned)
    assert not any("sk-live-hardcoded" in m for m in warned)


def test_secret_reference_records_surface_literals_without_values(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
              tls: true
              api_key: literal-temporal-key
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
              api_key:
                value_from:
                  env: FAKE_API_KEY
                  required: false
            """,
        ),
        load_dotenv=False,
    )

    from typeflux.yaml.secrets import secret_reference_records

    records = {record.runtime_path: record for record in secret_reference_records(spec)}
    literal = records["runtime.temporal.api_key"]
    assert literal.source_kind == "literal"
    assert literal.source_name == ""
    assert literal.configured is True
    assert "literal-temporal-key" not in str(records)
    reference = records["runtime.provider.api_key"]
    assert reference.source_kind == "env"
    assert reference.source_name == "FAKE_API_KEY"


def test_yaml_secret_reference_schema_rejects_ambiguous_source(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          api_key:
            value_from:
              env: OPENAI_API_KEY
              file: /run/secrets/openai-api-key
        """,
    )

    with pytest.raises(ValueError, match="exactly one of env or file"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_temporal_structured_tls_config_builds_sdk_tls_config(tmp_path: Path) -> None:
    root_ca = tmp_path / "root-ca.pem"
    client_cert = tmp_path / "client.pem"
    client_key = tmp_path / "client.key"
    root_ca.write_bytes(b"root-ca")
    client_cert.write_bytes(b"client-cert")
    client_key.write_bytes(b"client-key")
    path = _write_demo_yaml(
        tmp_path,
        runtime=f"""
        temporal:
          tls:
            server_root_ca_cert_file: {root_ca}
            domain: temporal.example.com
            client_cert_file: {client_cert}
            client_private_key_file: {client_key}
        registry:
          type: inline
          prompts: {{}}
        provider:
          type: fake
        """,
    )

    spec = load_yaml_spec(path, load_dotenv=False)

    assert isinstance(spec.runtime.temporal.tls, TemporalTLSConfigSpec)
    tls_config = _build_temporal_tls_config(spec.runtime.temporal.tls)
    assert tls_config.server_root_ca_cert == b"root-ca"
    assert tls_config.domain == "temporal.example.com"
    assert tls_config.client_cert == b"client-cert"
    assert tls_config.client_private_key == b"client-key"


def test_yaml_temporal_structured_tls_supports_secret_file_references(tmp_path: Path) -> None:
    root_ca = tmp_path / "root-ca.pem"
    client_cert = tmp_path / "client.pem"
    client_key = tmp_path / "client.key"
    root_ca.write_bytes(b"root-ca")
    client_cert.write_bytes(b"client-cert")
    client_key.write_bytes(b"client-key")
    path = _write_demo_yaml(
        tmp_path,
        runtime=f"""
        temporal:
          tls:
            server_root_ca_cert:
              value_from:
                file: {root_ca}
            domain: temporal.example.com
            client_cert:
              value_from:
                file: {client_cert}
            client_private_key:
              value_from:
                file: {client_key}
        registry:
          type: inline
          prompts: {{}}
        provider:
          type: fake
        """,
    )

    spec = load_yaml_spec(path)

    assert isinstance(spec.runtime.temporal.tls, TemporalTLSConfigSpec)
    tls_config = _build_temporal_tls_config(spec.runtime.temporal.tls)
    assert tls_config.server_root_ca_cert == b"root-ca"
    assert tls_config.domain == "temporal.example.com"
    assert tls_config.client_cert == b"client-cert"
    assert tls_config.client_private_key == b"client-key"


def test_yaml_temporal_structured_tls_rejects_old_and_new_ca_fields(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          tls:
            server_root_ca_cert_file: /tmp/ca.pem
            server_root_ca_cert:
              value_from:
                file: /run/secrets/ca.pem
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )

    with pytest.raises(ValueError, match="cannot both be configured"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_temporal_structured_tls_rejects_unpaired_client_cert(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          tls:
            client_cert_file: /tmp/client.pem
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )

    with pytest.raises(ValueError, match="must be configured together"):
        load_yaml_spec(path)


def test_yaml_temporal_structured_tls_missing_file_fails_clearly(tmp_path: Path) -> None:
    tls = TemporalTLSConfigSpec(server_root_ca_cert_file=str(tmp_path / "missing-ca.pem"))

    with pytest.raises(ValueError, match="server_root_ca_cert_file"):
        _build_temporal_tls_config(tls)


def test_yaml_gemini_vertex_config_builds_vertex_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #332: a `vertex:` block selects Vertex AI (ADC, no api_key) and wires
    # project/location through to GeminiProvider.
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: gemini
          model: gemini-2.5-flash
          vertex:
            project: my-gcp-project
            location: us-central1
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeGeminiProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr("typeflux.yaml.runtime.GeminiProvider", FakeGeminiProvider)
    _build_provider(spec, enable_langfuse=False)

    assert calls["kwargs"]["use_vertex"] is True
    assert calls["kwargs"]["project"] == "my-gcp-project"
    assert calls["kwargs"]["location"] == "us-central1"
    assert "api_key" not in calls["kwargs"]  # Vertex uses ADC, not a key


def test_yaml_gemini_without_vertex_uses_developer_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No vertex block → Developer-API path (api_key, no use_vertex).
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: gemini
          model: gemini-2.5-flash
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeGeminiProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr("typeflux.yaml.runtime.GeminiProvider", FakeGeminiProvider)
    _build_provider(spec, enable_langfuse=False)

    assert "use_vertex" not in calls["kwargs"]
    assert "api_key" in calls["kwargs"]


def test_yaml_vertex_rejected_on_non_gemini_provider(tmp_path: Path) -> None:
    # #332: a vertex block on a non-gemini provider is a loud error, not silently
    # ignored.
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          vertex:
            project: x
        """,
    )
    with pytest.raises(ValueError, match="vertex is only valid for the gemini provider"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_vertex_and_api_key_are_mutually_exclusive(tmp_path: Path) -> None:
    # #332: Vertex uses ADC; a leftover api_key alongside vertex is a contradiction
    # and must fail loud rather than be silently ignored.
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: gemini
          api_key: leftover-key
          vertex:
            project: my-gcp-project
        """,
    )
    with pytest.raises(ValueError, match="mutually exclusive with runtime.provider.api_key"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_vertex_allows_cleared_api_key(tmp_path: Path) -> None:
    # #332 (Bugbot): an empty / unconfigured api_key is NOT a real key — layered
    # YAML uses `api_key: ""` to clear a Developer-API key while switching to
    # Vertex, so it must not trip the mutual-exclusion check.
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: gemini
          api_key: ""
          vertex:
            project: my-gcp-project
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    assert spec.runtime.provider.vertex is not None


def test_yaml_provider_api_key_secret_reference_resolves_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", " provider-secret\n")
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          api_key:
            value_from:
              env: OPENAI_API_KEY
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)

    _build_provider(spec, enable_langfuse=False)

    assert calls["kwargs"]["api_key"] == "provider-secret"
    assert "provider-secret" not in spec.model_dump_json()
    assert "OPENAI_API_KEY" in spec.model_dump_json()


def test_yaml_provider_api_key_secret_reference_resolves_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = tmp_path / "api-key.secret"
    secret_file.write_text(" file-secret\n", encoding="utf-8")
    path = _write_demo_yaml(
        tmp_path,
        runtime=f"""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {{}}
        provider:
          type: openai
          api_key:
            value_from:
              file: {secret_file}
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)

    _build_provider(spec, enable_langfuse=False)

    assert calls["kwargs"]["api_key"] == "file-secret"


def test_yaml_provider_api_key_secret_reference_missing_required_file_fails_when_used(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "absent.secret"
    path = _write_demo_yaml(
        tmp_path,
        runtime=f"""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {{}}
        provider:
          type: openai
          api_key:
            value_from:
              file: {missing}
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)

    with pytest.raises(ValueError, match="does not exist"):
        _build_provider(spec, enable_langfuse=False)


def test_yaml_provider_api_key_secret_reference_missing_required_env_fails_when_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          api_key:
            value_from:
              env: OPENAI_API_KEY
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)

    with pytest.raises(ValueError, match="runtime.provider.api_key.*OPENAI_API_KEY"):
        _build_provider(spec, enable_langfuse=False)


def test_yaml_provider_api_key_secret_reference_optional_missing_env_is_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          api_key:
            value_from:
              env: OPENAI_API_KEY
              required: false
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)

    _build_provider(spec, enable_langfuse=False)

    assert calls["kwargs"]["api_key"] is None


def test_yaml_build_provider_supports_anthropic_secret_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", " anthropic-secret\n")
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: anthropic
          model: claude-test
          base_url: https://anthropic.example.test
          api_key:
            value_from:
              env: ANTHROPIC_API_KEY
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeAnthropicProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr(
        "typeflux.yaml.runtime.AnthropicProvider",
        FakeAnthropicProvider,
    )

    _build_provider(spec, enable_langfuse=True)

    assert calls["kwargs"]["default_model"] == "claude-test"
    assert calls["kwargs"]["api_key"] == "anthropic-secret"
    assert calls["kwargs"]["base_url"] == "https://anthropic.example.test"
    assert calls["kwargs"]["default_provider_params"].to_dict() == {"model": "claude-test"}
    assert "enable_langfuse" not in calls["kwargs"]
    assert "anthropic-secret" not in spec.model_dump_json()
    assert "ANTHROPIC_API_KEY" in spec.model_dump_json()


def test_yaml_provider_params_max_tokens_flow_into_anthropic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # runtime.provider.params.max_tokens is the YAML control for the provider
    # output-token cap (#183); it must reach the constructed provider.
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: anthropic
          model: claude-test
          params:
            max_tokens: 16000
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    class FakeAnthropicProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

    monkeypatch.setattr(
        "typeflux.yaml.runtime.AnthropicProvider",
        FakeAnthropicProvider,
    )

    _build_provider(spec, enable_langfuse=False)

    params = calls["kwargs"]["default_provider_params"]
    assert params.to_dict()["max_tokens"] == 16000


def test_yaml_provider_params_load_workflow_prompt_and_activity_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first:
              model: claude-test
              temperature: 0
              provider_params:
                max_tokens: 12000
                top_p: 0.8
              messages:
                - role: user
                  content: first {{value}}
            second: second {{value}}
        provider:
          type: anthropic
          model: claude-test
          params:
            max_tokens: 8000
            timeout: 30
        """,
        modules=None,
        definitions="""
        - name: first
          input: schemas:InputModel
          output: schemas:MiddleModel
          prompt: first
          provider_params:
            max_tokens: 16000
          start_to_close_timeout_seconds: 600
        - name: second
          input: schemas:MiddleModel
          output: schemas:OutputModel
          prompt: second
        """,
    )

    spec = load_yaml_spec(path, load_dotenv=False)

    assert spec.runtime.provider.provider_params().to_dict() == {
        "model": "claude-test",
        "max_tokens": 8000,
        "timeout": 30,
    }
    assert spec.runtime.registry.prompts["first"].provider_params.to_provider_params(
        legacy_model="claude-test",
        legacy_temperature=0,
    ).to_dict() == {
        "model": "claude-test",
        "temperature": 0,
        "max_tokens": 12000,
        "top_p": 0.8,
    }
    assert spec.activities.definitions[0].provider_params.to_provider_params().to_dict() == {
        "max_tokens": 16000
    }
    assert spec.activities.definitions[0].start_to_close_timeout_seconds == 600


def test_yaml_provider_params_reject_conflicting_provider_model(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          model: gpt-4o-mini
          params:
            model: gpt-4o
        """,
    )

    with pytest.raises(ValueError, match="runtime.provider.model.*params.model"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_activities_get_bounded_default_temporal_retry_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    workflow_cls = create_workflow(spec, collect_activities(spec))

    from datetime import timedelta

    for call in workflow_cls.__typeflux_activity_calls__:
        policy = call.retry_policy
        assert policy is not None
        assert policy.maximum_attempts == 5
        assert policy.initial_interval == timedelta(seconds=1)
        assert policy.maximum_interval == timedelta(seconds=60)
        assert policy.backoff_coefficient == 2.0


def test_yaml_activity_retry_overrides_runtime_default_and_allows_unlimited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            activity_retry:
              maximum_attempts: 2
              initial_interval_seconds: 0.5
            """,
            modules=None,
            definitions="""
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: first
              retry:
                maximum_attempts: 0
            - name: second
              input: schemas:MiddleModel
              output: schemas:OutputModel
              prompt: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    first_call, second_call = workflow_cls.__typeflux_activity_calls__
    # The per-activity retry block wins; maximum_attempts 0 is Temporal's
    # explicit unlimited sentinel.
    assert first_call.retry_policy.maximum_attempts == 0
    # Activities without their own retry block use the runtime default.
    assert second_call.retry_policy.maximum_attempts == 2
    assert second_call.retry_policy.initial_interval.total_seconds() == 0.5


def test_yaml_activity_cache_config_reaches_runtime_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #60: the opt-in ``cache:`` block on a definition must thread through to the
    # runtime AIActivity (as SessionCacheConfig) so the executor can act on it.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            """,
            modules=None,
            definitions="""
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: first
              cache:
                ttl_seconds: 300
            - name: second
              input: schemas:MiddleModel
              output: schemas:OutputModel
              prompt: second
            """,
        )
    )
    activities = collect_activities(spec)

    assert activities["first"].session_cache is not None
    assert activities["first"].session_cache.enabled is True
    assert activities["first"].session_cache.ttl_seconds == 300
    # Absent ``cache:`` ⇒ no request (opt-in).
    assert activities["second"].session_cache is None


_MODERATION_DEFINITIONS = """
- name: first
  input: schemas:InputModel
  output: schemas:MiddleModel
  prompt: first
  moderation:
{moderation}
- name: second
  input: schemas:MiddleModel
  output: schemas:OutputModel
  prompt: second
"""


def test_yaml_moderation_provider_openai_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #158: a built-in provider moderator wires from YAML with no Python.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions=_MODERATION_DEFINITIONS.format(
                moderation="    provider: openai\n    on_violation: block"
            ),
        )
    )
    activities = collect_activities(spec)

    assert activities["first"].moderation is not None
    assert activities["first"].moderation.on_violation == "block"
    assert callable(activities["first"].moderation.moderator)
    # Absent ``moderation:`` ⇒ no moderator (opt-in).
    assert activities["second"].moderation is None


def _moderator_closure_value(moderator: object, name: str) -> object:
    # Read a value captured by the built-in moderator closure (e.g. its model), so
    # a YAML-wired provider moderator can be checked without a live call.
    fn = moderator  # type: ignore[assignment]
    idx = fn.__code__.co_freevars.index(name)  # type: ignore[attr-defined]
    return fn.__closure__[idx].cell_contents  # type: ignore[attr-defined]


def test_yaml_moderation_provider_gemini_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #382: provider: gemini wires the built-in gemini_moderator from YAML with no
    # Python and no injected client (the SDK is only needed at call time).
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions=_MODERATION_DEFINITIONS.format(
                moderation="    provider: gemini\n    on_violation: block"
            ),
        )
    )
    activities = collect_activities(spec)

    moderator = activities["first"].moderation.moderator
    assert moderator.__name__ == "gemini_moderator"
    assert _moderator_closure_value(moderator, "model") == "gemini-flash-lite-latest"


def test_yaml_moderation_provider_gemini_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #382 D382-4: moderation.model overrides the default gemini model.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions=_MODERATION_DEFINITIONS.format(
                moderation="    provider: gemini\n    model: gemini-2.5-flash"
            ),
        )
    )
    activities = collect_activities(spec)
    moderator = activities["first"].moderation.moderator
    assert moderator.__name__ == "gemini_moderator"
    assert _moderator_closure_value(moderator, "model") == "gemini-2.5-flash"


def test_yaml_moderation_custom_moderator_requires_allow_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #158: importing an arbitrary moderator callable from YAML is gated by the
    # imports policy, like a type: custom extension class.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions=_MODERATION_DEFINITIONS.format(
                moderation="    moderator: yaml_demo_project.moderators:safe\n"
                "    on_violation: flag"
            ),
        )
    )
    with pytest.raises(ValueError, match="allow_moderator_callable"):
        collect_activities(spec)


def test_yaml_moderation_custom_moderator_resolves_with_allow_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #158: with the imports flag on, a project moderator callable resolves.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    (tmp_path / "yaml_demo_project" / "moderators.py").write_text(
        dedent(
            """
            from pydantic import BaseModel
            from typeflux import ModerationResult

            def safe(output: BaseModel) -> ModerationResult:
                return ModerationResult(flagged=False)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            imports:
              allow_moderator_callable: true
            """,
            modules=None,
            definitions=_MODERATION_DEFINITIONS.format(
                moderation="    moderator: yaml_demo_project.moderators:safe\n"
                "    on_violation: flag"
            ),
        )
    )
    activities = collect_activities(spec)
    assert activities["first"].moderation is not None
    assert activities["first"].moderation.on_violation == "flag"
    assert activities["first"].moderation.moderator.__name__ == "safe"


def test_yaml_moderation_requires_exactly_one_source(tmp_path: Path) -> None:
    # #158: provider and moderator are mutually exclusive; one is required.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="exactly one of 'provider' or 'moderator'"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                modules=None,
                definitions=_MODERATION_DEFINITIONS.format(
                    moderation="    provider: openai\n    moderator: x:y"
                ),
            )
        )
    # An empty/whitespace model is rejected rather than silently using the default.
    with pytest.raises(ValidationError, match="non-empty and unpadded"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                modules=None,
                definitions=_MODERATION_DEFINITIONS.format(
                    moderation='    provider: openai\n    model: ""'
                ),
            )
        )


def test_yaml_map_steps_get_resolved_temporal_retry_policy(tmp_path: Path) -> None:
    _write_map_demo_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        spec = load_yaml_spec(_write_map_demo_yaml(tmp_path))
        workflow_cls = create_workflow(spec, collect_activities(spec))

        map_call = workflow_cls.__typeflux_activity_calls__[0]
        assert map_call.retry_policy is not None
        assert map_call.retry_policy.maximum_attempts == 5
    finally:
        sys.path.remove(str(tmp_path))


def test_yaml_activity_retry_rejects_invalid_values(tmp_path: Path) -> None:
    base = """
    - name: first
      input: schemas:InputModel
      output: schemas:MiddleModel
      prompt: first
      retry:
        {retry_field}
    - name: second
      input: schemas:MiddleModel
      output: schemas:OutputModel
      prompt: second
    """
    for retry_field, message in [
        ("maximum_attempts: -1", "maximum_attempts must be >= 0"),
        ("initial_interval_seconds: 0", "initial_interval_seconds must be > 0"),
        ("maximum_interval_seconds: 0", "maximum_interval_seconds must be > 0"),
        ("backoff_coefficient: 0.5", "backoff_coefficient must be >= 1"),
    ]:
        path = _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions=base.format(retry_field=retry_field),
        )
        with pytest.raises(ValueError, match=message):
            load_yaml_spec(path, load_dotenv=False)


def test_yaml_run_rejects_expected_policy_hash_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", "0" * 64)
    path = _write_demo_yaml(tmp_path)

    with pytest.raises(SystemExit, match="project run"):
        yaml_run.main([str(path), "--preflight"])


def test_yaml_provider_class_rejected_for_builtin_provider_types(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: openai
          class: demo.providers:CustomProvider
        imports:
          allow_provider_class: true
        """,
    )

    with pytest.raises(ValueError, match="only supported with provider type 'custom'"):
        load_yaml_spec(path, load_dotenv=False)


def test_yaml_provider_prompt_model_override_defaults_to_disabled(tmp_path: Path) -> None:
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))

    assert spec.runtime.provider.allow_prompt_model_override is False


def test_yaml_provider_prompt_model_override_parses_opt_in(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
          allow_prompt_model_override: true
        """,
    )

    spec = load_yaml_spec(path, load_dotenv=False)

    assert spec.runtime.provider.allow_prompt_model_override is True


def test_yaml_activity_timeout_rejects_non_positive_values(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        modules=None,
        definitions="""
        - name: first
          input: schemas:InputModel
          output: schemas:MiddleModel
          prompt: first
          start_to_close_timeout_seconds: 0
        """,
    )

    with pytest.raises(ValueError, match="start_to_close_timeout_seconds must be > 0"):
        load_yaml_spec(path)


def test_yaml_activity_heartbeat_timeout_parses(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        modules=None,
        definitions="""
        - name: first
          input: schemas:InputModel
          output: schemas:MiddleModel
          prompt: first
          heartbeat_timeout_seconds: 30
        - name: second
          input: schemas:MiddleModel
          output: schemas:OutputModel
          prompt: second
        """,
    )

    spec = load_yaml_spec(path, load_dotenv=False)
    assert spec.activities.definitions[0].heartbeat_timeout_seconds == 30


def test_yaml_activity_heartbeat_timeout_rejects_non_positive_values(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        modules=None,
        definitions="""
        - name: first
          input: schemas:InputModel
          output: schemas:MiddleModel
          prompt: first
          heartbeat_timeout_seconds: 0
        """,
    )

    with pytest.raises(ValueError, match="heartbeat_timeout_seconds must be > 0"):
        load_yaml_spec(path)


def test_activity_kwargs_include_heartbeat_timeout_only_when_set() -> None:
    base = {
        "step_id": "classify",
        "activity_name": "classify",
        "task_queue": None,
        "start_to_close_timeout": workflow_module.DEFAULT_START_TO_CLOSE_TIMEOUT,
        "retry_policy": None,
    }
    with_hb = workflow_module.ActivityCallSpec(**base, heartbeat_timeout=timedelta(seconds=20))
    without_hb = workflow_module.ActivityCallSpec(**base)

    assert workflow_module._activity_kwargs(with_hb, "classify")["heartbeat_timeout"] == timedelta(
        seconds=20
    )
    assert "heartbeat_timeout" not in workflow_module._activity_kwargs(without_hb, "classify")


def test_yaml_build_provider_rejects_unsupported_runtime_provider_params(
    tmp_path: Path,
) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts: {}
        provider:
          type: openai
          params:
            top_k: 40
        """,
    )
    # #789: the rejection moved from provider BUILD to spec LOAD (TS parity — the TS
    # spec rejects via unsupportedProviderParamKeys at load), so validate/admit/run/
    # submit all refuse the same spec the worker preflight would.
    with pytest.raises(ValueError, match="top_k"):
        load_yaml_spec(path, load_dotenv=False)


@pytest.mark.asyncio
async def test_yaml_temporal_api_key_secret_reference_resolves_env_for_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_API_KEY", " temporal-secret\n")
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: cloud.tmprl.cloud:7233
          namespace: cloud
          tls: true
          api_key:
            value_from:
              env: TEMPORAL_API_KEY
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        """,
    )
    spec = load_yaml_spec(path, load_dotenv=False)
    calls: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any):
        calls["address"] = address
        calls["kwargs"] = kwargs
        return "client"

    from temporalio.client import Client

    monkeypatch.setattr(Client, "connect", fake_connect)

    client = await _connect_client(spec, plugin="plugin")

    assert client == "client"
    assert calls["address"] == "cloud.tmprl.cloud:7233"
    assert calls["kwargs"]["namespace"] == "cloud"
    assert calls["kwargs"]["tls"] is True
    assert calls["kwargs"]["api_key"] == "temporal-secret"
    assert calls["kwargs"]["plugins"] == ["plugin"]


def test_committed_example_yamls_default_temporal_tls_and_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    for name in (
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "TEMPORAL_TLS",
        "TEMPORAL_API_KEY",
        "TEMPORAL_TASK_QUEUE",
    ):
        monkeypatch.delenv(name, raising=False)

    repo_root = Path(__file__).resolve().parents[1]
    example_paths = [
        repo_root / "examples" / "lifecycle_review" / "typeflux.yaml",
        repo_root / "examples" / "support_triage_langfuse" / "typeflux.yaml",
        repo_root / "examples" / "insurance_claim_review" / "typeflux.yaml",
        repo_root / "examples" / "financial_claims_marketing_review" / "typeflux.yaml",
    ]

    for path in example_paths:
        spec = load_yaml_spec(path)
        assert spec.runtime.temporal.address == "localhost:7233"
        assert spec.runtime.temporal.namespace == "default"
        assert spec.runtime.temporal.tls is False
        api_key = spec.runtime.temporal.api_key
        assert api_key is not None
        assert api_key.value_from.env == "TEMPORAL_API_KEY"
        assert api_key.value_from.required is False


def test_env_example_documents_temporal_connection_vars() -> None:
    repo_root = Path(__file__).resolve().parents[3]  # .env.example is a repo-root asset
    env_example = (repo_root / ".env.example").read_text(encoding="utf-8")

    for name in (
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "TEMPORAL_TLS",
        "TEMPORAL_API_KEY",
        "TEMPORAL_TASK_QUEUE",
    ):
        assert f"{name}=" in env_example


def test_yaml_env_interpolation_fails_on_missing_required_var(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: demo
        task_queue: ${MISSING_TASK_QUEUE}
        runtime:
          registry:
            type: inline
            prompts: {}
          provider:
            type: fake
        activities:
          modules: [activities]
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )

    with pytest.raises(KeyError, match="MISSING_TASK_QUEUE"):
        load_yaml_spec(path)


def test_yaml_runtime_import_policy_parses(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: demo
        task_queue: demo-task-queue
        runtime:
          registry:
            type: inline
            prompts: {}
          provider:
            type: fake
          imports:
            allow_absolute_activity_modules: true
            allow_provider_class: true
            allowed_module_roots:
              - shared_ai
        activities:
          modules: [activities]
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )

    spec = load_yaml_spec(path)

    assert spec.runtime.imports.allow_absolute_activity_modules is True
    assert spec.runtime.imports.allow_provider_class is True
    assert spec.runtime.imports.allowed_module_roots == ["shared_ai"]


def test_yaml_env_interpolation_error_includes_file_and_nested_path(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: demo
        task_queue: demo-task-queue
        runtime:
          registry:
            type: inline
            prompts: {}
          provider:
            type: fake
        activities:
          modules: [activities]
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: ${MISSING_ACTIVITY}
        """,
    )

    with pytest.raises(KeyError) as exc_info:
        load_yaml_spec(path)

    message = str(exc_info.value)
    assert "MISSING_ACTIVITY" in message
    assert str(path) in message
    assert "$.workflow.steps[0].activity" in message


def test_yaml_env_interpolation_skips_inline_prompt_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prompt bodies are model-facing content: a literal ${NAME} must survive
    # loading verbatim — neither substituted nor treated as a missing variable.
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("LEAKY_SECRET", "should-never-appear")
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: demo
        task_queue: demo-task-queue
        runtime:
          registry:
            type: inline
            prompts:
              string_prompt: "Literal ${LEAKY_SECRET} and ${UNSET_VAR} stay."
              message_prompt:
                model: ${PROMPT_MODEL:-fake-model}
                messages:
                  - role: system
                    content: "System keeps ${UNSET_VAR} literal."
                  - role: user
                    content:
                      - type: text
                        text: "Part keeps ${LEAKY_SECRET} literal."
                      - type: artifact_group
                        group: docs
          provider:
            type: fake
          artifacts:
            local_roots: [fixtures]
        activities:
          definitions:
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: string_prompt
              artifacts:
                - name: docs
                  from: input.value
                  kind: document
                  attach:
                    role: user
                    text: "Attach keeps ${UNSET_VAR} literal:"
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )

    spec = load_yaml_spec(path, load_dotenv=False)

    prompts = spec.runtime.registry.prompts
    assert prompts["string_prompt"] == "Literal ${LEAKY_SECRET} and ${UNSET_VAR} stay."
    message_prompt = prompts["message_prompt"]
    assert message_prompt.messages[0].content == "System keeps ${UNSET_VAR} literal."
    assert message_prompt.messages[1].content[0].text == "Part keeps ${LEAKY_SECRET} literal."
    # Prompt config keys (model) still interpolate.
    assert message_prompt.model == "fake-model"
    attach = spec.activities.definitions[0].artifacts[0].attach
    assert attach is not None
    assert attach.text == "Attach keeps ${UNSET_VAR} literal:"
    assert "should-never-appear" not in spec.model_dump_json()


def test_yaml_env_interpolation_escape_renders_literal_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("REAL_QUEUE", "real-queue")
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: "demo-$${NOT_A_VAR}"
        task_queue: ${REAL_QUEUE}-$${SUFFIX:-x}
        runtime:
          registry:
            type: inline
            prompts: {}
          provider:
            type: fake
        activities:
          modules: [activities]
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )

    spec = load_yaml_spec(path, load_dotenv=False)

    assert spec.name == "demo-${NOT_A_VAR}"
    assert spec.task_queue == "real-queue-${SUFFIX:-x}"


def test_yaml_observability_redaction_options_load(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            registry:
              type: inline
              prompts: {}
            provider:
              type: fake
            observability:
              redaction:
                enabled: true
                emails: false
                phones: true
                ssn: false
                credit_cards: false
                preserve_typeflux_metadata: false
                exclude_paths:
                  - metadata.ticket_id
            """,
        )
    )

    redaction = spec.runtime.observability.redaction
    assert redaction.enabled is True
    assert redaction.emails is False
    assert redaction.phones is True
    assert redaction.ssn is False
    assert redaction.credit_cards is False
    assert redaction.preserve_typeflux_metadata is False
    assert redaction.exclude_paths == ["metadata.ticket_id"]


def test_load_yaml_spec_rejects_disallowed_override_keys(tmp_path: Path) -> None:
    path = _write_demo_yaml(tmp_path)

    for key in ("project", "name", "activities", "workflow"):
        with pytest.raises(ValueError, match=f"overrides.{key}"):
            load_yaml_spec(path, overrides={key: "not-allowed"})

    with pytest.raises(ValueError, match="overrides.runtime.imports"):
        load_yaml_spec(path, overrides={"runtime": {"imports": {}}})


def test_load_yaml_spec_allows_safe_runtime_and_task_queue_overrides(
    tmp_path: Path,
) -> None:
    path = _write_demo_yaml(tmp_path)

    spec = load_yaml_spec(
        path,
        overrides={
            "task_queue": "override-queue",
            "runtime": {
                "temporal": {"address": "override:7233"},
                "provider": {"model": "override-model"},
            },
        },
    )

    assert spec.task_queue == "override-queue"
    assert spec.runtime.temporal.address == "override:7233"
    assert spec.runtime.provider.model == "override-model"
    assert spec._override_provenance is not None
    assert spec._override_provenance.source == "load_yaml_spec"
    assert spec._override_provenance.override_paths == (
        "runtime.provider.model",
        "runtime.temporal.address",
        "task_queue",
    )


def test_yaml_provider_limits_load(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            registry:
              type: inline
              prompts: {}
            provider:
              type: fake
            provider_limits:
              default:
                max_concurrent: 8
                min_interval_seconds: 0.0
              providers:
                fake:
                  max_concurrent: 6
                  min_interval_seconds: 0.1
                  models:
                    slow-model:
                      max_concurrent: 3
                      min_interval_seconds: 0.25
            """,
        )
    )

    provider_limits = spec.runtime.provider_limits
    assert provider_limits is not None
    assert provider_limits.default is not None
    assert provider_limits.default.max_concurrent == 8
    assert provider_limits.default.min_interval_seconds == 0.0
    fake_limits = provider_limits.providers["fake"]
    assert fake_limits.max_concurrent == 6
    assert fake_limits.min_interval_seconds == 0.1
    assert fake_limits.models["slow-model"].max_concurrent == 3
    assert fake_limits.models["slow-model"].min_interval_seconds == 0.25


def test_yaml_provider_limits_reject_invalid_limits(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        registry:
          type: inline
          prompts: {}
        provider:
          type: fake
        provider_limits:
          default:
            max_concurrent: 0
        """,
    )

    with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
        load_yaml_spec(path)


def test_yaml_provider_retry_loads(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            registry:
              type: inline
              prompts: {}
            provider:
              type: fake
            provider_retry:
              max_attempts: 3
              initial_backoff_seconds: 0.5
              max_backoff_seconds: 5.0
              backoff_multiplier: 1.5
              retry_rate_limits: false
              retry_transient_errors: true
            """,
        )
    )

    provider_retry = spec.runtime.provider_retry
    assert provider_retry is not None
    assert provider_retry.max_attempts == 3
    assert provider_retry.initial_backoff_seconds == 0.5
    assert provider_retry.max_backoff_seconds == 5.0
    assert provider_retry.backoff_multiplier == 1.5
    assert provider_retry.retry_rate_limits is False
    assert provider_retry.retry_transient_errors is True


def test_yaml_provider_retry_rejects_invalid_values(tmp_path: Path) -> None:
    invalid_cases = [
        ("max_attempts: 0", "max_attempts must be >= 1"),
        ("initial_backoff_seconds: -0.1", "initial_backoff_seconds must be >= 0"),
        ("max_backoff_seconds: -1", "max_backoff_seconds must be >= 0"),
        ("backoff_multiplier: 0.5", "backoff_multiplier must be >= 1"),
    ]

    for config, message in invalid_cases:
        path = _write_demo_yaml(
            tmp_path,
            runtime=f"""
            registry:
              type: inline
              prompts: {{}}
            provider:
              type: fake
            provider_retry:
              {config}
            """,
        )

        with pytest.raises(ValueError, match=message):
            load_yaml_spec(path)


def test_yaml_lifecycle_block_loads(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            cancellation: true
            review:
              after_step: first
              user_decisions:
                send_email:
                  route: second
              invalid_user_decision: fail
            """,
        )
    )

    lifecycle = spec.workflow.lifecycle
    assert lifecycle is not None
    assert lifecycle.enabled is True
    assert lifecycle.progress is True
    assert lifecycle.cancellation is True
    assert lifecycle.review is not None
    assert lifecycle.review.after_step == "first"
    assert lifecycle.review.user_decisions["send_email"].route == "second"
    assert lifecycle.review.invalid_user_decision == "fail"
    assert lifecycle.history.status_event_limit == 50


def test_yaml_lifecycle_history_limit_loads(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            history:
              status_event_limit: 3
            """,
        )
    )

    assert spec.workflow.lifecycle is not None
    assert spec.workflow.lifecycle.history.status_event_limit == 3


def test_yaml_lifecycle_rejects_negative_history_limit(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        history:
          status_event_limit: -1
        """,
    )

    with pytest.raises(ValueError, match="status_event_limit must be >= 0"):
        load_yaml_spec(path)


def test_yaml_lifecycle_rejects_unknown_review_step(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: missing
          user_decisions:
            send_email:
              route: second
        """,
    )

    with pytest.raises(ValueError, match="after_step references unknown step: missing"):
        load_yaml_spec(path)


def test_yaml_lifecycle_rejects_empty_user_decisions(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: first
          user_decisions: {}
        """,
    )

    with pytest.raises(ValueError, match="user_decisions must not be empty"):
        load_yaml_spec(path)


def test_yaml_lifecycle_rejects_unknown_review_route(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: first
          user_decisions:
            send_email:
              route: missing
        """,
    )

    with pytest.raises(ValueError, match="routes to unknown step: missing"):
        load_yaml_spec(path)


def test_yaml_lifecycle_rejects_non_forward_review_route(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: second
          user_decisions:
            retry_first:
              route: first
        """,
    )

    with pytest.raises(ValueError, match="must route to a step after second"):
        load_yaml_spec(path)


def test_yaml_lifecycle_rejects_invalid_user_decision_policy(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: first
          invalid_user_decision: ignore
          user_decisions:
            send_email:
              route: second
        """,
    )

    with pytest.raises(ValueError, match="invalid_user_decision"):
        load_yaml_spec(path)


def test_yaml_lifecycle_rejects_legacy_review_decision_lists(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: first
          approve_decisions: [approved]
          reject_decisions: [rejected]
          user_decisions:
            send_email:
              route: second
        """,
    )

    with pytest.raises(ValueError, match="approve_decisions"):
        load_yaml_spec(path)


def test_yaml_review_timeout_loads_and_participates_in_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #297: a review timeout parses and, because it adds a durable timer + a
    # timeout branch, changes the workflow digest (vs no timeout / a different
    # timeout) so the versioned workflow type is distinct.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))

    def _digest(lifecycle: str) -> str:
        spec = load_yaml_spec(_write_demo_yaml(tmp_path, lifecycle=lifecycle))
        return create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

    base_review = """
        enabled: true
        review:
          after_step: first
          user_decisions:
            send_email:
              route: second
    """
    no_timeout = _digest(base_review)
    fail_timeout = _digest(base_review + "\n          timeout:\n            seconds: 3600\n")
    cancel_timeout = _digest(
        base_review
        + "\n          timeout:\n            seconds: 3600\n            on_timeout: cancel\n"
    )
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path, lifecycle=base_review + "\n          timeout:\n            seconds: 60\n"
        )
    )
    assert spec.workflow.lifecycle.review.timeout.seconds == 60
    assert spec.workflow.lifecycle.review.timeout.on_timeout == "fail"
    # Distinct control flow ⇒ distinct digests.
    assert len({no_timeout, fail_timeout, cancel_timeout}) == 3


def test_yaml_review_timeout_validation(tmp_path: Path) -> None:
    # #297: timeout spec validators.
    def _yaml(timeout_block: str) -> Path:
        return _write_demo_yaml(
            tmp_path,
            lifecycle=f"""
            enabled: true
            review:
              after_step: first
              user_decisions:
                send_email:
                  route: second
              timeout:
{timeout_block}
            """,
        )

    # seconds must be >= 1.
    with pytest.raises(ValueError, match="review timeout seconds must be >= 1"):
        load_yaml_spec(_yaml("                seconds: 0"))
    # route required when on_timeout: route.
    with pytest.raises(ValueError, match="requires a non-empty route"):
        load_yaml_spec(_yaml("                seconds: 60\n                on_timeout: route"))
    # route forbidden when on_timeout is not route.
    with pytest.raises(ValueError, match="route is only valid with on_timeout: route"):
        load_yaml_spec(
            _yaml(
                "                seconds: 60\n                on_timeout: fail\n                route: second"
            )
        )
    # timeout route must exist and be forward of after_step.
    with pytest.raises(ValueError, match="timeout routes to unknown step"):
        load_yaml_spec(
            _yaml(
                "                seconds: 60\n                on_timeout: route\n                route: nope"
            )
        )


def _review_timeout_lifecycle(tmp_path: Path, action: str, *, route: str | None = None):
    extra = f"\n                route: {route}" if route else ""
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle=f"""
            enabled: true
            review:
              after_step: first
              user_decisions:
                send_email:
                  route: second
              timeout:
                seconds: 3600
                on_timeout: {action}{extra}
            """,
        )
    )
    return workflow_module._LifecycleRuntime(spec.workflow.lifecycle)


def test_review_timed_out_actions(tmp_path: Path) -> None:
    # #297 / #55 slice 4: the three timeout actions on the per-gate lifecycle runtime.
    # fail-closed → raises.
    fail_rt = _review_timeout_lifecycle(tmp_path, "fail")
    with pytest.raises(Exception, match="review timed out"):
        fail_rt.gate_timed_out(None, fail_rt.pending_gate_after("first"))
    assert fail_rt.terminal_status == "failed"

    # cancel → marks cancellation, returns None.
    cancel_rt = _review_timeout_lifecycle(tmp_path, "cancel")
    assert cancel_rt.gate_timed_out(None, cancel_rt.pending_gate_after("first")) is None
    assert cancel_rt.cancellation_requested is True
    assert cancel_rt.state == "cancelling"

    # route → returns the route target, sets it in status, keeps running.
    route_rt = _review_timeout_lifecycle(tmp_path, "route", route="second")
    route_gate = route_rt.pending_gate_after("first")
    route_rt.waiting_for_gate(None, route_gate)  # open the gate as the interpreter does
    assert route_rt.gate_timed_out(None, route_gate) == "second"
    assert route_rt.state == "running"
    assert route_rt.review_route_target == "second"  # status reflects the route
    # Byte-parity: the review_timed_out event snapshots state BEFORE the action flips it (V1).
    timed_out = next(e for e in route_rt.status().events if e.event == "review_timed_out")
    assert timed_out.state == "waiting_for_review"
    assert timed_out.waiting_checkpoint is None


@pytest.mark.asyncio
async def test_maybe_wait_for_review_applies_timeout(tmp_path: Path) -> None:
    # #297: when no decision arrives, _maybe_wait_for_review fires the timeout
    # action. Uses the polling fallback (no wait_condition) with a tiny override.
    rt = _review_timeout_lifecycle(tmp_path, "fail")
    rt.pending_gate_after("first").timeout_seconds = 0.01  # fast, bypass spec >=1
    fake_workflow = SimpleNamespace()  # no wait_condition → polling fallback

    with pytest.raises(Exception, match="review timed out"):
        await workflow_module._maybe_wait_for_review(fake_workflow, rt, "first")


def _multi_gate_lifecycle() -> Any:
    # A directly-built two-gate lifecycle spec (the runtime needs no steps to exercise gate logic).
    from typeflux.yaml.spec import (
        WorkflowLifecycleGateSpec,
        WorkflowLifecycleReviewRouteSpec,
        WorkflowLifecycleSpec,
    )

    return WorkflowLifecycleSpec(
        enabled=True,
        gates=[
            WorkflowLifecycleGateSpec(
                id="first",
                after_step="a",
                user_decisions={"go": WorkflowLifecycleReviewRouteSpec(route="b")},
            ),
            WorkflowLifecycleGateSpec(
                id="second",
                after_step="b",
                user_decisions={
                    "yes": WorkflowLifecycleReviewRouteSpec(route="c"),
                    "no": WorkflowLifecycleReviewRouteSpec(route="c"),
                },
                invalid_user_decision="fail",
            ),
        ],
    )


def test_multi_gate_runtime_targets_by_id_and_stamps_gate_id() -> None:
    # #55 slice 4: a decision addressed to a gate by id resolves only that gate, and
    # multi-gate review events carry gate_id.
    rt = workflow_module._LifecycleRuntime(_multi_gate_lifecycle())
    rt.start(None, total_units=0)
    first = rt.pending_gate_after("a")
    rt.waiting_for_gate(None, first)
    status = rt.status()
    assert status.waiting_checkpoint == "a"
    assert [g.model_dump() for g in status.waiting_gates] == [
        {"gate_id": "first", "after_step": "a", "valid_user_decisions": {"go": "b"}}
    ]
    assert status.events[-1].event == "waiting_for_review"
    assert status.events[-1].gate_id == "first"
    rt.submit_review(None, {"user_decision": "go", "gate": "first"})
    assert first.route_target == "b"
    assert rt.gate_routed(None, first) == "b"
    assert rt.status().review_route_target == "b"
    assert rt.status().waiting_gates == ()
    assert rt.status().events[-1].gate_id == "first"


def test_multi_gate_runtime_ambiguous_and_explicit_targeting() -> None:
    rt = workflow_module._LifecycleRuntime(_multi_gate_lifecycle())
    rt.start(None, total_units=0)
    first = rt.pending_gate_after("a")
    second = rt.pending_gate_after("b")
    rt.waiting_for_gate(None, first)
    rt.waiting_for_gate(None, second)
    # Two gates waiting, no `gate` field ⇒ ambiguous ⇒ recorded invalid, nothing resolved.
    rt.submit_review(None, {"user_decision": "go"})
    assert rt.status().events[-1].event == "review_invalid_user_decision"
    assert first.route_target is None and second.route_target is None
    # waiting_gates lists both, ordered by open time; waiting_checkpoint is the earliest-opened.
    assert [g.gate_id for g in rt.status().waiting_gates] == ["first", "second"]
    assert rt.status().waiting_checkpoint == "a"
    # An explicit gate id resolves exactly that gate.
    rt.submit_review(None, {"user_decision": "yes", "gate": "second"})
    assert second.route_target == "c" and first.route_target is None


def test_multi_gate_runtime_pre_submission_and_fail_policy() -> None:
    rt = workflow_module._LifecycleRuntime(_multi_gate_lifecycle())
    rt.start(None, total_units=0)
    # Decide `second` before it opens — stored on that gate, not on `first`.
    rt.submit_review(None, {"user_decision": "yes", "gate": "second"})
    assert rt.pending_gate_after("a").route_target is None
    assert rt.pending_gate_after("b").route_target == "c"
    # An unknown decision on the `fail`-policy `second` gate trips ONLY that gate's flag
    # (per-gate — one gate's failure can never leak into another gate's wait loop).
    rt.submit_review(None, {"user_decision": "maybe", "gate": "second"})
    assert rt.pending_gate_after("b").invalid_failed is True
    assert rt.pending_gate_after("a").invalid_failed is False
    assert rt.status().events[-1].event == "review_invalid_user_decision"
    assert rt.status().events[-1].gate_id == "second"


def test_multi_gate_runtime_closed_gate_untargetable_and_no_flag_leak() -> None:
    # #55 slice 4 review round (item 3): a routed gate is CLOSED — an explicit `gate` id
    # aimed at it records invalid (never-guess) and its fail policy cannot trip any flag.
    rt = workflow_module._LifecycleRuntime(_multi_gate_lifecycle())
    rt.start(None, total_units=0)
    first = rt.pending_gate_after("a")
    rt.waiting_for_gate(None, first)
    rt.submit_review(None, {"user_decision": "go", "gate": "first"})
    rt.gate_routed(None, first)
    # Explicit targeting of the closed `first` gate: invalid, with the ATTEMPTED id recorded.
    rt.submit_review(None, {"user_decision": "go", "gate": "first"})
    assert rt.status().events[-1].event == "review_invalid_user_decision"
    assert rt.status().events[-1].gate_id == "first"
    # Close the fail-policy `second` gate, then aim an invalid decision at it: un-targetable,
    # so no flag trips anywhere.
    second = rt.pending_gate_after("b")
    rt.waiting_for_gate(None, second)
    rt.submit_review(None, {"user_decision": "yes", "gate": "second"})
    rt.gate_routed(None, second)
    rt.submit_review(None, {"user_decision": "bogus", "gate": "second"})
    assert second.invalid_failed is False
    assert first.invalid_failed is False
    # A mistyped gate id records the ATTEMPTED id for audit legibility (item 10).
    rt.submit_review(None, {"user_decision": "go", "gate": "nope"})
    assert rt.status().events[-1].gate_id == "nope"


def test_multi_gate_timeout_events_never_leak_prior_gate_decision() -> None:
    # #55 slice 4 review round (item 1, cross-edition parity pin): after gate A resolves,
    # gate B's waiting/timeout events must carry NO decision/route (None/None) — the TS
    # edition passes explicit nulls to match this exact behavior.
    from typeflux.yaml.spec import (
        WorkflowLifecycleGateSpec,
        WorkflowLifecycleReviewRouteSpec,
        WorkflowLifecycleReviewTimeoutSpec,
        WorkflowLifecycleSpec,
    )

    spec = WorkflowLifecycleSpec(
        enabled=True,
        gates=[
            WorkflowLifecycleGateSpec(
                id="first",
                after_step="a",
                user_decisions={"go": WorkflowLifecycleReviewRouteSpec(route="b")},
            ),
            WorkflowLifecycleGateSpec(
                id="second",
                after_step="b",
                user_decisions={"yes": WorkflowLifecycleReviewRouteSpec(route="c")},
                timeout=WorkflowLifecycleReviewTimeoutSpec(
                    seconds=5, on_timeout="route", route="c"
                ),
            ),
        ],
    )
    rt = workflow_module._LifecycleRuntime(spec)
    rt.start(None, total_units=0)
    first = rt.pending_gate_after("a")
    rt.waiting_for_gate(None, first)
    rt.submit_review(None, {"user_decision": "go", "gate": "first"})
    rt.gate_routed(None, first)
    assert rt.status().review_user_decision == "go"  # the singleton holds A's decision
    second = rt.pending_gate_after("b")
    rt.waiting_for_gate(None, second)
    assert rt.gate_timed_out(None, second) == "c"
    b_events = [e for e in rt.status().events if e.gate_id == "second"]
    assert [e.event for e in b_events] == [
        "waiting_for_review",
        "review_timed_out",
        "review_routed",
    ]
    assert all(e.review_user_decision is None for e in b_events)
    assert [e.review_route_target for e in b_events] == [None, None, "c"]
    # And the review_timed_out event still snapshots the waiting state (byte-parity).
    assert b_events[1].state == "waiting_for_review"
    # STATUS must not pair A's decision with B's timeout route: the singleton
    # decision clears when a timeout route is set.
    assert rt.status().review_user_decision is None
    assert rt.status().review_route_target == "c"


def test_multi_gate_runtime_checkpoint_advances_on_resolution() -> None:
    rt = workflow_module._LifecycleRuntime(_multi_gate_lifecycle())
    rt.start(None, total_units=0)
    first = rt.pending_gate_after("a")
    second = rt.pending_gate_after("b")
    rt.waiting_for_gate(None, first)
    rt.waiting_for_gate(None, second)
    rt.submit_review(None, {"user_decision": "go", "gate": "first"})
    rt.gate_routed(None, first)
    # `first` resolved; the checkpoint moves to the still-waiting `second`.
    assert rt.status().state == "waiting_for_review"
    assert rt.status().waiting_checkpoint == "b"
    assert [g.gate_id for g in rt.status().waiting_gates] == ["second"]


def test_yaml_lifecycle_rejects_review_and_gates_together(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="use either 'review' .* or 'gates'"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                lifecycle="""
                enabled: true
                review:
                  after_step: first
                  user_decisions:
                    go: { route: second }
                gates:
                  - id: g1
                    after_step: first
                    user_decisions:
                      go: { route: second }
                """,
            )
        )


def test_yaml_lifecycle_rejects_duplicate_gate_ids(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="duplicate gate id 'g1'"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                lifecycle="""
                enabled: true
                gates:
                  - id: g1
                    after_step: first
                    user_decisions:
                      go: { route: second }
                  - id: g1
                    after_step: second
                    user_decisions:
                      ok: { route: second }
                """,
            )
        )


def test_yaml_lifecycle_rejects_gates_sharing_after_step(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="shares after_step 'first' with an earlier gate"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                lifecycle="""
                enabled: true
                gates:
                  - id: g1
                    after_step: first
                    user_decisions:
                      go: { route: second }
                  - id: g2
                    after_step: first
                    user_decisions:
                      ok: { route: second }
                """,
            )
        )


def test_yaml_lifecycle_gates_route_validation_is_gate_scoped(tmp_path: Path) -> None:
    with pytest.raises(Exception, match=r"gates\['g1'\].user_decisions .* routes to unknown step"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                lifecycle="""
                enabled: true
                gates:
                  - id: g1
                    after_step: first
                    user_decisions:
                      go: { route: nowhere }
                """,
            )
        )


def test_yaml_lifecycle_gates_rejects_divergent_decision_reuse(tmp_path: Path) -> None:
    # DS4-6: the same decision name on two gates with DIFFERENT routes is rejected at load
    # (the CP's flat valid_user_decisions union would silently advertise the later route).
    with pytest.raises(Exception, match=r"decision 'ok' routes to 'second' on gate 'g1' but"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                lifecycle="""
                enabled: true
                gates:
                  - id: g1
                    after_step: first
                    user_decisions:
                      ok: { route: second }
                  - id: g2
                    after_step: second
                    user_decisions:
                      ok: { route: first }
                """,
            )
        )


def test_yaml_lifecycle_gates_allows_identical_decision_reuse() -> None:
    # DS4-6: identical route semantics across gates stay allowed (well-defined union).
    # Validated at the lifecycle level (route ORDER is a separate WorkflowSpec check).
    from typeflux.yaml.spec import (
        WorkflowLifecycleGateSpec,
        WorkflowLifecycleReviewRouteSpec,
        WorkflowLifecycleSpec,
    )

    lifecycle = WorkflowLifecycleSpec(
        enabled=True,
        gates=[
            WorkflowLifecycleGateSpec(
                id="g1",
                after_step="a",
                user_decisions={"skip": WorkflowLifecycleReviewRouteSpec(route="z")},
            ),
            WorkflowLifecycleGateSpec(
                id="g2",
                after_step="b",
                user_decisions={"skip": WorkflowLifecycleReviewRouteSpec(route="z")},
            ),
        ],
    )
    assert [gate.id for gate in lifecycle.gates] == ["g1", "g2"]


def test_policy_review_check_generalizes_over_gates(tmp_path: Path) -> None:
    # #55 slice 4 review round (item 4): a `gates` workflow satisfies require_review_routes;
    # a gateless one still fails; the invalid_user_decision constraint quantifies per gate.
    from typeflux.project import policy_enforcement

    gated = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            yaml_name="gated_policy",
            lifecycle="""
            enabled: true
            gates:
              - id: g1
                after_step: first
                user_decisions:
                  ok: { route: second }
                invalid_user_decision: warn
            """,
        )
    )
    gateless = load_yaml_spec(_write_demo_yaml(tmp_path, yaml_name="gateless_policy"))
    resolved_gated = SimpleNamespace(spec=gated)
    resolved_gateless = SimpleNamespace(spec=gateless)
    routes_required = {"review": {"require_review_routes": True}}
    assert policy_enforcement._validate_review(resolved_gated, routes_required).status == "passed"
    check = policy_enforcement._validate_review(resolved_gateless, routes_required)
    assert check.status == "failed"
    assert "review routes are required" in (check.message or "")
    # The per-gate invalid_user_decision constraint names the offending gate.
    must_fail = {"review": {"invalid_user_decision": "fail"}}
    check = policy_enforcement._validate_review(resolved_gated, must_fail)
    assert check.status == "failed"
    assert "gate 'g1'" in (check.message or "")


def test_effective_valid_user_decisions_prefers_execution_reported() -> None:
    # #55 §6 (item 11): the execution-reported waiting-gate decisions win over the resolved
    # spec (drift case); the resolved fallback is a THUNK, only invoked when no gate waits.
    from typeflux.core.contracts import WaitingGate, WorkflowLifecycleStatus
    from typeflux.project.operations import effective_valid_user_decisions

    waiting = WorkflowLifecycleStatus(
        state="waiting_for_review",
        waiting_gates=(
            WaitingGate(
                gate_id="final",
                after_step="b",
                valid_user_decisions={"release": "finalize"},
            ),
        ),
    )

    def _resolved_must_not_run() -> dict[str, str]:
        raise AssertionError("resolved-spec fallback must not be computed on the waiting path")

    assert effective_valid_user_decisions(waiting, _resolved_must_not_run) == {
        "release": "finalize"
    }
    # No waiting gate: the resolved-spec fallback applies.
    idle = WorkflowLifecycleStatus(state="running")
    assert effective_valid_user_decisions(idle, lambda: {"approve": "s2"}) == {"approve": "s2"}


def test_bundle_projections_cover_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #55 slice 4 review round (item 5): a multi-gate workflow projects review edges per gate
    # in the topology, and the bundle lifecycle carries the additive `gates` list while a
    # single-`review` bundle keeps its byte-identical `review` shape (gates excluded).
    from typeflux.project.bundle import _bundle_lifecycle, _bundle_topology

    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    gated = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            yaml_name="gated_bundle",
            lifecycle="""
            enabled: true
            gates:
              - id: g1
                after_step: first
                user_decisions:
                  ok: { route: second }
            """,
        )
    )
    workflow_cls = create_workflow(gated, collect_activities(gated))
    topology = _bundle_topology(workflow_cls, gated)
    review_edges = [edge for edge in topology.edges if edge.kind == "review"]
    assert [(edge.source, edge.target, edge.condition) for edge in review_edges] == [
        ("first", "second", "ok")
    ]
    lifecycle = _bundle_lifecycle(gated)
    assert lifecycle is not None and lifecycle.review is None
    assert [gate.id for gate in lifecycle.gates] == ["g1"]
    assert lifecycle.gates[0].user_decisions == {"ok": "second"}
    # Single-review spec: `review` populated, `gates` None (excluded on the wire).
    reviewed = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            yaml_name="reviewed_bundle",
            lifecycle="""
            enabled: true
            review:
              after_step: first
              user_decisions:
                ok: { route: second }
            """,
        )
    )
    single = _bundle_lifecycle(reviewed)
    assert single is not None and single.review is not None and single.gates is None
    assert "gates" not in single.model_dump(exclude_none=True)


def test_yaml_gates_digest_stable_and_distinct_from_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `gates` spec loads and digests deterministically; adding gates yields a DIFFERENT
    # digest than the equivalent single `review` (a distinct control-flow program), while the
    # single-`review` digest is unchanged from pre-slice-4 (pinned elsewhere).
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))

    def _digest(lifecycle: str) -> str:
        spec = load_yaml_spec(_write_demo_yaml(tmp_path, lifecycle=lifecycle))
        return create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

    gate_digest = _digest(
        """
        enabled: true
        gates:
          - id: review
            after_step: first
            user_decisions:
              go: { route: second }
        """
    )
    review_digest = _digest(
        """
        enabled: true
        review:
          after_step: first
          user_decisions:
            go: { route: second }
        """
    )
    # Deterministic (stable across recompute) and distinct between the two representations.
    assert gate_digest == _digest(
        """
        enabled: true
        gates:
          - id: review
            after_step: first
            user_decisions:
              go: { route: second }
        """
    )
    assert gate_digest != review_digest


@pytest.mark.asyncio
async def test_review_timeout_is_absolute_not_refreshed_by_signals(tmp_path: Path) -> None:
    # #297 review: a flood of signals that don't resolve the gate must NOT refresh
    # the timeout — the deadline is absolute. A production-style wait_condition that
    # raises asyncio.TimeoutError maps to the timeout action.

    rt = _review_timeout_lifecycle(tmp_path, "fail")

    async def wait_condition(predicate, timeout=None):
        # Simulate the durable timer firing regardless of signal churn.
        raise TimeoutError

    fake_workflow = SimpleNamespace(wait_condition=wait_condition, in_workflow=lambda: True)
    with pytest.raises(Exception, match="review timed out"):
        await workflow_module._maybe_wait_for_review(fake_workflow, rt, "first")


def test_label_override_registry_applies_label_when_no_selector() -> None:
    registry = _RecordingPromptRegistry()
    wrapped = _LabelOverrideRegistry(registry=registry, label="staging")

    resolved = wrapped.resolve(PromptRef("demo", prompt_type="chat"))

    assert registry.refs == [PromptRef("demo", label="staging", prompt_type="chat")]
    assert resolved.ref == registry.refs[0]


def test_label_override_registry_preserves_explicit_production_label() -> None:
    registry = _RecordingPromptRegistry()
    wrapped = _LabelOverrideRegistry(registry=registry, label="staging")

    resolved = wrapped.resolve(PromptRef("demo", label="production"))

    assert registry.refs == [PromptRef("demo", label="production")]
    assert resolved.ref == registry.refs[0]


def test_label_override_registry_preserves_non_default_label() -> None:
    registry = _RecordingPromptRegistry()
    wrapped = _LabelOverrideRegistry(registry=registry, label="staging")

    resolved = wrapped.resolve(PromptRef("demo", label="canary"))

    assert registry.refs == [PromptRef("demo", label="canary")]
    assert resolved.ref == registry.refs[0]


def test_label_override_registry_preserves_pinned_registry_version() -> None:
    registry = _RecordingPromptRegistry()
    wrapped = _LabelOverrideRegistry(registry=registry, label="staging")

    resolved = wrapped.resolve(PromptRef("demo", version=7))

    assert registry.refs == [PromptRef("demo", version=7)]
    assert resolved.ref == registry.refs[0]


def test_prompt_ref_rejects_version_and_label_together() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        PromptRef("demo", version=7, label="production")


def test_yaml_activities_requires_at_least_one_source(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        project: demo
        name: demo
        task_queue: demo-task-queue
        runtime:
          registry:
            type: inline
            prompts: {}
          provider:
            type: fake
        activities: {}
        workflow:
          name: DemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
            - id: first
              activity: first
        """,
    )

    with pytest.raises(ValueError, match="activities must define modules, definitions, or both"):
        load_yaml_spec(path)


def test_collect_activities_uses_all_activities_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))

    activities = collect_activities(spec)

    assert list(activities) == ["first", "second"]
    assert activities["first"].definition_source.kind == "python"
    assert activities["first"].definition_source.module == "yaml_demo_project.activities"
    assert activities["first"].definition_source.export == "ALL_ACTIVITIES"


def test_collect_activities_prefers_typeflux_activities_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="TYPEFLUX_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))

    activities = collect_activities(spec)

    assert list(activities) == ["first", "second"]
    assert activities["first"].definition_source.kind == "python"
    assert activities["first"].definition_source.export == "TYPEFLUX_ACTIVITIES"


def test_collect_activities_falls_back_to_module_values_and_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name=None)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules="""
            - module: activities
              include: [second]
            """,
            steps="""
            - id: second
              activity: second
            """,
            workflow_input="schemas:MiddleModel",
        )
    )

    activities = collect_activities(spec)

    assert list(activities) == ["second"]
    assert activities["second"].definition_source.kind == "python"
    assert activities["second"].definition_source.export == "module_values"


def _write_temporal_activity_project(
    tmp_path: Path,
    *,
    export_name: str | None = None,
    untyped: bool = False,
) -> None:
    for name in tuple(sys.modules):
        if name == "yaml_temporal_project" or name.startswith("yaml_temporal_project."):
            del sys.modules[name]
    package = tmp_path / "yaml_temporal_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        dedent(
            """
            from pydantic import BaseModel

            class InputModel(BaseModel):
                value: str

            class MiddleModel(BaseModel):
                value: str

            class OutputModel(BaseModel):
                value: str
            """
        ),
        encoding="utf-8",
    )
    normalize_fn = (
        dedent(
            """
            @activity.defn(name="normalize")
            async def normalize(value, extra=None):
                return value
            """
        )
        if untyped
        else dedent(
            """
            @activity.defn(name="normalize")
            async def normalize(value: MiddleModel) -> OutputModel:
                return OutputModel(value=value.value.strip().lower())
            """
        )
    )
    export_line = "" if export_name is None else f"{export_name} = (first, normalize)"
    (package / "activities.py").write_text(
        dedent(
            """
            from temporalio import activity

            from typeflux.core import AIActivity, PromptRef
            from yaml_temporal_project.schemas import InputModel, MiddleModel, OutputModel

            first = AIActivity(
                name="first",
                input_type=InputModel,
                output_type=MiddleModel,
                prompt_ref=PromptRef("first"),
            )
            """
        )
        + normalize_fn
        + f"\n{export_line}\n",
        encoding="utf-8",
    )


def _write_temporal_demo_yaml(tmp_path: Path, *, steps: str | None = None) -> Path:
    resolved_steps = (
        steps
        or """
            - id: first
              activity: first
            - id: normalize
              activity: normalize"""
    )
    return _write_yaml(
        tmp_path,
        f"""
        project: yaml_temporal_project
        name: temporal_demo
        task_queue: temporal-demo-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              first: first {{{{value}}}}
          provider:
            type: fake
        activities:
          modules: [activities]
        workflow:
          name: TemporalDemoWorkflow
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:{resolved_steps}
        """,
    )


def test_collect_activities_discovers_temporal_activities_from_module_globals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_temporal_activity_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_temporal_demo_yaml(tmp_path), load_dotenv=False)

    activities = collect_activities(spec)

    assert set(activities) == {"first", "normalize"}
    assert isinstance(activities["first"], AIActivity)
    descriptor = activities["normalize"]
    assert isinstance(descriptor, TemporalActivityDescriptor)
    assert descriptor.name == "normalize"
    assert descriptor.input_type.__name__ == "MiddleModel"
    assert descriptor.output_type.__name__ == "OutputModel"
    assert descriptor.start_to_close_timeout is None
    assert descriptor.retry_policy is None
    assert descriptor.task_queue is None
    assert descriptor.definition_source.to_dict() == {
        "kind": "python",
        "module": "yaml_temporal_project.activities",
        "export": "module_values",
    }


def test_collect_activities_discovers_temporal_activities_from_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_temporal_activity_project(tmp_path, export_name="TYPEFLUX_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_temporal_demo_yaml(tmp_path), load_dotenv=False)

    activities = collect_activities(spec)

    assert set(activities) == {"first", "normalize"}
    assert isinstance(activities["normalize"], TemporalActivityDescriptor)
    assert activities["normalize"].definition_source.export == "TYPEFLUX_ACTIVITIES"


def test_collect_activities_rejects_untyped_temporal_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_temporal_activity_project(tmp_path, untyped=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_temporal_demo_yaml(tmp_path), load_dotenv=False)

    with pytest.raises(TypeError, match="exactly one typed input"):
        collect_activities(spec)


def test_workflow_graph_validates_temporal_activity_type_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_temporal_activity_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    # normalize expects MiddleModel; putting it first breaks the chain.
    spec = load_yaml_spec(
        _write_temporal_demo_yaml(
            tmp_path,
            steps="""
            - id: normalize
              activity: normalize
            - id: first
              activity: first""",
        ),
        load_dotenv=False,
    )
    activities = collect_activities(spec)

    with pytest.raises(TypeError, match="expects MiddleModel"):
        create_workflow(spec, activities)


@pytest.mark.asyncio
async def test_generated_workflow_executes_ai_and_temporal_activity_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import temporalio.workflow

    _write_temporal_activity_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_temporal_demo_yaml(tmp_path), load_dotenv=False)
    activities = collect_activities(spec)
    workflow_cls = create_workflow(spec, activities)

    from yaml_temporal_project.schemas import InputModel, MiddleModel, OutputModel

    scheduled: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        scheduled.append(name)
        if name == "first":
            return MiddleModel(value=f"  {arg.value.upper()}  ")
        if name == "normalize":
            return OutputModel(value=arg.value.strip().lower())
        raise AssertionError(f"unexpected activity: {name}")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    result = await workflow_cls().run(InputModel(value="hello"))

    assert result == OutputModel(value="hello")
    assert scheduled == ["first", "normalize"]


def test_map_steps_reject_plain_temporal_activities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Map calls pass (item, MapActivityContext); only the AI wrapper accepts
    # the context, so a plain target must fail at build time, not runtime.
    _write_temporal_activity_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_yaml(
            tmp_path,
            """
            project: yaml_temporal_project
            name: temporal_map_demo
            task_queue: temporal-demo-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts: {}
              provider:
                type: fake
            activities:
              modules: [activities]
            workflow:
              name: TemporalMapDemoWorkflow
              input: schemas:MapInput
              output: schemas:MapOutput
              steps:
                - id: normalize_all
                  map:
                    activity: normalize
                    over: input.items
                    concurrency: 2
                    collect:
                      output: schemas:MapOutput
                      field: items
            """,
        ),
        load_dotenv=False,
    )
    schemas = tmp_path / "yaml_temporal_project" / "schemas.py"
    schemas.write_text(
        schemas.read_text()
        + dedent(
            """

            class MapInput(BaseModel):
                items: list[MiddleModel]

            class MapOutput(BaseModel):
                items: list[OutputModel]
            """
        ),
        encoding="utf-8",
    )
    for name in tuple(sys.modules):
        if name.startswith("yaml_temporal_project"):
            del sys.modules[name]
    activities = collect_activities(spec)

    with pytest.raises(TypeError, match="map steps support AI activities only"):
        create_workflow(spec, activities)


def test_bundle_activities_serialize_plain_temporal_activities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typeflux.project.bundle import _bundle_activities

    _write_temporal_activity_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_temporal_demo_yaml(tmp_path), load_dotenv=False)
    activities = collect_activities(spec)
    workflow_cls = create_workflow(spec, activities)

    descriptors = {item.name: item for item in _bundle_activities(activities, workflow_cls)}

    assert descriptors["first"].kind == "ai"
    assert descriptors["first"].prompt_ref == {"name": "first", "version": None, "label": None}
    assert descriptors["normalize"].kind == "temporal"
    assert descriptors["normalize"].prompt_ref is None
    assert descriptors["normalize"].validation_retries is None
    assert descriptors["normalize"].artifact_inputs == ()
    assert descriptors["normalize"].used_by_steps == ("normalize",)
    assert descriptors["normalize"].input_schema["name"] == "MiddleModel"


def test_collect_activities_supports_absolute_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            imports:
              allow_absolute_activity_modules: true
            """,
            modules="""
            - module: yaml_demo_project.activities
              absolute: true
              exclude: [first]
            """,
            steps="""
            - id: second
              activity: second
            """,
            workflow_input="schemas:MiddleModel",
        )
    )

    activities = collect_activities(spec)

    assert list(activities) == ["second"]


@pytest.mark.parametrize("ref", ["BaseModel", "pydantic:", ""])
def test_import_type_ref_requires_module_name_syntax(ref: str) -> None:
    with pytest.raises(ValueError, match="module:Name syntax"):
        import_type_ref("yaml_demo_project", ref)


def test_import_type_ref_rejects_non_type_target() -> None:
    with pytest.raises(TypeError, match="did not resolve to a type"):
        import_type_ref("pydantic", "pydantic:VERSION")


@pytest.mark.parametrize("path", ["loads", "json:", ""])
def test_import_object_requires_module_name_syntax(path: str) -> None:
    with pytest.raises(ValueError, match="module:Name syntax"):
        import_object(path)


def test_import_object_resolves_dotted_attribute() -> None:
    assert import_object("json:JSONDecoder.__name__") == "JSONDecoder"


def test_collect_activities_rejects_absolute_modules_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules="""
            - module: yaml_demo_project.activities
              absolute: true
            """,
        )
    )

    with pytest.raises(ValueError, match="allow_absolute_activity_modules"):
        collect_activities(spec)


def test_collect_activities_rejects_absolute_modules_outside_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            imports:
              allow_absolute_activity_modules: true
              allowed_module_roots:
                - approved_shared
            """,
            modules="""
            - module: shared_ai.activities
              absolute: true
            """,
        )
    )

    with pytest.raises(ValueError, match="allowed_module_roots"):
        collect_activities(spec)


def test_collect_activities_allows_absolute_modules_under_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    shared = tmp_path / "shared_ai"
    shared.mkdir()
    (shared / "__init__.py").write_text("", encoding="utf-8")
    (shared / "activities.py").write_text(
        dedent(
            """
            from typeflux.core import AIActivity, PromptRef
            from yaml_demo_project.schemas import MiddleModel, OutputModel

            shared_second = AIActivity(
                name="shared_second",
                input_type=MiddleModel,
                output_type=OutputModel,
                prompt_ref=PromptRef("second"),
            )

            ALL_ACTIVITIES = (shared_second,)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                second: second {{value}}
            provider:
              type: fake
            imports:
              allow_absolute_activity_modules: true
              allowed_module_roots:
                - shared_ai
            """,
            modules="""
            - module: shared_ai.activities
              absolute: true
            """,
            steps="""
            - id: shared_second
              activity: shared_second
            """,
            workflow_input="schemas:MiddleModel",
        )
    )

    activities = collect_activities(spec)

    assert list(activities) == ["shared_second"]


def test_collect_activities_fails_on_duplicate_activity_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    package = tmp_path / "yaml_demo_project"
    (package / "more.py").write_text(
        dedent(
            """
            from typeflux.core import AIActivity, PromptRef
            from yaml_demo_project.schemas import InputModel, MiddleModel

            duplicate = AIActivity(
                name="first",
                input_type=InputModel,
                output_type=MiddleModel,
                prompt_ref=PromptRef("duplicate"),
            )
            ALL_ACTIVITIES = (duplicate,)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules="""
            - activities
            - more
            """,
        )
    )

    with pytest.raises(ValueError, match="duplicate activity name: first"):
        collect_activities(spec)


def test_collect_activities_supports_yaml_definitions_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions="""
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: first
              validation_retries: 2
              start_to_close_timeout_seconds: 45
              heartbeat_timeout_seconds: 15
            - name: second
              input: schemas:MiddleModel
              output: schemas:OutputModel
              prompt:
                name: second
                label: staging
                type: chat
            """,
        )
    )

    activities = collect_activities(spec)

    assert list(activities) == ["first", "second"]
    assert activities["first"].prompt_ref == PromptRef("first")
    assert activities["first"].validation_retries == 2
    assert activities["first"].start_to_close_timeout is not None
    assert activities["first"].start_to_close_timeout.total_seconds() == 45
    assert activities["first"].heartbeat_timeout is not None
    assert activities["first"].heartbeat_timeout.total_seconds() == 15
    assert activities["second"].heartbeat_timeout is None
    assert activities["first"].definition_source.to_dict() == {
        "kind": "yaml",
        "yaml_project": "yaml_demo_project",
        "yaml_name": "demo_yaml",
    }
    assert activities["second"].prompt_ref == PromptRef(
        "second",
        label="staging",
        prompt_type="chat",
    )
    assert activities["second"].prompt_ref.prompt_type == "chat"
    assert activities["first"].hook is None
    assert activities["second"].hook is None


def test_yaml_definitions_support_artifact_inputs_and_content_part_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    path = _write_demo_yaml(
        tmp_path,
        modules=None,
        runtime="""
        temporal:
          address: localhost:7233
        artifacts:
          local_roots: [fixtures]
          allowed_media_types: [text/plain]
        registry:
          type: inline
          prompts:
            first:
              messages:
                - role: user
                  content:
                    - type: text
                      text: Review {{ value }}.
                    - type: artifact_group
                      group: claim_documents
        provider:
          type: fake
        """,
        definitions="""
        - name: first
          input: schemas:InputModel
          output: schemas:MiddleModel
          prompt: first
          artifacts:
            - name: claim_documents
              from: input.value
              kind: data
              media_types: [text/plain]
              max_count: 2
              attach:
                role: user
                text: Attached evidence.
        """,
        steps="""
        - id: first
          activity: first
        """,
        workflow_output="schemas:MiddleModel",
    )

    spec = load_yaml_spec(path)
    activity = collect_activities(spec)["first"]
    prompt = _build_registry(spec).resolve(PromptRef("first"))
    policy = _build_artifact_policy(spec)

    assert activity.artifact_inputs[0].name == "claim_documents"
    assert activity.artifact_inputs[0].from_path == "input.value"
    assert activity.artifact_inputs[0].attach is not None
    assert prompt.messages[0].content == (
        TextPart("Review {{ value }}."),
        ArtifactGroupPart(group="claim_documents"),
    )
    assert policy.local_roots == (fixtures.resolve(),)


def test_multimodal_claim_review_example_yaml_loads() -> None:
    path = Path("examples/multimodal_claim_review/typeflux.yaml")

    spec = load_yaml_spec(path, load_dotenv=False)
    activities = collect_activities(spec)
    policy = _build_artifact_policy(spec)

    assert list(activities) == ["review_claim"]
    assert len(activities["review_claim"].artifact_inputs) == 2
    assert policy.local_roots == ((path.resolve().parent / "fixtures").resolve(),)


def test_collect_activities_tracks_yaml_prompt_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions="""
            - name: string_prompt
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: string-prompt
            - name: explicit_text
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: text-prompt
                type: text
            - name: explicit_chat
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: chat-prompt
                type: chat
            """,
        )
    )

    activities = collect_activities(spec)

    assert activities["string_prompt"].prompt_ref.prompt_type == "auto"
    assert activities["explicit_text"].prompt_ref == PromptRef("text-prompt", prompt_type="text")
    assert activities["explicit_chat"].prompt_ref == PromptRef("chat-prompt", prompt_type="chat")


def test_collect_activities_tracks_yaml_prompt_selectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions="""
            - name: string_prompt
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: string-prompt
            - name: no_selector
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: no-selector
            - name: explicit_production
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: explicit-production
                label: production
            - name: explicit_canary
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: explicit-canary
                label: canary
            - name: pinned_version
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: pinned-version
                version: 7
            """,
        )
    )

    activities = collect_activities(spec)

    assert activities["string_prompt"].prompt_ref == PromptRef("string-prompt")
    assert activities["no_selector"].prompt_ref == PromptRef("no-selector")
    assert activities["explicit_production"].prompt_ref == PromptRef(
        "explicit-production",
        label="production",
    )
    assert activities["explicit_canary"].prompt_ref == PromptRef(
        "explicit-canary",
        label="canary",
    )
    assert activities["pinned_version"].prompt_ref == PromptRef(
        "pinned-version",
        version=7,
    )


def test_yaml_prompt_ref_rejects_string_version_and_dual_selectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="use 'label: production'"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                modules=None,
                definitions="""
                - name: first
                  input: schemas:InputModel
                  output: schemas:MiddleModel
                  prompt:
                    name: first
                    version: production
                """,
            )
        )

    with pytest.raises(ValueError, match="mutually exclusive"):
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                modules=None,
                definitions="""
                - name: first
                  input: schemas:InputModel
                  output: schemas:MiddleModel
                  prompt:
                    name: first
                    version: 7
                    label: production
                """,
            )
        )


def test_collect_activities_supports_mixed_modules_and_yaml_definitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules="""
            - module: activities
              include: [first]
            """,
            definitions="""
            - name: second
              input: schemas:MiddleModel
              output: schemas:OutputModel
              prompt: second
            """,
        )
    )

    activities = collect_activities(spec)

    assert list(activities) == ["first", "second"]
    assert activities["first"].prompt_ref == PromptRef("first")
    assert activities["second"].prompt_ref == PromptRef("second")


def test_collect_activities_fails_on_duplicate_yaml_definition_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            definitions="""
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: first
            """,
        )
    )

    with pytest.raises(ValueError, match="duplicate activity name: first"):
        collect_activities(spec)


def test_collect_activities_fails_on_missing_yaml_definition_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions="""
            - name: first
              input: schemas:MissingModel
              output: schemas:MiddleModel
              prompt: first
            """,
        )
    )

    with pytest.raises(AttributeError, match="MissingModel"):
        collect_activities(spec)


def test_collect_activities_fails_on_non_pydantic_yaml_definition_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions="""
            - name: first
              input: schemas:PlainModel
              output: schemas:MiddleModel
              prompt: first
            """,
        )
    )

    with pytest.raises(TypeError, match="input_type must be a Pydantic BaseModel subclass"):
        collect_activities(spec)


def test_create_workflow_fails_on_unknown_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            steps="""
            - id: missing
              activity: missing
            """,
        )
    )

    with pytest.raises(ValueError, match="unknown activity"):
        create_workflow(spec, collect_activities(spec))


def test_create_workflow_fails_on_duplicate_step_ids(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        steps="""
        - id: duplicate
          activity: first
        - id: duplicate
          activity: second
        """,
    )

    with pytest.raises(ValueError, match="duplicate workflow step id"):
        load_yaml_spec(path)


def test_create_workflow_fails_on_reserved_step_id(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        steps="""
        - id: input
          activity: first
        """,
    )

    with pytest.raises(ValueError, match="reserved workflow step id"):
        load_yaml_spec(path)


def test_create_workflow_validates_implicit_type_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            steps="""
            - id: second
              activity: second
            - id: first
              activity: first
            """,
        )
    )

    with pytest.raises(TypeError, match="expects MiddleModel"):
        create_workflow(spec, collect_activities(spec))


def test_yaml_map_step_requires_explicit_concurrency(tmp_path: Path) -> None:
    path = _write_map_demo_yaml(
        tmp_path,
        steps="""
        - id: review_pages
          map:
            activity: review_page
            over: input.pages
            collect:
              output: schemas:PageReviewBatch
              field: reviews
        """,
    )

    with pytest.raises(ValueError, match="concurrency"):
        load_yaml_spec(path)


def test_yaml_map_step_requires_collect_field(tmp_path: Path) -> None:
    path = _write_map_demo_yaml(
        tmp_path,
        steps="""
        - id: review_pages
          map:
            activity: review_page
            over: input.pages
            concurrency: 2
            collect:
              output: schemas:PageReviewBatch
        """,
    )

    with pytest.raises(ValueError, match="field"):
        load_yaml_spec(path)


def test_yaml_map_step_requires_collect_output(tmp_path: Path) -> None:
    path = _write_map_demo_yaml(
        tmp_path,
        steps="""
        - id: review_pages
          map:
            activity: review_page
            over: input.pages
            concurrency: 2
            collect:
              field: reviews
        """,
    )

    with pytest.raises(ValueError, match="output"):
        load_yaml_spec(path)


def test_yaml_map_step_rejects_invalid_concurrency(tmp_path: Path) -> None:
    path = _write_map_demo_yaml(
        tmp_path,
        steps="""
        - id: review_pages
          map:
            activity: review_page
            over: input.pages
            concurrency: 0
            collect:
              output: schemas:PageReviewBatch
              field: reviews
        """,
    )

    with pytest.raises(ValueError, match="map.concurrency must be >= 1"):
        load_yaml_spec(path)


def test_create_workflow_validates_map_step_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            steps="""
            - id: review_pages
              map:
                activity: review_page
                over: input.value
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            """,
        )
    )

    with pytest.raises(TypeError, match="map.over must resolve to a list field"):
        create_workflow(spec, collect_activities(spec))


def test_create_workflow_validates_map_over_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            steps="""
            - id: review_pages
              map:
                activity: review_page
                over: input.missing_pages
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            """,
        )
    )

    with pytest.raises(ValueError, match="unknown field 'missing_pages'"):
        create_workflow(spec, collect_activities(spec))


def test_create_workflow_validates_map_collect_field_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            steps="""
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 2
                collect:
                  output: schemas:WrongReviewBatch
                  field: reviews
            """,
        )
    )

    with pytest.raises(TypeError, match="expects MiddleModel, but activity returns PageReview"):
        create_workflow(spec, collect_activities(spec))


@pytest.mark.asyncio
async def test_map_collect_payload_guard_bounds_collected_size() -> None:
    class ReviewBatch(BaseModel):
        reviews: list[str]

    class ImmediateWorkflow:
        def start_activity(self, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
            del args, kwargs
            future: asyncio.Future[str] = asyncio.Future()
            future.set_result("x" * 100)
            return future

    def map_call(max_bytes: int) -> workflow_module.MapCallSpec:
        return workflow_module.MapCallSpec(
            step_id="review_pages",
            activity_name="review_page",
            over="input.pages",
            concurrency=2,
            collect=workflow_module.MapCollectCallSpec(
                output_type=ReviewBatch,
                field="reviews",
                max_bytes=max_bytes,
            ),
            task_queue=None,
            start_to_close_timeout=workflow_module.DEFAULT_START_TO_CLOSE_TIMEOUT,
            retry_policy=None,
        )

    with pytest.raises(Exception, match="collected payload is .* exceeding") as exc_info:
        await workflow_module._execute_map_call(
            ImmediateWorkflow(), map_call(max_bytes=64), ["a", "b"]
        )
    assert "review_pages" in str(exc_info.value)

    # max_bytes 0 disables the guard.
    collected = await workflow_module._execute_map_call(
        ImmediateWorkflow(), map_call(max_bytes=0), ["a", "b"]
    )
    assert len(collected.reviews) == 2


def test_yaml_map_collect_max_bytes_defaults_and_validates(tmp_path: Path) -> None:
    spec = load_yaml_spec(_write_map_demo_yaml(tmp_path))
    step = spec.workflow.steps[0]
    assert step.map.collect.max_bytes == 1_500_000

    invalid = _write_map_demo_yaml(
        tmp_path,
        steps="""
        - id: review_pages
          map:
            activity: review_page
            over: input.pages
            concurrency: 2
            collect:
              output: schemas:PageReviewBatch
              field: reviews
              max_bytes: -1
        - id: consolidate
          activity: consolidate_reviews
        """,
    )
    with pytest.raises(ValueError, match="max_bytes must be >= 0"):
        load_yaml_spec(invalid)


def test_yaml_load_bounds_document_size_and_alias_count() -> None:
    from typeflux.yaml.loader import MAX_YAML_BYTES, strict_safe_load

    with pytest.raises(ValueError, match="byte limit"):
        strict_safe_load("padding: " + "x" * MAX_YAML_BYTES)

    alias_bomb = "base: &a [1, 2]\nitems:\n" + "".join("  - *a\n" for _ in range(1001))
    with pytest.raises(Exception, match="alias limit"):
        strict_safe_load(alias_bomb)


@pytest.mark.asyncio
async def test_execute_map_call_awaits_cancelled_siblings_before_reraising() -> None:
    class ReviewBatch(BaseModel):
        reviews: list[str]

    class DelayedCancellationHandle(asyncio.Future[Any]):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_requested = False
            self.cancel_acknowledged = False

        def cancel(self, msg: Any = None) -> bool:
            del msg
            if self.done():
                return False
            self.cancel_requested = True
            asyncio.get_running_loop().call_soon(self._acknowledge_cancellation)
            return True

        def _acknowledge_cancellation(self) -> None:
            self.cancel_acknowledged = True
            if not self.done():
                self.set_exception(asyncio.CancelledError())

    failed_handle: asyncio.Future[str] = asyncio.Future()
    failed_handle.set_exception(RuntimeError("first page failed"))
    sibling_handle = DelayedCancellationHandle()
    handles: list[asyncio.Future[Any]] = [failed_handle, sibling_handle]

    class FakeWorkflow:
        def start_activity(self, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
            del args, kwargs
            return handles.pop(0)

    call = workflow_module.MapCallSpec(
        step_id="review_pages",
        activity_name="review_page",
        over="input.pages",
        concurrency=2,
        collect=workflow_module.MapCollectCallSpec(
            output_type=ReviewBatch,
            field="reviews",
        ),
        task_queue=None,
        start_to_close_timeout=workflow_module.DEFAULT_START_TO_CLOSE_TIMEOUT,
        retry_policy=None,
    )

    with pytest.raises(RuntimeError, match="first page failed"):
        await workflow_module._execute_map_call(
            FakeWorkflow(),
            call,
            ["first", "second"],
        )

    assert sibling_handle.cancel_requested is True
    assert sibling_handle.cancel_acknowledged is True


@pytest.mark.asyncio
async def test_subworkflow_map_same_batch_failure_still_drains_batch_successes() -> None:
    # #787 (ported from the activity map's #299 hardening): at concurrency 3 all three
    # children settle in the SAME asyncio.wait(FIRST_COMPLETED) batch. Two succeed, one
    # fails. The drain must record every SUCCESS in the batch (its lifecycle progress
    # tick) BEFORE propagating the failure — a batch's successes are never discarded by
    # a racing sibling failure. Load-bearing the day map.workflow grows per-item
    # compensation; TS needs no twin (the shared fanOut settles every sibling by
    # construction and documents the #299 invariant).
    class ChildBatch(BaseModel):
        results: list[Any]

    class FakeInfo:
        workflow_id = "parent-wf"
        typed_search_attributes = None

    class FakeWorkflow:
        def info(self) -> FakeInfo:
            return FakeInfo()

        async def execute_child_workflow(self, wf_type: str, item: Any, **kwargs: Any) -> str:
            # No await point: all three children settle in ONE loop tick -> same batch.
            del wf_type, kwargs
            if item == "boom":
                raise RuntimeError("boom child failed")
            return f"r-{item}"

    class RecordingLifecycle:
        cancellation_enabled = False
        cancellation_requested = False

        def __init__(self) -> None:
            self.ticks: list[str] = []

        def unit_completed(self, workflow: Any, step_id: str) -> None:
            del workflow
            self.ticks.append(step_id)

    call = workflow_module.MapSubworkflowCallSpec(
        step_id="fan",
        child_workflow_id="child",
        child_workflow_type="ChildType",
        child_workflow_name="child",
        child_project="proj",
        child_digest="digest",
        output_type=ChildBatch,
        over="input.items",
        concurrency=3,
        collect=workflow_module.MapCollectCallSpec(
            output_type=ChildBatch,
            field="results",
            max_bytes=0,
        ),
        search_attributes=None,
        id_reuse_policy=None,
        parent_close_policy=None,
    )
    lifecycle = RecordingLifecycle()

    with pytest.raises(RuntimeError, match="boom child failed"):
        await workflow_module._execute_subworkflow_map_call(
            FakeWorkflow(),
            call,
            ["ok0", "boom", "ok2"],
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )

    # BOTH batch successes ticked progress before the failure propagated.
    assert lifecycle.ticks == ["fan", "fan"]


@pytest.mark.asyncio
async def test_generated_workflow_runs_bounded_map_and_explicit_consolidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_map_demo_yaml(tmp_path))
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_map_project.schemas import FinalReview, InputModel, PageReview, PageReviewBatch

    active = 0
    max_active = 0
    activity_ids: list[str] = []

    async def fake_map_activity(name: str, arg: Any, map_context: Any, **kwargs: Any) -> Any:
        nonlocal active, max_active
        activity_ids.append(kwargs["activity_id"])
        assert map_context.map_step_id == "review_pages"
        assert map_context.map_index == len(activity_ids) - 1
        assert map_context.map_size == 3
        assert map_context.map_concurrency == 2
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return PageReview(value=f"{arg.value}:reviewed")
        finally:
            active -= 1

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        assert arg is None
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        assert name == "consolidate_reviews"
        assert isinstance(arg, PageReviewBatch)
        assert [review.value for review in arg.reviews] == [
            "first:reviewed",
            "second:reviewed",
            "third:reviewed",
        ]
        return FinalReview(value=" / ".join(review.value for review in arg.reviews))

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)
    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    result = await workflow_cls().run(
        InputModel(
            value="root",
            pages=[
                {"value": "first"},
                {"value": "second"},
                {"value": "third"},
            ],
        )
    )

    assert result == FinalReview(value="first:reviewed / second:reviewed / third:reviewed")
    assert max_active == 2
    assert activity_ids == ["review_pages-0", "review_pages-1", "review_pages-2"]


@pytest.mark.asyncio
async def test_generated_workflow_cancels_queued_map_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            workflow_output="schemas:PageReviewBatch",
            steps="""
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 1
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_map_project.schemas import InputModel, PageReview

    started_activity_ids: list[str] = []
    activity_started = asyncio.Event()
    activity_cancelled = asyncio.Event()

    async def fake_map_activity(name: str, arg: Any, map_context: Any, **kwargs: Any) -> Any:
        started_activity_ids.append(kwargs["activity_id"])
        assert map_context.map_step_id == "review_pages"
        assert map_context.map_index == 0
        assert map_context.map_size == 3
        assert map_context.map_concurrency == 1
        activity_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            activity_cancelled.set()
            raise
        return PageReview(value=f"{arg.value}:reviewed")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        assert arg is None
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)

    run_task = asyncio.create_task(
        workflow_cls().run(
            InputModel(
                value="root",
                pages=[
                    {"value": "first"},
                    {"value": "second"},
                    {"value": "third"},
                ],
            )
        )
    )
    await asyncio.wait_for(activity_started.wait(), timeout=1)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await asyncio.wait_for(activity_cancelled.wait(), timeout=1)

    assert started_activity_ids == ["review_pages-0"]


@pytest.mark.asyncio
async def test_generated_workflow_cancels_running_map_items_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            workflow_output="schemas:PageReviewBatch",
            steps="""
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_map_project.schemas import InputModel, PageReview

    started_activity_ids: list[str] = []
    second_started = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def fake_map_activity(name: str, arg: Any, map_context: Any, **kwargs: Any) -> Any:
        activity_id = kwargs["activity_id"]
        started_activity_ids.append(activity_id)
        assert map_context.map_step_id == "review_pages"
        assert map_context.map_index == len(started_activity_ids) - 1
        assert map_context.map_size == 3
        assert map_context.map_concurrency == 2
        if activity_id == "review_pages-0":
            await second_started.wait()
            raise RuntimeError("first page failed")
        second_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            second_cancelled.set()
            raise
        return PageReview(value=f"{arg.value}:reviewed")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        assert arg is None
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)

    with pytest.raises(RuntimeError, match="first page failed"):
        await workflow_cls().run(
            InputModel(
                value="root",
                pages=[
                    {"value": "first"},
                    {"value": "second"},
                    {"value": "third"},
                ],
            )
        )
    await asyncio.wait_for(second_cancelled.wait(), timeout=1)

    assert started_activity_ids == ["review_pages-0", "review_pages-1"]


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_cancels_running_map_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            cancellation: true
            """,
            workflow_output="schemas:PageReviewBatch",
            steps="""
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_map_project.schemas import InputModel, PageReview

    started_activity_ids: list[str] = []
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def fake_map_activity(name: str, arg: Any, map_context: Any, **kwargs: Any) -> Any:
        activity_id = kwargs["activity_id"]
        started_activity_ids.append(activity_id)
        if activity_id == "review_pages-0":
            first_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if activity_id == "review_pages-0":
                first_cancelled.set()
            if activity_id == "review_pages-1":
                second_cancelled.set()
            raise
        return PageReview(value=f"{arg.value}:reviewed")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        assert arg is None
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(
        workflow_instance.run(
            InputModel(
                value="root",
                pages=[
                    {"value": "first"},
                    {"value": "second"},
                    {"value": "third"},
                ],
            )
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    workflow_instance.typeflux_request_cancel("cancel map")

    with pytest.raises(Exception, match="workflow cancellation requested"):
        await run_task

    status = workflow_instance.typeflux_lifecycle_status()
    assert started_activity_ids == ["review_pages-0", "review_pages-1"]
    assert first_cancelled.is_set()
    assert second_cancelled.is_set()
    assert status.state == "cancelled"
    assert status.completed_units == 0
    assert status.total_units == 3


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_counts_prior_step_map_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_map_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            cancellation: true
            """,
            workflow_output="schemas:PageReviewBatch",
            steps="""
            - id: select_pages
              activity: select_pages
            - id: review_pages
              map:
                activity: review_page
                over: select_pages.pages
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_map_project.schemas import InputModel, PageList, PageReview

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        assert name == "select_pages"
        assert kwargs["activity_id"] == "select_pages"
        return PageList(pages=arg.pages)

    async def fake_map_activity(name: str, arg: Any, map_context: Any, **kwargs: Any) -> Any:
        assert name == "review_page"
        assert map_context.map_step_id == "review_pages"
        return PageReview(value=f"{arg.value}:reviewed")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        assert arg is None
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)
    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)

    workflow_instance = workflow_cls()
    result = await workflow_instance.run(
        InputModel(
            value="root",
            pages=[
                {"value": "first"},
                {"value": "second"},
            ],
        )
    )

    status = workflow_instance.typeflux_lifecycle_status()
    assert [review.value for review in result.reviews] == ["first:reviewed", "second:reviewed"]
    assert status.state == "completed"
    assert status.completed_units == 3
    assert status.total_units == 3


def test_workflow_timestamp_has_no_wall_clock_fallback() -> None:
    class MissingWorkflowClock:
        pass

    class BadWorkflowClock:
        @staticmethod
        def now() -> str:
            return "not-a-datetime"

    assert workflow_module._workflow_timestamp(MissingWorkflowClock()) is None
    assert workflow_module._workflow_timestamp(BadWorkflowClock()) is None


def test_generated_workflow_uses_temporal_sandbox_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    workflow_cls = create_workflow(spec, collect_activities(spec))

    from temporalio import workflow

    definition = workflow._Definition.must_from_class(workflow_cls)

    assert definition.sandboxed is True


def test_build_provider_rejects_provider_class_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_recording_provider(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: custom
              class: yaml_demo_project.providers:RecordingProvider
            """,
        )
    )

    with pytest.raises(ValueError, match="allow_provider_class"):
        _build_provider(spec, enable_langfuse=False)


def test_build_provider_allows_provider_class_under_project_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_recording_provider(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: custom
              class: yaml_demo_project.providers:RecordingProvider
            imports:
              allow_provider_class: true
            """,
        )
    )

    provider = _build_provider(spec, enable_langfuse=False)

    assert type(provider).__name__ == "RecordingProvider"


def test_build_provider_rejects_provider_class_outside_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: custom
              class: shared_ai.providers:Provider
            imports:
              allow_provider_class: true
              allowed_module_roots:
                - approved_shared
            """,
        )
    )

    with pytest.raises(ValueError, match="allowed_module_roots"):
        _build_provider(spec, enable_langfuse=False)


def _write_custom_extensions(tmp_path: Path) -> None:
    """A custom prompt registry + observability backend in the project package."""
    package = tmp_path / "yaml_demo_project"
    (package / "extensions.py").write_text(
        dedent(
            """
            from typeflux.core.contracts import ChatMessage, ResolvedPrompt
            from typeflux.observability.backend import (
                NoOpTraceReader,
                NoOpTraceWriter,
            )

            class CustomRegistry:
                def resolve(self, ref):
                    return ResolvedPrompt(
                        ref=ref,
                        messages=(ChatMessage(role="user", content=f"custom {ref.name}"),),
                        resolved_version="custom",
                    )

            class CustomObservability:
                @property
                def writer(self):
                    return NoOpTraceWriter()

                @property
                def reader(self):
                    return NoOpTraceReader()
            """
        ),
        encoding="utf-8",
    )


def _custom_extension_runtime(*, kind: str, allow: bool, root: str | None = None) -> str:
    flag = {"registry": "allow_registry_class", "observability": "allow_observability_class"}[kind]
    block = {
        "registry": (
            "            registry:\n"
            "              type: custom\n"
            "              class: yaml_demo_project.extensions:CustomRegistry\n"
        ),
        "observability": (
            "            registry:\n"
            "              type: inline\n"
            "              prompts:\n"
            "                first: first {{value}}\n"
            "                second: second {{value}}\n"
            "            observability:\n"
            "              type: custom\n"
            "              class: yaml_demo_project.extensions:CustomObservability\n"
        ),
    }[kind]
    roots = f"\n              allowed_module_roots:\n                - {root}" if root else ""
    return (
        "\n            temporal:\n              address: localhost:7233\n"
        f"{block}"
        "            provider:\n              type: fake\n"
        f"            imports:\n              {flag}: {str(allow).lower()}{roots}\n"
    )


def test_build_registry_allows_custom_class_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_custom_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(tmp_path, runtime=_custom_extension_runtime(kind="registry", allow=True))
    )

    registry = _build_registry(spec)
    assert type(registry).__name__ == "CustomRegistry"
    assert registry.resolve(PromptRef("greeting")).messages[0].content == "custom greeting"


def test_build_registry_rejects_custom_class_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_custom_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(tmp_path, runtime=_custom_extension_runtime(kind="registry", allow=False))
    )

    with pytest.raises(ValueError, match="allow_registry_class"):
        _build_registry(spec)


def test_build_observability_allows_custom_class_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_custom_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path, runtime=_custom_extension_runtime(kind="observability", allow=True)
        )
    )

    backend = _build_observability(spec)
    assert type(backend).__name__ == "CustomObservability"


def test_build_observability_rejects_custom_class_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_custom_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path, runtime=_custom_extension_runtime(kind="observability", allow=False)
        )
    )

    with pytest.raises(ValueError, match="allow_observability_class"):
        _build_observability(spec)


# --- Custom-extension declared config (#792) -----------------------------------------


def _write_configured_extensions(tmp_path: Path) -> None:
    """Custom classes that ACCEPT a declared config block (keyword-only ``config``)."""
    package = tmp_path / "yaml_demo_project"
    (package / "configured_extensions.py").write_text(
        dedent(
            """
            from typeflux.core.contracts import ChatMessage, ResolvedPrompt
            from typeflux.observability.backend import (
                NoOpTraceReader,
                NoOpTraceWriter,
            )

            class ConfiguredRegistry:
                def __init__(self, *, config):
                    self.config = config

                def resolve(self, ref):
                    return ResolvedPrompt(
                        ref=ref,
                        messages=(
                            ChatMessage(
                                role="user",
                                content=f"{self.config['endpoint']} {ref.name}",
                            ),
                        ),
                        resolved_version="configured",
                    )

            class ConfiguredObservability:
                def __init__(self, *, config):
                    self.config = config

                @property
                def writer(self):
                    return NoOpTraceWriter()

                @property
                def reader(self):
                    return NoOpTraceReader()
            """
        ),
        encoding="utf-8",
    )


def _configured_registry_runtime(config_yaml: str) -> str:
    return (
        "\n            temporal:\n              address: localhost:7233\n"
        "            registry:\n"
        "              type: custom\n"
        "              class: yaml_demo_project.configured_extensions:ConfiguredRegistry\n"
        f"{config_yaml}"
        "            provider:\n              type: fake\n"
        "            imports:\n              allow_registry_class: true\n"
    )


def test_custom_registry_receives_resolved_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#792: a declared config block resolves (literals + value_from) and reaches the
    class as ``cls(config=...)`` — the end of ad-hoc env reads for custom extensions."""
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_configured_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CUSTOM_REGISTRY_TOKEN", "resolved-token")
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=_configured_registry_runtime(
                "              config:\n"
                "                endpoint: https://registry.internal\n"
                "                api_key:\n"
                "                  value_from:\n"
                "                    env: CUSTOM_REGISTRY_TOKEN\n"
            ),
        )
    )

    registry = _build_registry(spec)
    assert type(registry).__name__ == "ConfiguredRegistry"
    assert registry.config == {  # type: ignore[attr-defined]
        "endpoint": "https://registry.internal",
        "api_key": "resolved-token",
    }
    assert (
        registry.resolve(PromptRef("greeting")).messages[0].content
        == "https://registry.internal greeting"
    )


def test_custom_observability_receives_resolved_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_configured_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=(
                "\n            temporal:\n              address: localhost:7233\n"
                "            registry:\n"
                "              type: inline\n"
                "              prompts:\n"
                "                first: first {{value}}\n"
                "                second: second {{value}}\n"
                "            observability:\n"
                "              type: custom\n"
                "              class: yaml_demo_project.configured_extensions:ConfiguredObservability\n"
                "              config:\n"
                "                project: analytics\n"
                "            provider:\n              type: fake\n"
                "            imports:\n              allow_observability_class: true\n"
            ),
        )
    )

    backend = _build_observability(spec)
    assert type(backend).__name__ == "ConfiguredObservability"
    assert backend.config == {"project": "analytics"}  # type: ignore[attr-defined]


def test_custom_config_declared_but_class_rejects_it_fails_with_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared-but-unaccepted config must fail loudly — silently dropping it would
    read like working configuration that isn't."""
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_custom_extensions(tmp_path)  # CustomRegistry is zero-arg
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=(
                "\n            temporal:\n              address: localhost:7233\n"
                "            registry:\n"
                "              type: custom\n"
                "              class: yaml_demo_project.extensions:CustomRegistry\n"
                "              config:\n"
                "                endpoint: https://registry.internal\n"
                "            provider:\n              type: fake\n"
                "            imports:\n              allow_registry_class: true\n"
            ),
        )
    )

    with pytest.raises(TypeError, match=r"runtime\.registry\.config is declared but"):
        _build_registry(spec)


def test_custom_config_missing_required_source_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_configured_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("CUSTOM_REGISTRY_TOKEN", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=_configured_registry_runtime(
                "              config:\n"
                "                endpoint: https://registry.internal\n"
                "                api_key:\n"
                "                  value_from:\n"
                "                    env: CUSTOM_REGISTRY_TOKEN\n"
            ),
        )
    )

    with pytest.raises(
        ValueError, match=r"missing required secret for runtime\.registry\.config\[api_key\]"
    ):
        _build_registry(spec)


def test_custom_config_optional_missing_source_is_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_configured_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("CUSTOM_REGISTRY_TOKEN", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=_configured_registry_runtime(
                "              config:\n"
                "                endpoint: https://registry.internal\n"
                "                api_key:\n"
                "                  value_from:\n"
                "                    env: CUSTOM_REGISTRY_TOKEN\n"
                "                    required: false\n"
            ),
        )
    )

    registry = _build_registry(spec)
    assert registry.config == {"endpoint": "https://registry.internal"}  # type: ignore[attr-defined]


def test_custom_config_rejected_on_builtin_extension_types(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          config:
            endpoint: https://registry.internal
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        """,
    )

    with pytest.raises(
        ValueError, match=r"runtime\.registry\.config is only valid for type: custom"
    ):
        load_yaml_spec(path, load_dotenv=False)


def _custom_provider_spec(tmp_path: Path, *, subdir: str, config_line: str):
    directory = tmp_path / subdir
    directory.mkdir(parents=True, exist_ok=True)
    return load_yaml_spec(
        _write_demo_yaml(
            directory,
            runtime=(
                "\n            temporal:\n              address: localhost:7233\n"
                "            registry:\n"
                "              type: inline\n"
                "              prompts:\n"
                "                first: first {{value}}\n"
                "                second: second {{value}}\n"
                "            provider:\n"
                "              type: custom\n"
                "              class: pkg.mod:AcmeProvider\n"
                f"{config_line}"
                "            imports:\n              allow_provider_class: true\n"
            ),
        ),
        load_dotenv=False,
    )


def test_composed_custom_provider_with_divergent_config_rejects(tmp_path: Path) -> None:
    """#792's provider slice: the composed worker builds ONE provider — the parent's — so a
    child naming the same custom class with a different config errors loudly instead of
    silently running on the parent's credentials (codex round 3)."""
    from typeflux.yaml.runtime import (
        ProviderCompositionError,
        _assert_consistent_custom_provider_config,
    )

    parent = _custom_provider_spec(
        tmp_path,
        subdir="parent",
        config_line="              config: { endpoint: https://parent }\n",
    )
    child = _custom_provider_spec(
        tmp_path, subdir="child", config_line="              config: { endpoint: https://child }\n"
    )
    with pytest.raises(ProviderCompositionError, match=r"DIFFERENT config block"):
        _assert_consistent_custom_provider_config(parent, "child-wf", child)

    # Identical config (or an identical absent config) composes.
    same = _custom_provider_spec(
        tmp_path, subdir="same", config_line="              config: { endpoint: https://parent }\n"
    )
    _assert_consistent_custom_provider_config(parent, "child-wf", same)  # must not raise


def test_custom_config_rejects_empty_literal_values(tmp_path: Path) -> None:
    """ "" means unset everywhere in the secret-slot machinery, so an explicit empty literal
    would silently vanish before reaching the class — rejected at load instead (#792)."""
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: custom
          class: pkg.mod:Reg
          config:
            mode: ""
        provider:
          type: fake
        imports:
          allow_registry_class: true
        """,
    )

    with pytest.raises(ValueError, match=r"config\['mode'\] must not be an empty literal"):
        load_yaml_spec(path, load_dotenv=False)


def test_custom_config_internal_type_error_propagates_unmislabeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TypeError raised INSIDE a config-accepting __init__ is the class's own bug — it
    must surface unchanged, never rewrapped as the 'does not accept config' pointer."""
    package = tmp_path / "yaml_demo_project"
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    (package / "broken_extensions.py").write_text(
        dedent(
            """
            class BrokenConfiguredRegistry:
                def __init__(self, *, config):
                    dict(config, **{1: "non-string-key"})  # raises TypeError internally

                def resolve(self, ref):  # pragma: no cover - never reached
                    raise NotImplementedError
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=(
                "\n            temporal:\n              address: localhost:7233\n"
                "            registry:\n"
                "              type: custom\n"
                "              class: yaml_demo_project.broken_extensions:BrokenConfiguredRegistry\n"
                "              config:\n"
                "                endpoint: https://registry.internal\n"
                "            provider:\n              type: fake\n"
                "            imports:\n              allow_registry_class: true\n"
            ),
        )
    )

    with pytest.raises(TypeError) as exc:
        _build_registry(spec)
    assert "does not accept it" not in str(exc.value)


def test_custom_config_secret_references_join_the_inventory(tmp_path: Path) -> None:
    """#792's point: a custom extension's declared credentials appear in
    ``secret_reference_records`` (kind/name only, never the value) like every built-in slot."""
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: custom
              class: yaml_demo_project.extensions:CustomRegistry
              config:
                endpoint: https://registry.internal
                api_key:
                  value_from:
                    env: CUSTOM_REGISTRY_TOKEN
                    required: false
            provider:
              type: fake
            imports:
              allow_registry_class: true
            """,
        ),
        load_dotenv=False,
    )

    from typeflux.yaml.secrets import secret_reference_records

    records = {record.runtime_path: record for record in secret_reference_records(spec)}
    literal = records["runtime.registry.config[endpoint]"]
    assert literal.source_kind == "literal"
    assert literal.source_name == ""
    reference = records["runtime.registry.config[api_key]"]
    assert reference.source_kind == "env"
    assert reference.source_name == "CUSTOM_REGISTRY_TOKEN"
    assert "https://registry.internal" not in str(records)


def _required_observability_guard() -> RuntimePolicyGuard:
    """A composed policy guard whose payload sets ``observability.required`` (#756)."""
    return RuntimePolicyGuard(
        policy=ComposedProjectPolicy(
            selected_policy_ids=("require_obs",),
            applied_policy_ids=("require_obs",),
            policy_names=("require_obs",),
            policy_hash="d" * 64,
            payload={
                "observability": {"required": True, "allowed_backends": ["langfuse", "langsmith"]}
            },
        ),
    )


def _observability_spec(tmp_path: Path, backend: str | None, *, subdir: str = "spec"):
    """A loaded demo spec declaring the given observability backend (None ⇒ block absent).

    Each spec gets its own subdirectory: ``_write_yaml`` always writes ``typeflux.yaml``,
    so parent+child specs written to one tmp_path would clobber each other.
    """
    directory = tmp_path / subdir
    directory.mkdir(parents=True, exist_ok=True)
    observability = "" if backend is None else f"observability:\n  type: {backend}\n"
    return load_yaml_spec(
        _write_demo_yaml(
            directory,
            runtime=(
                "registry:\n"
                "  type: inline\n"
                "  prompts: {}\n"
                "provider:\n"
                "  type: fake\n" + observability
            ),
        )
    )


def test_build_observability_fails_closed_when_langfuse_required_but_keys_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #756: the regulated-tier adopter gap — a langfuse spec admitted under a
    # required-observability policy must REFUSE (naming the env vars) rather than degrade
    # to an untraced run when the credentials are absent (parity with the TS observer gate).
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = _observability_spec(tmp_path, "langfuse")
    with pytest.raises(RuntimeError, match=r"observability\.required.*no credentials resolve"):
        _build_observability(spec, policy_guard=_required_observability_guard())


def test_build_observability_fails_closed_when_langsmith_required_but_key_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    spec = _observability_spec(tmp_path, "langsmith")
    with pytest.raises(RuntimeError, match=r"observability\.required.*LANGSMITH_API_KEY"):
        _build_observability(spec, policy_guard=_required_observability_guard())


def test_build_observability_builds_when_required_and_keys_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    spec = _observability_spec(tmp_path, "langfuse")
    # No client is constructed here (the Langfuse client is lazy), so this stays offline.
    backend = _build_observability(spec, policy_guard=_required_observability_guard())
    assert type(backend).__name__ == "LangfuseObservabilityBackend"


def test_build_observability_still_degrades_when_not_required_and_keys_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Back-compat: with no policy (or a policy that does not require observability) the
    # absent-credential path is unchanged — a backend is returned, no RuntimeError.
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = _observability_spec(tmp_path, "langfuse")
    backend = _build_observability(spec, policy_guard=None)
    assert type(backend).__name__ == "LangfuseObservabilityBackend"


def test_build_observability_rejects_a_child_declaring_a_different_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The finder's exact scenario (#756 review round): parent langfuse WITH credentials,
    # child langsmith with no key. The composed worker builds ONE observer — the parent's —
    # so the child's declaration was dead config: never resolved, never credential-checked.
    # It must be a loud composition error naming both workflows and both types, not silence.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    parent = _observability_spec(tmp_path, "langfuse", subdir="parent")
    child = _observability_spec(tmp_path, "langsmith", subdir="child")
    with pytest.raises(ObservabilityCompositionError, match=r"'child'.*'langsmith'.*'langfuse'"):
        _build_observability(parent, child_specs=(("child", child),))


# --- Spec-declared observability credentials (#793) ---------------------------------


def _langfuse_credentials_spec(tmp_path: Path):
    return load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=(
                "\n            temporal:\n              address: localhost:7233\n"
                "            registry:\n"
                "              type: inline\n"
                "              prompts:\n"
                "                first: first {{value}}\n"
                "                second: second {{value}}\n"
                "            provider:\n              type: fake\n"
                "            observability:\n"
                "              type: langfuse\n"
                "              langfuse:\n"
                "                host: https://langfuse.internal\n"
                "                public_key: { value_from: { env: SPEC_LF_PUBLIC } }\n"
                "                secret_key: { value_from: { env: SPEC_LF_SECRET } }\n"
            ),
        ),
        load_dotenv=False,
    )


def test_observability_credentials_block_requires_matching_type(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        observability:
          type: langsmith
          langfuse:
            public_key: pk-dead
        """,
    )

    with pytest.raises(
        ValueError, match=r"langfuse credentials are only valid with type: langfuse"
    ):
        load_yaml_spec(path, load_dotenv=False)


def test_observability_credentials_join_the_secret_inventory(tmp_path: Path) -> None:
    spec = _langfuse_credentials_spec(tmp_path)

    from typeflux.yaml.secrets import secret_reference_records

    records = {record.runtime_path: record for record in secret_reference_records(spec)}
    assert records["runtime.observability.langfuse.public_key"].source_kind == "env"
    assert records["runtime.observability.langfuse.public_key"].source_name == "SPEC_LF_PUBLIC"
    assert records["runtime.observability.langfuse.secret_key"].source_name == "SPEC_LF_SECRET"


def test_build_observability_threads_spec_credentials_with_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#793: spec-declared credentials reach from_env explicitly (spec wins; env fallback
    untouched for unset fields), and the required-observability gate accepts them."""
    monkeypatch.setenv("SPEC_LF_PUBLIC", "pk-from-spec")
    monkeypatch.setenv("SPEC_LF_SECRET", "sk-from-spec")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = _langfuse_credentials_spec(tmp_path)

    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    captured: dict[str, Any] = {}
    original = LangfuseObservabilityBackend.from_env.__func__

    def _capture(cls, **kwargs):
        captured.update(kwargs)
        return original(cls, **kwargs)

    monkeypatch.setattr(LangfuseObservabilityBackend, "from_env", classmethod(_capture))
    backend = _build_observability(spec, policy_guard=_required_observability_guard())
    assert type(backend).__name__ == "LangfuseObservabilityBackend"
    assert captured["public_key"] == "pk-from-spec"
    assert captured["secret_key"] == "sk-from-spec"
    assert captured["host"] == "https://langfuse.internal"


def test_langsmith_spec_credentials_thread_and_satisfy_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The langsmith mirror of the langfuse threading test (finder B: the two backends
    must not implement #793 asymmetrically)."""
    monkeypatch.setenv("SPEC_LS_KEY", "ls-from-spec")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=(
                "\n            temporal:\n              address: localhost:7233\n"
                "            registry:\n"
                "              type: inline\n"
                "              prompts:\n"
                "                first: first {{value}}\n"
                "                second: second {{value}}\n"
                "            provider:\n              type: fake\n"
                "            observability:\n"
                "              type: langsmith\n"
                "              langsmith:\n"
                "                api_key: { value_from: { env: SPEC_LS_KEY } }\n"
                "                project: analytics\n"
            ),
        ),
        load_dotenv=False,
    )

    from typeflux.observability.langsmith import LangSmithObservabilityBackend

    captured: dict[str, Any] = {}
    original = LangSmithObservabilityBackend.from_env.__func__

    def _capture(cls, **kwargs):
        captured.update(kwargs)
        return original(cls, **kwargs)

    monkeypatch.setattr(LangSmithObservabilityBackend, "from_env", classmethod(_capture))
    backend = _build_observability(spec, policy_guard=_required_observability_guard())
    assert type(backend).__name__ == "LangSmithObservabilityBackend"
    assert captured["api_key"] == "ls-from-spec"
    assert captured["project"] == "analytics"


def test_observability_host_rejects_value_from(tmp_path: Path) -> None:
    """host/endpoint/project are NOT credentials: plain strings only (env interpolation
    covers the env-var pattern), so they never silently miss the secret inventory."""
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        observability:
          type: langfuse
          langfuse:
            host:
              value_from:
                env: LF_HOST
        """,
    )

    with pytest.raises(ValueError, match=r"host"):
        load_yaml_spec(path, load_dotenv=False)


def test_required_observability_gate_fails_when_spec_source_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SPEC_LF_PUBLIC", raising=False)
    monkeypatch.delenv("SPEC_LF_SECRET", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = _langfuse_credentials_spec(tmp_path)

    with pytest.raises(ValueError, match=r"missing required secret for runtime\.observability"):
        _build_observability(spec, policy_guard=_required_observability_guard())


def _custom_observability_spec(tmp_path: Path, *, subdir: str, config_line: str):
    directory = tmp_path / subdir
    directory.mkdir(parents=True, exist_ok=True)
    return load_yaml_spec(
        _write_demo_yaml(
            directory,
            runtime=(
                "registry:\n"
                "  type: inline\n"
                "  prompts: {}\n"
                "provider:\n"
                "  type: fake\n"
                "observability:\n"
                "  type: custom\n"
                "  class: yaml_demo_project.extensions:CustomObservability\n"
                f"{config_line}"
                "imports:\n"
                "  allow_observability_class: true\n"
            ),
        )
    )


def test_build_observability_rejects_same_custom_class_with_divergent_config(
    tmp_path: Path,
) -> None:
    """#792: a custom backend's declared config is part of its composition identity — the
    composed worker builds ONE observer from the parent's config, so a child naming the
    same class with different config would be silently dead. Loud error instead."""
    parent = _custom_observability_spec(
        tmp_path, subdir="parent", config_line="  config: { project: parent }\n"
    )
    child = _custom_observability_spec(
        tmp_path, subdir="child", config_line="  config: { project: child }\n"
    )
    with pytest.raises(ObservabilityCompositionError, match=r"DIFFERENT\s+config block"):
        _build_observability(parent, child_specs=(("child", child),))


def test_build_observability_same_custom_class_and_config_composes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_custom_extensions(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    config_line = "  config: { project: shared }\n"
    parent = _custom_observability_spec(tmp_path, subdir="parent", config_line=config_line)
    child = _custom_observability_spec(tmp_path, subdir="child", config_line=config_line)
    # CustomObservability is zero-arg, so a declared config fails the constructor contract —
    # which is AFTER the composition check this test targets: reaching that pointer error
    # proves the identical-config closure passed composition.
    with pytest.raises(TypeError, match=r"runtime\.observability\.config is declared"):
        _build_observability(parent, child_specs=(("child", child),))


def test_build_observability_child_with_absent_or_none_inherits_the_parent_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Absent ≡ explicit none (admission's `type or "none"` collapse): both mean "no own
    # declaration" and inherit the parent's observer — no conflict, backend builds.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    parent = _observability_spec(tmp_path, "langfuse", subdir="parent")
    absent = _observability_spec(tmp_path, None, subdir="absent")
    explicit_none = _observability_spec(tmp_path, "none", subdir="none")
    backend = _build_observability(
        parent, child_specs=(("absent", absent), ("explicit_none", explicit_none))
    )
    assert type(backend).__name__ == "LangfuseObservabilityBackend"


def test_build_observability_rejects_a_child_backend_when_the_parent_declares_none(
    tmp_path: Path,
) -> None:
    # The inverted gap: the parent's effective backend is none ⇒ NO observer is ever built,
    # so a child declaring langfuse would be silently ignored — loud error instead.
    parent = _observability_spec(tmp_path, None, subdir="parent")
    child = _observability_spec(tmp_path, "langfuse", subdir="child")
    with pytest.raises(ObservabilityCompositionError, match=r"'child'.*'langfuse'.*'none'"):
        _build_observability(parent, child_specs=(("child", child),))


def test_build_observability_consistent_closure_under_required_policy_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Composed + required + credentials present: the consistency check passes (child agrees),
    # the credential gate passes (keys resolve), and the single effective backend builds.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    parent = _observability_spec(tmp_path, "langfuse", subdir="parent")
    child = _observability_spec(tmp_path, "langfuse", subdir="child")
    backend = _build_observability(
        parent,
        policy_guard=_required_observability_guard(),
        child_specs=(("child", child),),
    )
    assert type(backend).__name__ == "LangfuseObservabilityBackend"


def test_build_registry_builds_langsmith(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    # No live LangSmith client is constructed: inject a fake so build is offline.
    from typeflux.prompts import langsmith as langsmith_module

    monkeypatch.setattr(langsmith_module, "_langsmith_client", lambda **_: object())
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langsmith
              label: production
            provider:
              type: fake
            """,
        )
    )

    registry = _build_registry(spec)
    # Wrapped by the label-override registry because runtime.registry.label is set.
    assert type(registry).__name__ == "_LabelOverrideRegistry"


def test_runtime_cache_erasure_accepts_declared_values_only(tmp_path: Path) -> None:
    """#795: the declared cache-erasure requirement is a closed vocabulary."""
    good = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        cache_erasure: targeted
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        """,
    )
    assert load_yaml_spec(good, load_dotenv=False).runtime.cache_erasure == "targeted"

    (tmp_path / "bad").mkdir()
    bad = _write_demo_yaml(
        tmp_path / "bad",
        runtime="""
        temporal:
          address: localhost:7233
        cache_erasure: sometimes
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        """,
    )
    with pytest.raises(ValueError, match="cache_erasure"):
        load_yaml_spec(bad, load_dotenv=False)


def test_custom_extension_type_requires_a_class() -> None:
    from typeflux.yaml.spec import ObservabilitySpec, ProviderSpec, RegistrySpec

    with pytest.raises(ValueError, match="requires a 'class'"):
        ProviderSpec(type="custom")
    with pytest.raises(ValueError, match="requires a 'class'"):
        RegistrySpec(type="custom")
    with pytest.raises(ValueError, match="requires a 'class'"):
        ObservabilitySpec(type="custom")


def test_generated_workflow_classes_do_not_overwrite_module_globals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    first_spec = load_yaml_spec(_write_demo_yaml(tmp_path, yaml_name="first_yaml"))
    second_spec = load_yaml_spec(_write_demo_yaml(tmp_path, yaml_name="second_yaml"))

    first_workflow = create_workflow(first_spec, collect_activities(first_spec))
    second_workflow = create_workflow(second_spec, collect_activities(second_spec))

    from temporalio import workflow

    first_definition = workflow._Definition.must_from_class(first_workflow)
    second_definition = workflow._Definition.must_from_class(second_workflow)

    assert first_workflow is not second_workflow
    assert first_workflow.__name__ != second_workflow.__name__
    assert getattr(workflow_module, first_workflow.__name__) is first_workflow
    assert getattr(workflow_module, second_workflow.__name__) is second_workflow
    assert not hasattr(workflow_module, first_spec.workflow.name)
    first_digest = first_workflow.__typeflux_spec_digest__
    second_digest = second_workflow.__typeflux_spec_digest__
    assert first_definition.name == f"DemoYamlWorkflow.{first_digest[:12]}"
    assert second_definition.name == f"DemoYamlWorkflow.{second_digest[:12]}"
    assert first_definition.name != second_definition.name
    assert first_workflow.__typeflux_activity_calls__ == second_workflow.__typeflux_activity_calls__
    assert first_workflow.__typeflux_workflow_name__ == "DemoYamlWorkflow"
    assert first_workflow.__typeflux_project__ == "yaml_demo_project"
    assert first_workflow.__typeflux_yaml_name__ == "first_yaml"


def test_validate_unique_yaml_workflow_names_rejects_duplicates(tmp_path: Path) -> None:
    first = load_yaml_spec(_write_demo_yaml(tmp_path, yaml_name="first_yaml"))
    second = load_yaml_spec(_write_demo_yaml(tmp_path, yaml_name="second_yaml"))

    with pytest.raises(ValueError, match="duplicate YAML workflow name\\(s\\): DemoYamlWorkflow"):
        validate_unique_yaml_workflow_names((first, second))


def test_validate_unique_yaml_workflow_names_allows_distinct_names(tmp_path: Path) -> None:
    first = load_yaml_spec(
        _write_demo_yaml(tmp_path, yaml_name="first_yaml", workflow_name="FirstWorkflow")
    )
    second = load_yaml_spec(
        _write_demo_yaml(tmp_path, yaml_name="second_yaml", workflow_name="SecondWorkflow")
    )

    validate_unique_yaml_workflow_names((first, second))


@pytest.mark.asyncio
async def test_generated_workflow_prepares_with_sandbox_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    workflow_cls = create_workflow(spec, collect_activities(spec))

    from temporalio import workflow

    from typeflux.yaml.workflow import create_yaml_workflow_runner

    definition = workflow._Definition.must_from_class(workflow_cls)

    create_yaml_workflow_runner().prepare_workflow(definition)


@pytest.mark.asyncio
async def test_build_runtime_configures_yaml_sandbox_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))

    async def fake_connect(spec, *, plugin):
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    from temporalio import workflow

    workflow_runner = runtime.worker.worker_kwargs["workflow_runner"]
    definition = workflow._Definition.must_from_class(runtime.workflow_class)
    assert definition.sandboxed is True
    workflow_runner.prepare_workflow(definition)


@pytest.mark.asyncio
async def test_sandboxed_yaml_runtime_worker_executes_generated_workflow_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_recording_provider(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    from temporalio import workflow
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment
    from yaml_demo_project.schemas import InputModel, OutputModel

    task_queue = f"demo-task-queue-{uuid4().hex}"
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            task_queue=task_queue,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: custom
              class: yaml_demo_project.providers:RecordingProvider
            imports:
              allow_provider_class: true
            observability:
              type: none
            """,
        )
    )

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as exc:
        pytest.skip(f"Temporal test server unavailable: {exc}")

    async with env:

        async def fake_connect(spec, *, plugin):
            assert plugin is None
            return env.client

        monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
        runtime = await build_runtime(spec)

        workflow_runner = runtime.worker.worker_kwargs["workflow_runner"]
        definition = workflow._Definition.must_from_class(runtime.workflow_class)
        assert definition.sandboxed is True
        workflow_runner.prepare_workflow(definition)

        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                InputModel(value="start"),
                id=f"yaml-sandbox-e2e-{uuid4().hex}",
                result_type=OutputModel,
            )

    assert result == OutputModel(value="start:middle:output")
    assert runtime.provider.calls == [
        {
            "messages": ["first start"],
            "output_schema": "MiddleModel",
        },
        {
            "messages": ["second start:middle"],
            "output_schema": "OutputModel",
        },
    ]


@pytest.mark.asyncio
async def test_generated_workflow_calls_activities_in_declared_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append((name, arg, kwargs))
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    result = await workflow_cls().run(InputModel(value="start"))

    assert result == OutputModel(value="start:middle:output")
    assert [call[0] for call in calls] == ["first", "second"]
    assert [call[2]["activity_id"] for call in calls] == ["first", "second"]


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_query_tracks_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            cancellation: true
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    result = await workflow_instance.run(InputModel(value="start"))
    status = workflow_instance.typeflux_lifecycle_status()

    assert result == OutputModel(value="start:middle:output")
    assert status.state == "completed"
    assert status.completed_units == 2
    assert status.total_units == 2
    assert status.terminal_status == "completed"
    assert [event.event for event in status.events] == [
        "workflow_started",
        "step_started",
        "progress",
        "step_started",
        "progress",
        "workflow_completed",
    ]


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_status_keeps_bounded_event_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            cancellation: true
            history:
              status_event_limit: 3
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    result = await workflow_instance.run(InputModel(value="start"))
    status = workflow_instance.typeflux_lifecycle_status()

    assert result == OutputModel(value="start:middle:output")
    assert status.event_count == 6
    assert status.events_truncated is True
    assert status.oldest_event_sequence == 4
    assert status.latest_event_sequence == 6
    assert [event.event for event in status.events] == [
        "step_started",
        "progress",
        "workflow_completed",
    ]


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_status_can_omit_event_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            history:
              status_event_limit: 0
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    await workflow_instance.run(InputModel(value="start"))
    status = workflow_instance.typeflux_lifecycle_status()

    assert status.completed_units == 2
    assert status.event_count == 6
    assert status.events_truncated is True
    assert status.oldest_event_sequence is None
    assert status.latest_event_sequence == 6
    assert status.events == ()


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_cancels_before_first_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            cancellation: true
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel

    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        raise AssertionError("activity should not start")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    workflow_instance.typeflux_request_cancel("user requested cancel")

    with pytest.raises(Exception, match="workflow cancellation requested"):
        await workflow_instance.run(InputModel(value="start"))

    status = workflow_instance.typeflux_lifecycle_status()
    assert calls == []
    assert status.state == "cancelled"
    assert status.cancellation_requested is True
    assert status.cancellation_reason == "user requested cancel"
    assert status.terminal_status == "cancelled"


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_cancels_between_activities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            progress: true
            cancellation: true
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel

    workflow_instance = workflow_cls()
    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if name == "first":
            workflow_instance.typeflux_request_cancel("stop after first")
            return MiddleModel(value=f"{arg.value}:middle")
        raise AssertionError("second activity should not start")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    with pytest.raises(Exception, match="workflow cancellation requested"):
        await workflow_instance.run(InputModel(value="start"))

    status = workflow_instance.typeflux_lifecycle_status()
    assert calls == ["first"]
    assert status.completed_units == 1
    assert status.total_units == 2
    assert status.state == "cancelled"


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_review_routes_to_configured_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            review:
              after_step: first
              user_decisions:
                send_email:
                  route: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")

    status = workflow_instance.typeflux_lifecycle_status()
    assert status.waiting_checkpoint == "first"

    workflow_instance.typeflux_submit_review({"user_decision": "send_email", "notes": "looks good"})
    result = await run_task
    status = workflow_instance.typeflux_lifecycle_status()

    assert result == OutputModel(value="start:middle:output")
    assert status.state == "completed"
    assert status.review_user_decision == "send_email"
    assert status.review_route_target == "second"
    assert "looks good" not in status.model_dump_json()


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_gates_single_gate_routes_and_reports_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #55 slice 4: a `gates:` list (even with one gate) drives the SAME generated workflow as
    # `review`, routing correctly and reporting the additive wire fields (gate_id, waiting_gates).
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            gates:
              - id: screen_gate
                after_step: first
                user_decisions:
                  send_email:
                    route: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")

    status = workflow_instance.typeflux_lifecycle_status()
    assert status.waiting_checkpoint == "first"
    # The execution reports its open gate by name with the decisions it will honor.
    assert [g.model_dump() for g in status.waiting_gates] == [
        {
            "gate_id": "screen_gate",
            "after_step": "first",
            "valid_user_decisions": {"send_email": "second"},
        }
    ]
    # Multi-gate mode stamps gate_id on the gate events.
    assert status.events[-1].event == "waiting_for_review"
    assert status.events[-1].gate_id == "screen_gate"

    # A decision addressed to the named gate resolves it (an explicit gate id also works).
    workflow_instance.typeflux_submit_review({"user_decision": "send_email", "gate": "screen_gate"})
    result = await run_task
    status = workflow_instance.typeflux_lifecycle_status()

    assert result == OutputModel(value="start:middle:output")
    assert status.state == "completed"
    assert status.review_user_decision == "send_email"
    assert status.review_route_target == "second"
    assert status.waiting_gates == ()  # empty once completed (always present)
    assert {e.gate_id for e in status.events if e.event == "review_submitted"} == {"screen_gate"}


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_review_invalid_warn_keeps_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            review:
              after_step: first
              invalid_user_decision: warn
              user_decisions:
                send_email:
                  route: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")
    workflow_instance.typeflux_submit_review(
        {"user_decision": "unknown", "reviewer": "user@example.com"}
    )
    await asyncio.sleep(0.02)

    status = workflow_instance.typeflux_lifecycle_status()
    assert status.state == "waiting_for_review"
    assert status.review_user_decision is None
    assert "unknown" not in status.model_dump_json()
    assert "user@example.com" not in status.model_dump_json()

    workflow_instance.typeflux_submit_review({"user_decision": "send_email"})
    result = await run_task

    status = workflow_instance.typeflux_lifecycle_status()
    assert result == OutputModel(value="start:middle:output")
    assert status.state == "completed"
    assert status.review_user_decision == "send_email"


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_review_invalid_fail_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            review:
              after_step: first
              invalid_user_decision: fail
              user_decisions:
                send_email:
                  route: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        raise AssertionError("second activity should not start")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")
    workflow_instance.typeflux_submit_review({"user_decision": "unknown"})

    with pytest.raises(Exception, match="invalid user_decision"):
        await run_task

    status = workflow_instance.typeflux_lifecycle_status()
    assert status.state == "failed"
    assert status.review_user_decision is None
    assert status.terminal_status == "failed"


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_review_invalid_fail_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            lifecycle="""
            enabled: true
            review:
              after_step: first
              invalid_user_decision: fail
              user_decisions:
                send_email:
                  route: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        raise AssertionError("second activity should not start after invalid review")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")
    workflow_instance.typeflux_submit_review({"user_decision": "unknown"})
    workflow_instance.typeflux_submit_review({"user_decision": "send_email"})

    with pytest.raises(Exception, match="invalid user_decision"):
        await run_task

    status = workflow_instance.typeflux_lifecycle_status()
    assert status.state == "failed"
    assert status.review_user_decision == "send_email"
    assert status.review_route_target == "second"
    assert status.terminal_status == "failed"


_REVIEW_ROUTE_ORDER_RUNTIME = """
temporal:
  address: localhost:7233
registry:
  type: inline
  prompts:
    first: first {{value}}
    second: second {{value}}
    polish: polish {{value}}
provider:
  type: fake
"""

_REVIEW_ROUTE_ORDER_DEFINITIONS = """
- name: polish_a
  input: schemas:MiddleModel
  output: schemas:MiddleModel
  prompt: polish
- name: polish_b
  input: schemas:MiddleModel
  output: schemas:MiddleModel
  prompt: polish
"""

_REVIEW_ROUTE_ORDER_STEPS = """
- id: first
  activity: first
- id: polish_a
  activity: polish_a
- id: polish_b
  activity: polish_b
- id: second
  activity: second
"""

_REVIEW_ROUTE_ORDER_LIFECYCLE = """
enabled: true
review:
  after_step: first
  user_decisions:
    finalize:
      route: second
    full:
      route: polish_a
"""


def _review_route_order_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=_REVIEW_ROUTE_ORDER_RUNTIME,
            definitions=_REVIEW_ROUTE_ORDER_DEFINITIONS,
            lifecycle=_REVIEW_ROUTE_ORDER_LIFECYCLE,
            steps=_REVIEW_ROUTE_ORDER_STEPS,
        )
    )
    return create_workflow(spec, collect_activities(spec))


def _patch_review_route_order_activities(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    import temporalio.workflow
    from yaml_demo_project.schemas import MiddleModel, OutputModel

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        if name in {"polish_a", "polish_b"}:
            return MiddleModel(value=f"{arg.value}:{name}")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_review_forward_route_skips_intermediate_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_cls = _review_route_order_workflow(tmp_path, monkeypatch)
    from yaml_demo_project.schemas import InputModel, OutputModel

    calls: list[str] = []
    _patch_review_route_order_activities(monkeypatch, calls)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")
    workflow_instance.typeflux_submit_review({"user_decision": "finalize"})
    result = await run_task
    status = workflow_instance.typeflux_lifecycle_status()

    assert calls == ["first", "second"]
    assert result == OutputModel(value="start:middle:output")
    assert status.state == "completed"
    assert status.review_route_target == "second"
    assert status.completed_units == status.total_units == 2


@pytest.mark.asyncio
async def test_generated_workflow_lifecycle_review_route_falls_through_later_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_cls = _review_route_order_workflow(tmp_path, monkeypatch)
    from yaml_demo_project.schemas import InputModel, OutputModel

    calls: list[str] = []
    _patch_review_route_order_activities(monkeypatch, calls)

    workflow_instance = workflow_cls()
    run_task = asyncio.create_task(workflow_instance.run(InputModel(value="start")))
    await _wait_for_status(workflow_instance, "waiting_for_review")
    workflow_instance.typeflux_submit_review({"user_decision": "full"})
    result = await run_task
    status = workflow_instance.typeflux_lifecycle_status()

    assert calls == ["first", "polish_a", "polish_b", "second"]
    assert result == OutputModel(value="start:middle:polish_a:polish_b:output")
    assert status.state == "completed"
    assert status.review_route_target == "polish_a"
    assert status.completed_units == status.total_units == 4


@pytest.mark.asyncio
async def test_wait_lifecycle_condition_propagates_wait_condition_failures() -> None:
    from typeflux.yaml.workflow import _wait_lifecycle_condition

    async def failing_wait_condition(predicate: Any) -> None:
        raise RuntimeError("durable wait failed")

    fake_workflow = SimpleNamespace(
        wait_condition=failing_wait_condition,
        in_workflow=lambda: True,
    )

    with pytest.raises(RuntimeError, match="durable wait failed"):
        await _wait_lifecycle_condition(fake_workflow, lambda: False)


@pytest.mark.asyncio
async def test_wait_lifecycle_condition_polls_outside_workflow_context() -> None:
    from typeflux.yaml.workflow import _wait_lifecycle_condition

    async def failing_wait_condition(predicate: Any) -> None:
        raise RuntimeError("not in workflow event loop")

    fake_workflow = SimpleNamespace(
        wait_condition=failing_wait_condition,
        in_workflow=lambda: False,
    )
    ready = False

    async def make_ready() -> None:
        nonlocal ready
        await asyncio.sleep(0.02)
        ready = True

    flip_task = asyncio.create_task(make_ready())
    await asyncio.wait_for(
        _wait_lifecycle_condition(fake_workflow, lambda: ready),
        timeout=1.0,
    )
    await flip_task


def test_yaml_docs_lifecycle_review_snippets_match_schema() -> None:
    docs = (Path(__file__).resolve().parents[3] / "docs" / "yaml.md").read_text(encoding="utf-8")

    assert "user_decisions:" in docs
    assert "approve_decisions" not in docs
    assert "reject_decisions" not in docs
    assert '{"user_decision": "approve"}' in docs
    assert '{"decision"' not in docs


def test_lifecycle_audit_normalizes_double_l_cancelled_activity_events() -> None:
    scheduled_activities: dict[int, dict[str, str | None]] = {
        2: {"activity_id": "first-1", "activity_type": "first", "step_id": "first"},
    }
    cancelled = workflow_lifecycle_audit_event_from_history_event(
        SimpleNamespace(
            event_id=7,
            event_type=SimpleNamespace(name="EVENT_TYPE_ACTIVITY_TASK_CANCELLED"),
            event_time=None,
            activity_task_canceled_event_attributes=SimpleNamespace(
                scheduled_event_id=2,
                started_event_id=4,
            ),
        ),
        workflow_id="wf-audit",
        run_id="run-audit",
        scheduled_activities=scheduled_activities,
    )

    assert cancelled is not None
    assert cancelled.lifecycle_event == "activity_cancelled"
    assert cancelled.activity_id == "first-1"
    assert cancelled.step_id == "first"
    assert cancelled.scheduled_event_id == 2
    assert cancelled.started_event_id == 4


def test_lifecycle_audit_normalizes_workflow_and_activity_history_events() -> None:
    scheduled_activities: dict[int, dict[str, str | None]] = {}

    workflow_started = workflow_lifecycle_audit_event_from_history_event(
        _history_event(1, "WORKFLOW_EXECUTION_STARTED"),
        workflow_id="wf-audit",
        run_id="run-audit",
        scheduled_activities=scheduled_activities,
    )
    activity_scheduled = workflow_lifecycle_audit_event_from_history_event(
        _history_event(
            2,
            "ACTIVITY_TASK_SCHEDULED",
            activity_id="review_pages-3",
            activity_type="review_page",
        ),
        workflow_id="wf-audit",
        run_id="run-audit",
        scheduled_activities=scheduled_activities,
    )
    activity_completed = workflow_lifecycle_audit_event_from_history_event(
        _history_event(
            3,
            "ACTIVITY_TASK_COMPLETED",
            scheduled_event_id=2,
            started_event_id=4,
        ),
        workflow_id="wf-audit",
        run_id="run-audit",
        scheduled_activities=scheduled_activities,
    )

    assert workflow_started is not None
    assert workflow_started.lifecycle_event == "workflow_started"
    assert workflow_started.workflow_id == "wf-audit"
    assert activity_scheduled is not None
    assert activity_scheduled.lifecycle_event == "activity_scheduled"
    assert activity_scheduled.activity_id == "review_pages-3"
    assert activity_scheduled.activity_type == "review_page"
    assert activity_scheduled.step_id == "review_pages"
    assert activity_completed is not None
    assert activity_completed.lifecycle_event == "activity_completed"
    assert activity_completed.activity_id == "review_pages-3"
    assert activity_completed.scheduled_event_id == 2
    assert activity_completed.started_event_id == 4


def test_lifecycle_audit_normalizes_review_and_cancel_signals() -> None:
    converter = _FakeLifecycleAuditConverter()

    review = workflow_lifecycle_audit_event_from_history_event(
        _history_event(
            10,
            "WORKFLOW_EXECUTION_SIGNALED",
            signal_name="typeflux_submit_review",
            payloads=[
                ReviewCommand(
                    user_decision="send_email",
                    reviewer="reviewer@example.com",
                    notes="private notes",
                )
            ],
        ),
        workflow_id="wf-audit",
        run_id="run-audit",
        data_converter=converter,
    )
    cancel = workflow_lifecycle_audit_event_from_history_event(
        _history_event(
            11,
            "WORKFLOW_EXECUTION_SIGNALED",
            signal_name="typeflux_request_cancel",
            payloads=["customer withdrew request"],
        ),
        workflow_id="wf-audit",
        run_id="run-audit",
        data_converter=converter,
    )

    assert review is not None
    assert review.lifecycle_event == "review_submitted"
    assert review.review_user_decision == "send_email"
    assert review.reviewer == "reviewer@example.com"
    assert "private notes" not in review.model_dump_json()
    assert cancel is not None
    assert cancel.lifecycle_event == "cancellation_requested"
    assert cancel.cancellation_reason == "customer withdrew request"


class _FakeAuditHandle:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.id = "wf-audit"
        self.run_id = "run-audit"

    async def fetch_history_events(self, page_size: int | None = None):
        for event in self._events:
            yield event


def _sensitive_audit_history() -> list[Any]:
    return [
        _history_event(
            10,
            "WORKFLOW_EXECUTION_SIGNALED",
            signal_name="typeflux_submit_review",
            payloads=[ReviewCommand(user_decision="send_email", reviewer="reviewer@example.com")],
        ),
        _history_event(
            11,
            "WORKFLOW_EXECUTION_SIGNALED",
            signal_name="typeflux_request_cancel",
            payloads=["call me back at 555-123-4567"],
        ),
    ]


@pytest.mark.asyncio
async def test_lifecycle_audit_export_preserves_sensitive_fields_by_default() -> None:
    export = await export_workflow_lifecycle_audit(
        _FakeAuditHandle(_sensitive_audit_history()),
        data_converter=_FakeLifecycleAuditConverter(),
    )

    assert export.events[0].reviewer == "reviewer@example.com"
    assert export.events[1].cancellation_reason == "call me back at 555-123-4567"


@pytest.mark.asyncio
async def test_lifecycle_audit_export_redacts_sensitive_fields_with_redactor() -> None:
    export = await export_workflow_lifecycle_audit(
        _FakeAuditHandle(_sensitive_audit_history()),
        data_converter=_FakeLifecycleAuditConverter(),
        redactor=RegexPIIRedactor.default(),
    )

    review, cancel = export.events
    assert review.reviewer == "[REDACTED_EMAIL]"
    assert review.review_user_decision == "send_email"
    assert review.lifecycle_event == "review_submitted"
    assert cancel.cancellation_reason is not None
    assert "555-123-4567" not in cancel.cancellation_reason
    assert "[REDACTED_PHONE]" in cancel.cancellation_reason


@pytest.mark.asyncio
async def test_generated_workflow_calls_yaml_defined_activities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            modules=None,
            definitions="""
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: first
            - name: second
              input: schemas:MiddleModel
              output: schemas:OutputModel
              prompt: second
            """,
        )
    )
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow
    from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

    calls: list[tuple[str, Any]] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append((name, arg))
        if name == "first":
            return MiddleModel(value=f"{arg.value}:middle")
        return OutputModel(value=f"{arg.value}:output")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    result = await workflow_cls().run(InputModel(value="start"))

    assert result == OutputModel(value="start:middle:output")
    assert [call[0] for call in calls] == ["first", "second"]


@pytest.mark.asyncio
async def test_build_runtime_selects_inline_fake_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    assert isinstance(runtime, TypefluxYamlRuntime)
    assert isinstance(runtime.registry, InlinePromptRegistry)
    assert isinstance(runtime.provider, FakeProvider)
    assert isinstance(runtime.observer, NoOpObserver)
    from temporalio import workflow

    definition = workflow._Definition.must_from_class(runtime.workflow_class)
    spec_digest = runtime.workflow_class.__typeflux_spec_digest__
    assert definition.name == f"DemoYamlWorkflow.{spec_digest[:12]}"
    assert runtime.workflow_class.__typeflux_workflow_name__ == "DemoYamlWorkflow"
    assert runtime.workflow_class.__typeflux_workflow_type__ == definition.name
    assert runtime.preflight().ok is True


@pytest.mark.asyncio
async def test_build_runtime_passes_yaml_provider_rate_limit_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            provider_limits:
              default:
                max_concurrent: 8
              providers:
                fake:
                  max_concurrent: 6
                  models:
                    slow-model:
                      max_concurrent: 3
                      min_interval_seconds: 0.25
            """,
        )
    )

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    controller = runtime.worker.provider_rate_limit_controller
    assert controller is not None
    selection = controller.select(provider_name="fake", provider_model="slow-model")
    assert selection.policy_source == "model"
    assert selection.limits is not None
    assert selection.limits.max_concurrent == 3
    assert selection.limits.min_interval_seconds == 0.25


@pytest.mark.asyncio
async def test_build_runtime_passes_yaml_provider_retry_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            provider_retry:
              max_attempts: 3
              initial_backoff_seconds: 0.25
              max_backoff_seconds: 2.0
              backoff_multiplier: 1.5
              retry_rate_limits: false
              retry_transient_errors: true
            """,
        )
    )

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    retry_policy = runtime.worker.provider_retry_policy
    assert retry_policy is not None
    assert retry_policy.max_attempts == 3
    assert retry_policy.initial_backoff_seconds == 0.25
    assert retry_policy.max_backoff_seconds == 2.0
    assert retry_policy.backoff_multiplier == 1.5
    assert retry_policy.retry_rate_limits is False
    assert retry_policy.retry_transient_errors is True


@pytest.mark.asyncio
async def test_build_runtime_preserves_default_provider_retry_when_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    assert runtime.worker.provider_retry_policy is None


@pytest.mark.asyncio
async def test_yaml_provider_retry_policy_retries_provider_rate_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    _write_recording_provider(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: custom
              class: yaml_demo_project.providers:RetryingProvider
            provider_retry:
              max_attempts: 2
            imports:
              allow_provider_class: true
            """,
        )
    )

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    from yaml_demo_project.schemas import InputModel, MiddleModel

    result = execute_ai_activity(
        activity=runtime.activities["first"],
        input_value=InputModel(value="start"),
        registry=runtime.registry,
        provider=runtime.provider,
        provider_retry_policy=runtime.worker.provider_retry_policy,
    )

    assert result == MiddleModel(value="start:middle")
    provider_calls = runtime.provider.calls
    assert len(provider_calls) == 2
    first_controls = provider_calls[0]["metadata"]["typeflux"]["provider_controls"]
    second_controls = provider_calls[1]["metadata"]["typeflux"]["provider_controls"]
    assert first_controls["retry_attempt"] == 0
    assert first_controls["max_attempts"] == 2
    assert second_controls["retry_attempt"] == 1
    assert second_controls["previous_error_type"] == "ProviderRateLimitError"
    assert second_controls["previous_error_status_code"] == 429
    assert second_controls["rate_limited"] is True


@pytest.mark.asyncio
async def test_build_runtime_enables_langfuse_when_observability_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langfuse
              label: staging
            provider:
              type: openai
              model: test-model
            observability:
              type: langfuse
            """,
            lifecycle="""
            enabled: true
            review:
              after_step: first
              user_decisions:
                send_email:
                  route: second
            """,
        )
    )
    calls: dict[str, Any] = {}

    class FakeLangfuseRegistry:
        def __init__(self, **kwargs: Any) -> None:
            calls["registry_kwargs"] = kwargs

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["provider_kwargs"] = kwargs

    async def fake_connect(spec, *, plugin):
        calls["plugin"] = plugin
        return object()

    def fake_from_env(**kwargs: Any):
        calls["trace_kwargs"] = kwargs
        return _FakeObservabilityBackend(client="langfuse-client", plugin="plugin")

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeLangfuseRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        fake_from_env,
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    assert calls["plugin"] == "plugin"
    assert calls["provider_kwargs"]["enable_langfuse"] is True
    assert calls["provider_kwargs"]["default_model"] == "test-model"
    assert calls["trace_kwargs"]["redactor"].redact("Email jane.doe@example.com") == (
        "Email [REDACTED_EMAIL]"
    )
    assert runtime.langfuse_client == "langfuse-client"
    assert any(
        isinstance(contributor, RuntimePlacementContributor)
        for contributor in runtime.observability.writer.metadata_contributors
    )


@pytest.mark.asyncio
async def test_build_runtime_does_not_enable_langfuse_from_registry_or_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langfuse
              label: staging
            provider:
              type: openai
              model: test-model
            """,
        )
    )
    calls: dict[str, Any] = {}

    class FakeLangfuseRegistry:
        def __init__(self, **kwargs: Any) -> None:
            calls["registry_kwargs"] = kwargs

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["provider_kwargs"] = kwargs

    async def fake_connect(spec, *, plugin):
        calls["plugin"] = plugin
        return object()

    def fail_from_env(**kwargs: Any):
        raise AssertionError("Langfuse observability should require explicit YAML opt-in")

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeLangfuseRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        fail_from_env,
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    assert calls["plugin"] is None
    assert calls["registry_kwargs"] == {"host": None, "allow_prompt_model_override": False}
    assert calls["provider_kwargs"]["enable_langfuse"] is False
    assert runtime.langfuse_client is None


@pytest.mark.asyncio
async def test_build_runtime_treats_null_observability_type_as_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: openai
              model: test-model
            observability:
              type: null
            """,
        )
    )
    calls: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["provider_kwargs"] = kwargs

    async def fake_connect(spec, *, plugin):
        calls["plugin"] = plugin
        return object()

    def fail_from_env(**kwargs: Any):
        raise AssertionError("Langfuse observability should require explicit YAML opt-in")

    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        fail_from_env,
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    assert calls["plugin"] is None
    assert calls["provider_kwargs"]["enable_langfuse"] is False
    assert runtime.langfuse_client is None


@pytest.mark.asyncio
async def test_build_runtime_disables_langfuse_for_openai_provider_without_observability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: openai
              model: test-model
            observability:
              type: none
            """,
        )
    )
    calls: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            calls["provider_kwargs"] = kwargs

    async def fake_connect(spec, *, plugin):
        calls["plugin"] = plugin
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)

    assert calls["plugin"] is None
    assert calls["provider_kwargs"]["enable_langfuse"] is False
    assert calls["provider_kwargs"]["default_model"] == "test-model"
    assert runtime.langfuse_client is None


@pytest.mark.asyncio
async def test_build_runtime_registry_label_applies_only_without_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    recording_registry = _RecordingPromptRegistry()
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langfuse
              label: staging
            provider:
              type: fake
            """,
            modules=None,
            definitions="""
            - name: first
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt: first
            - name: second
              input: schemas:MiddleModel
              output: schemas:OutputModel
              prompt:
                name: second
                label: canary
            - name: omitted
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: omitted
            - name: production
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: production
                label: production
            - name: pinned
              input: schemas:InputModel
              output: schemas:MiddleModel
              prompt:
                name: pinned
                version: 7
            """,
        )
    )

    async def fake_connect(spec, *, plugin):
        return object()

    def fake_from_env(**kwargs: Any):
        return _FakeObservabilityBackend(client="langfuse-client", plugin="plugin")

    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfusePromptRegistry",
        lambda **kwargs: recording_registry,
    )
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        fake_from_env,
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(spec)
    runtime.registry.resolve(runtime.activities["first"].prompt_ref)
    runtime.registry.resolve(runtime.activities["second"].prompt_ref)
    runtime.registry.resolve(runtime.activities["omitted"].prompt_ref)
    runtime.registry.resolve(runtime.activities["production"].prompt_ref)
    runtime.registry.resolve(runtime.activities["pinned"].prompt_ref)

    assert recording_registry.refs == [
        PromptRef("first", label="staging"),
        PromptRef("second", label="canary"),
        PromptRef("omitted", label="staging"),
        PromptRef("production", label="production"),
        PromptRef("pinned", version=7),
    ]


@pytest.mark.asyncio
async def test_build_runtime_uses_yaml_redaction_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langfuse
            provider:
              type: openai
              model: test-model
            observability:
              type: langfuse
              redaction:
                enabled: true
                emails: false
                phones: true
                ssn: false
                credit_cards: false
                exclude_paths:
                  - metadata.ticket_id
            """,
        )
    )
    calls: dict[str, Any] = {}

    class FakeLangfuseRegistry:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    async def fake_connect(spec, *, plugin):
        return object()

    def fake_from_env(**kwargs: Any):
        calls["trace_kwargs"] = kwargs
        return _FakeObservabilityBackend(client="langfuse-client", plugin="plugin")

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeLangfuseRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        fake_from_env,
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    await build_runtime(spec)

    redactor = calls["trace_kwargs"]["redactor"]
    result = redactor.redact(
        "email jane.doe@example.com phone 555-123-4567 ssn 123-45-6789 card 4242 4242 4242 4242"
    )
    assert result == (
        "email jane.doe@example.com phone [REDACTED_PHONE] ssn 123-45-6789 card 4242 4242 4242 4242"
    )
    assert redactor.redact({"metadata": {"ticket_id": "ticket-555-123-4567"}}) == {
        "metadata": {"ticket_id": "ticket-555-123-4567"}
    }


@pytest.mark.asyncio
async def test_build_runtime_can_disable_yaml_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langfuse
            provider:
              type: openai
              model: test-model
            observability:
              type: langfuse
              redaction:
                enabled: false
            """,
        )
    )
    calls: dict[str, Any] = {}

    class FakeLangfuseRegistry:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    async def fake_connect(spec, *, plugin):
        return object()

    def fake_from_env(**kwargs: Any):
        calls["trace_kwargs"] = kwargs
        return _FakeObservabilityBackend(client="langfuse-client", plugin="plugin")

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeLangfuseRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        fake_from_env,
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    await build_runtime(spec)

    payload = {"email": "jane.doe@example.com"}
    assert calls["trace_kwargs"]["redactor"].redact(payload) is payload


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_uses_yaml_task_queue_without_langfuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(load_yaml_spec(_write_demo_yaml(tmp_path)))

    from yaml_demo_project.schemas import InputModel, OutputModel

    result = await runtime.execute_workflow(InputModel(value="start"), id="wf-plain")

    assert result == OutputModel(value="done")
    assert fake_client.calls[0]["workflow"] is runtime.workflow_class.run
    assert fake_client.calls[0]["input_value"] == InputModel(value="start")
    assert fake_client.calls[0]["kwargs"]["memo"] == {
        "typeflux_spec_digest": runtime.workflow_class.__typeflux_spec_digest__,
        "typeflux_workflow": "DemoYamlWorkflow",
        "typeflux_project": "yaml_demo_project",
    }
    assert fake_client.calls[0]["kwargs"]["id"] == "wf-plain"
    assert fake_client.calls[0]["kwargs"]["task_queue"] == "demo-task-queue"
    assert fake_client.calls[0]["kwargs"]["result_type"] is OutputModel


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_merges_identity_memo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        assert plugin is None
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(load_yaml_spec(_write_demo_yaml(tmp_path)))

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(
        InputModel(value="start"),
        id="wf-with-memo",
        memo={
            "caller_key": "caller-value",
            "typeflux_spec_digest": "stale-digest",
        },
    )

    assert fake_client.calls[0]["kwargs"]["memo"] == {
        "caller_key": "caller-value",
        "typeflux_spec_digest": runtime.workflow_class.__typeflux_spec_digest__,
        "typeflux_workflow": "DemoYamlWorkflow",
        "typeflux_project": "yaml_demo_project",
    }
    # The logical-name search attribute is opt-in; absent by default.
    assert "search_attributes" not in fake_client.calls[0]["kwargs"]


_SEARCH_ATTRIBUTE_RUNTIME = """
temporal:
  address: localhost:7233
  workflow_search_attribute: TypefluxWorkflow
registry:
  type: inline
  prompts:
    first: first {{value}}
    second: second {{value}}
provider:
  type: fake
"""


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_attaches_logical_name_search_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(_write_demo_yaml(tmp_path, runtime=_SEARCH_ATTRIBUTE_RUNTIME))
    )

    from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes
    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="start"), id="wf-search-attr")

    attributes = fake_client.calls[0]["kwargs"]["search_attributes"]
    assert isinstance(attributes, TypedSearchAttributes)
    assert [(pair.key.name, pair.value) for pair in attributes] == [
        ("TypefluxWorkflow", "DemoYamlWorkflow")
    ]

    # Caller-supplied attributes are preserved; the Typeflux key wins.
    caller_attrs = TypedSearchAttributes(
        [
            SearchAttributePair(SearchAttributeKey.for_keyword("Team"), "core"),
            SearchAttributePair(SearchAttributeKey.for_keyword("TypefluxWorkflow"), "stale"),
        ]
    )
    await runtime.execute_workflow(
        InputModel(value="start"),
        id="wf-search-attr-merge",
        search_attributes=caller_attrs,
    )

    merged = fake_client.calls[1]["kwargs"]["search_attributes"]
    assert sorted((pair.key.name, pair.value) for pair in merged) == [
        ("Team", "core"),
        ("TypefluxWorkflow", "DemoYamlWorkflow"),
    ]


_SUBJECTS_RUNTIME = """
temporal:
  address: localhost:7233
registry:
  type: inline
  prompts:
    first: first {{value}}
    second: second {{value}}
provider:
  type: fake
"""


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_stamps_subject_ids_search_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                runtime=_SUBJECTS_RUNTIME,
                subjects="- from: input.value",
            )
        )
    )

    from temporalio.common import SearchAttributeKey, TypedSearchAttributes
    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="pt-42"), id="wf-subjects")

    attributes = fake_client.calls[0]["kwargs"]["search_attributes"]
    assert isinstance(attributes, TypedSearchAttributes)
    key = SearchAttributeKey.for_keyword_list("TypefluxSubjectIds")
    assert attributes.get(key) == ["pt-42"]


@pytest.mark.asyncio
async def test_yaml_runtime_explicit_subject_ids_override_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                runtime=_SUBJECTS_RUNTIME,
                subjects="- from: input.value",
            )
        )
    )

    from temporalio.common import SearchAttributeKey
    from yaml_demo_project.schemas import InputModel

    # The declarative selector would yield 'pt-42'; the explicit override wins.
    await runtime.execute_workflow(
        InputModel(value="pt-42"),
        id="wf-subjects-override",
        subject_ids=["explicit-a", "explicit-b"],
    )

    attributes = fake_client.calls[0]["kwargs"]["search_attributes"]
    key = SearchAttributeKey.for_keyword_list("TypefluxSubjectIds")
    assert attributes.get(key) == ["explicit-a", "explicit-b"]


@pytest.mark.asyncio
async def test_yaml_runtime_empty_subject_override_falls_through_to_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An EMPTY explicit override is "no override provided": subjects are
    # erasure-critical, so `subject_ids=[]` must never silently bypass a declared
    # `subjects:` block (#715 review round 2). Extraction runs — and stamps.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                runtime=_SUBJECTS_RUNTIME,
                subjects="- from: input.value",
            )
        )
    )

    from temporalio.common import SearchAttributeKey
    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(
        InputModel(value="pt-42"), id="wf-empty-override", subject_ids=[]
    )
    attributes = fake_client.calls[0]["kwargs"]["search_attributes"]
    key = SearchAttributeKey.for_keyword_list("TypefluxSubjectIds")
    assert attributes.get(key) == ["pt-42"]

    # ...and extraction's validation still fires through an empty override (an
    # honored `[]` override would have skipped it entirely).
    with pytest.raises(ValueError, match="non-empty-string"):
        await runtime.execute_workflow(
            InputModel(value="   "), id="wf-empty-override-2", subject_ids=[]
        )


@pytest.mark.asyncio
async def test_yaml_runtime_empty_override_without_subjects_block_is_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No `subjects:` block + an empty override ⇒ unchanged pre-#715 behavior
    # (no TypefluxSubjectIds attribute stamped).
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(_write_demo_yaml(tmp_path, runtime=_SUBJECTS_RUNTIME))
    )

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(
        InputModel(value="pt-42"), id="wf-empty-override-inert", subject_ids=[]
    )
    assert "search_attributes" not in fake_client.calls[0]["kwargs"]


@pytest.mark.asyncio
async def test_yaml_runtime_missing_required_subject_raises_at_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                runtime=_SUBJECTS_RUNTIME,
                subjects="- from: input.absent_field",
            )
        )
    )

    from yaml_demo_project.schemas import InputModel

    # A required selector whose path is missing is a loud error at start — NOT a
    # silent no-subject start (an un-indexed execution is invisible to erasure).
    with pytest.raises(ValueError, match="resolved to no value"):
        await runtime.execute_workflow(InputModel(value="pt-42"), id="wf-subjects-missing")
    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_yaml_runtime_subjects_coexist_with_logical_name_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                runtime=_SEARCH_ATTRIBUTE_RUNTIME,
                subjects="- from: input.value",
            )
        )
    )

    from temporalio.common import SearchAttributeKey
    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="pt-9"), id="wf-both-attrs")

    attributes = fake_client.calls[0]["kwargs"]["search_attributes"]
    names = {(pair.key.name) for pair in attributes}
    assert names == {"TypefluxWorkflow", "TypefluxSubjectIds"}
    assert attributes.get(SearchAttributeKey.for_keyword_list("TypefluxSubjectIds")) == ["pt-9"]
    assert attributes.get(SearchAttributeKey.for_keyword("TypefluxWorkflow")) == "DemoYamlWorkflow"


def test_yaml_subjects_block_parses_to_selectors(tmp_path: Path) -> None:
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            subjects="""
            - from: input.value
            - from: input.other
              required: false
            """,
        )
    )
    assert [(s.from_path, s.required) for s in spec.workflow.subjects] == [
        ("input.value", True),
        ("input.other", False),
    ]


def test_yaml_subjects_reject_non_input_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must start with 'input.'"):
        load_yaml_spec(_write_demo_yaml(tmp_path, subjects="- from: context.value"))


def test_yaml_subjects_reject_unknown_field(tmp_path: Path) -> None:
    # spec-model-equals-wired: an unrecognised key on a subject selector is a
    # loud parse error (extra="forbid"), not a silently-ignored stub.
    with pytest.raises(ValueError):
        load_yaml_spec(_write_demo_yaml(tmp_path, subjects="- from: input.value\n  bogus: 1"))


def test_yaml_subjects_stay_out_of_spec_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Subjects are submit-time metadata with no control-flow effect, so — like
    # risk_tier — they must NOT shift the workflow spec digest (the same YAML
    # through the same generator is the same program regardless of the subject
    # selector), keeping every existing versioned type / digest golden stable.
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))

    def _digest(subjects: str | None) -> str:
        spec = load_yaml_spec(_write_demo_yaml(tmp_path, subjects=subjects))
        return create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

    assert _digest(None) == _digest("- from: input.value")


def test_yaml_workflow_search_attribute_rejects_invalid_names(tmp_path: Path) -> None:
    path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
          workflow_search_attribute: "not valid!"
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: fake
        """,
    )

    with pytest.raises(ValueError, match="workflow_search_attribute"):
        load_yaml_spec(path, load_dotenv=False)


@pytest.mark.asyncio
async def test_yaml_submit_cli_validates_input_and_uses_runtime_start_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    fake_client = _FakeTemporalClient()
    fake_langfuse = _FakeLangfuseClient()

    class FakeLangfuseRegistry:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    async def fake_connect(spec, *, plugin):
        assert plugin == "plugin"
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeLangfuseRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        lambda **kwargs: _FakeObservabilityBackend(client=fake_langfuse, plugin="plugin"),
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    spec_path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: langfuse
        provider:
          type: openai
          model: test-model
        observability:
          type: langfuse
        """,
    )
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "start"}', encoding="utf-8")

    exit_code = await yaml_submit._amain(
        str(spec_path),
        input_path=str(input_path),
        workflow_id="wf-submitted",
        task_queue="override-queue",
        tags=("smoke", "submit"),
        metadata_json='{"source": "cli"}',
    )

    from yaml_demo_project.schemas import InputModel, OutputModel

    assert exit_code == 0
    # The traced submit path starts the workflow and awaits the handle result.
    assert fake_client.start_calls[0]["workflow"]
    assert fake_client.start_calls[0]["input_value"] == InputModel(value="start")
    assert fake_client.start_calls[0]["kwargs"]["id"] == "wf-submitted"
    assert fake_client.start_calls[0]["kwargs"]["task_queue"] == "override-queue"
    assert fake_client.start_calls[0]["kwargs"]["result_type"] is OutputModel
    metadata = fake_langfuse.start_calls[0]["metadata"]
    assert metadata["source"] == "cli"
    assert set(metadata["tags"]) >= {
        "smoke",
        "submit",
        "typeflux",
        "typeflux.workflow:DemoYamlWorkflow",
    }
    assert metadata["typeflux"]["workflow"]["task_queue"] == "override-queue"
    assert json.loads(capsys.readouterr().out) == {"value": "done"}


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_rolls_up_langfuse_trace_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_TEMPORAL_REGION", "local-dev")
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)

    from yaml_demo_project.schemas import InputModel, OutputModel

    result = await runtime.execute_workflow(
        InputModel(value="start"),
        id="wf-traced",
        tags=["demo"],
        metadata={"custom": "value"},
    )

    assert result == OutputModel(value="done")
    # Traced execution goes through the start path to capture the run id.
    assert fake_client.start_calls[0]["kwargs"]["id"] == "wf-traced"
    assert fake_langfuse.start_calls[0]["name"] == "TypefluxWorkflow:DemoYamlWorkflow"
    assert fake_langfuse.start_calls[0]["input"] == {"value": "start"}
    metadata = fake_langfuse.start_calls[0]["metadata"]
    assert metadata["custom"] == "value"
    assert set(metadata["tags"]) >= {
        "demo",
        "typeflux",
        "typeflux.workflow:DemoYamlWorkflow",
        "typeflux.activity:first",
        "typeflux.activity:second",
        "typeflux.prompt:first",
        "typeflux.prompt:second",
    }
    yaml_metadata = metadata["typeflux"]["yaml"]
    spec_digest = runtime.workflow_class.__typeflux_spec_digest__
    assert yaml_metadata == {
        "name": "demo_yaml",
        "project": "yaml_demo_project",
        "spec_digest": spec_digest,
        "spec_digest_algorithm": "typeflux-yaml-graph-v1",
        "generator_version": "5",
        "workflow_type": f"DemoYamlWorkflow.{spec_digest[:12]}",
    }
    assert metadata["typeflux"]["workflow"] == {
        "workflow_name": "DemoYamlWorkflow",
        "workflow_id": "wf-traced",
        "task_queue": "demo-task-queue",
    }
    assert metadata["typeflux"]["temporal_connection"] == {
        "address": "localhost:7233",
        "namespace": "default",
        "region": "local-dev",
        "tls_enabled": False,
        "tls_mode": "disabled",
        "api_key_configured": False,
    }
    assert (
        metadata["typeflux"]["execution_manifest"]["contributions"]["temporal_connection"]
        == metadata["typeflux"]["temporal_connection"]
    )
    assert metadata["typeflux"]["activities"] == ["first", "second"]
    execution_activities = metadata["typeflux"]["execution_manifest"]["activities"]
    assert [item["activity_name"] for item in execution_activities] == ["first", "second"]
    assert execution_activities[0]["prompt_resolution"]["status"] == "failed"
    assert execution_activities[1]["prompt_resolution"]["status"] == "failed"
    assert fake_langfuse.trace_io_calls == [
        {"input": {"value": "start"}},
        {"output": {"value": "done"}},
    ]
    assert fake_langfuse.observations[0].updates[-1] == {"output": {"value": "done"}}


@pytest.mark.asyncio
async def test_yaml_runtime_policy_guard_is_safe_in_metadata_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_guard = RuntimePolicyGuard(
        policy=ComposedProjectPolicy(
            selected_policy_ids=("regulated",),
            applied_policy_ids=("base", "regulated"),
            policy_names=("base", "regulated"),
            policy_hash="c" * 64,
            payload={
                "version": "1",
                "selected_policy_ids": ["regulated"],
                "applied_policy_ids": ["base", "regulated"],
                "policy_names": ["base", "regulated"],
            },
        ),
        provider_name="fake",
        enforcement_mode="project_submit",
    )
    runtime, _fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(
        tmp_path,
        monkeypatch,
        policy_guard=policy_guard,
    )

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="start"), id="wf-policy")

    metadata = fake_langfuse.start_calls[0]["metadata"]
    expected_manifest = {
        "version": "1",
        "selected_policy_ids": ["regulated"],
        "applied_policy_ids": ["base", "regulated"],
        "policy_names": ["base", "regulated"],
        "policy_hash": "c" * 64,
        "admission_status": "passed",
    }
    assert metadata["typeflux"]["policy"] == {
        **expected_manifest,
        "enforcement_mode": "project_submit",
    }
    assert (
        metadata["typeflux"]["execution_manifest"]["contributions"]["policy"] == expected_manifest
    )
    assert "typeflux.policy:regulated" not in metadata["tags"]
    assert "c" * 64 not in metadata["tags"]


@pytest.mark.asyncio
async def test_yaml_runtime_override_provenance_is_safe_in_metadata_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(
        tmp_path,
        monkeypatch,
        overrides={
            "task_queue": "override-queue",
            "runtime": {
                "provider": {
                    "api_key": "secret-openai-key",
                    "model": "override-model",
                }
            },
        },
    )

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="start"), id="wf-overrides")

    metadata = fake_langfuse.start_calls[0]["metadata"]
    override_payload = {
        "source": "load_yaml_spec",
        "override_paths": [
            "runtime.provider.api_key",
            "runtime.provider.model",
            "task_queue",
        ],
    }
    assert metadata["typeflux"]["yaml_overrides"] == override_payload
    assert (
        metadata["typeflux"]["execution_manifest"]["contributions"]["yaml_overrides"]
        == override_payload
    )
    assert "secret-openai-key" not in json.dumps(metadata)


@pytest.mark.asyncio
async def test_yaml_runtime_secret_references_are_safe_in_metadata_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_API_KEY", "temporal-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-value")
    runtime, _fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(
        tmp_path,
        monkeypatch,
        overrides={
            "runtime": {
                "temporal": {
                    "tls": True,
                    "api_key": {
                        "value_from": {
                            "env": "TEMPORAL_API_KEY",
                        }
                    },
                },
                "provider": {
                    "api_key": {
                        "value_from": {
                            "env": "OPENAI_API_KEY",
                        }
                    },
                },
            },
        },
    )

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="start"), id="wf-secret-refs")

    metadata = fake_langfuse.start_calls[0]["metadata"]
    references = sorted(
        metadata["typeflux"]["secret_references"]["references"],
        key=lambda item: item["runtime_path"],
    )
    assert references == [
        {
            "runtime_path": "runtime.provider.api_key",
            "source_kind": "env",
            "source_name": "OPENAI_API_KEY",
            "configured": True,
        },
        {
            "runtime_path": "runtime.temporal.api_key",
            "source_kind": "env",
            "source_name": "TEMPORAL_API_KEY",
            "configured": True,
        },
    ]
    assert (
        metadata["typeflux"]["execution_manifest"]["contributions"]["secret_references"]
        == metadata["typeflux"]["secret_references"]
    )
    assert metadata["typeflux"]["temporal_connection"]["api_key_configured"] is True
    metadata_json = json.dumps(metadata)
    assert "temporal-secret-value" not in metadata_json
    assert "openai-secret-value" not in metadata_json


@pytest.mark.asyncio
async def test_yaml_runtime_policy_guard_uses_openai_default_model_when_omitted(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    class RecordingGuard:
        def enforce_provider_model(
            self,
            *,
            provider_name: str,
            provider_model: str | None,
            activity_name: str | None = None,
            prompt_name: str | None = None,
        ) -> None:
            seen["provider_name"] = provider_name
            seen["provider_model"] = provider_model
            raise RuntimeError("stop before runtime construction")

    with pytest.raises(RuntimeError, match="stop before runtime construction"):
        await build_runtime(
            load_yaml_spec(
                _write_demo_yaml(
                    tmp_path,
                    runtime="""
                    temporal:
                      address: localhost:7233
                    registry:
                      type: inline
                      prompts:
                        first: first {{value}}
                        second: second {{value}}
                    provider:
                      type: openai
                    """,
                )
            ),
            policy_guard=RecordingGuard(),
        )

    assert seen == {"provider_name": "openai", "provider_model": "gpt-4o-mini"}


@pytest.mark.asyncio
async def test_yaml_runtime_policy_guard_uses_anthropic_default_model_when_omitted(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    class RecordingGuard:
        def enforce_provider_model(
            self,
            *,
            provider_name: str,
            provider_model: str | None,
            activity_name: str | None = None,
            prompt_name: str | None = None,
        ) -> None:
            seen["provider_name"] = provider_name
            seen["provider_model"] = provider_model
            raise RuntimeError("stop before runtime construction")

    with pytest.raises(RuntimeError, match="stop before runtime construction"):
        await build_runtime(
            load_yaml_spec(
                _write_demo_yaml(
                    tmp_path,
                    runtime="""
                    temporal:
                      address: localhost:7233
                    registry:
                      type: inline
                      prompts:
                        first: first {{value}}
                        second: second {{value}}
                    provider:
                      type: anthropic
                    """,
                )
            ),
            policy_guard=RecordingGuard(),
        )

    assert seen == {"provider_name": "anthropic", "provider_model": "claude-sonnet-4-6"}


def test_provider_spec_materializes_builtin_default_models_when_omitted(
    tmp_path: Path,
) -> None:
    from typeflux.yaml.spec import DEFAULT_ANTHROPIC_MODEL, DEFAULT_OPENAI_MODEL

    openai_dir = tmp_path / "openai"
    anthropic_dir = tmp_path / "anthropic"
    fake_dir = tmp_path / "fake"
    openai_dir.mkdir()
    anthropic_dir.mkdir()
    fake_dir.mkdir()

    openai_spec = load_yaml_spec(
        _write_demo_yaml(
            openai_dir,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: openai
            """,
        )
    )
    # The default is materialized into the resolved spec (visible/auditable), not
    # left implicit for downstream readers to re-derive differently.
    assert openai_spec.runtime.provider.model == DEFAULT_OPENAI_MODEL

    anthropic_spec = load_yaml_spec(
        _write_demo_yaml(
            anthropic_dir,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: anthropic
            """,
        )
    )
    assert anthropic_spec.runtime.provider.model == DEFAULT_ANTHROPIC_MODEL

    fake_spec = load_yaml_spec(
        _write_demo_yaml(
            fake_dir,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: inline
              prompts:
                first: first {{value}}
                second: second {{value}}
            provider:
              type: fake
            """,
        )
    )
    # Materialization is explicit for real built-ins; fake remains model-less.
    assert fake_spec.runtime.provider.model is None


@pytest.mark.asyncio
async def test_yaml_runtime_temporal_connection_metadata_handles_structured_tls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TYPEFLUX_TEMPORAL_REGION", "us-east")
    fake_client = _FakeTemporalClient()
    fake_langfuse = _FakeLangfuseClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    monkeypatch.setattr(
        "typeflux.yaml.runtime._build_observability",
        lambda spec, **_kwargs: _FakeObservabilityBackend(client=fake_langfuse, plugin=None),
    )
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                runtime="""
                temporal:
                  address: namespace.tmprl.cloud:7233
                  namespace: namespace
                  tls:
                    domain: namespace.tmprl.cloud
                    server_root_ca_cert_file: /secrets/ca.pem
                    client_cert_file: /secrets/client.pem
                    client_private_key_file: /secrets/client-key.pem
                  api_key: configured
                registry:
                  type: inline
                  prompts:
                    first: first {{value}}
                    second: second {{value}}
                provider:
                  type: fake
                observability:
                  type: langfuse
                """,
            )
        )
    )

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="start"), id="wf-structured-tls")

    metadata = fake_langfuse.start_calls[0]["metadata"]
    assert metadata["typeflux"]["temporal_connection"] == {
        "address": "namespace.tmprl.cloud:7233",
        "namespace": "namespace",
        "region": "us-east",
        "tls_enabled": True,
        "tls_mode": "custom",
        "api_key_configured": True,
    }
    metadata_json = json.dumps(metadata)
    assert "/secrets/ca.pem" not in metadata_json
    assert "/secrets/client.pem" not in metadata_json
    assert "/secrets/client-key.pem" not in metadata_json


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_includes_map_step_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_map_demo_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    fake_client = _FakeTemporalClient()
    fake_langfuse = _FakeLangfuseClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    monkeypatch.setattr(
        "typeflux.yaml.runtime._build_observability",
        lambda spec, **_kwargs: _FakeObservabilityBackend(client=fake_langfuse, plugin=None),
    )
    runtime = await build_runtime(load_yaml_spec(_write_map_demo_yaml(tmp_path)))

    from yaml_map_project.schemas import InputModel

    await runtime.execute_workflow(
        InputModel(
            value="root",
            pages=[
                {"value": "first"},
                {"value": "second"},
                {"value": "third"},
            ],
        ),
        id="wf-map-traced",
    )

    metadata = fake_langfuse.start_calls[0]["metadata"]
    assert metadata["typeflux"]["yaml"]["map_steps"] == [
        {
            "map_step_id": "review_pages",
            "activity_name": "review_page",
            "over": "input.pages",
            "map_size": 3,
            "map_concurrency": 2,
            "collect_output": "PageReviewBatch",
            "collect_field": "reviews",
        }
    ]
    assert metadata["typeflux"]["execution_manifest"]["map_steps"] == [
        {
            "map_step_id": "review_pages",
            "activity_name": "review_page",
            "over": "input.pages",
            "map_size": 3,
            "map_concurrency": 2,
            "collect_output": "PageReviewBatch",
            "collect_field": "reviews",
        }
    ]
    assert metadata["typeflux"]["execution_manifest"]["contributions"]["yaml"]["map_steps"] == [
        {
            "map_step_id": "review_pages",
            "activity_name": "review_page",
            "over": "input.pages",
            "map_size": 3,
            "map_concurrency": 2,
            "collect_output": "PageReviewBatch",
            "collect_field": "reviews",
        }
    ]


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_includes_lifecycle_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    fake_client = _FakeTemporalClient()
    fake_langfuse = _FakeLangfuseClient()

    async def fake_connect(spec, *, plugin):
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    monkeypatch.setattr(
        "typeflux.yaml.runtime._build_observability",
        lambda spec, **_kwargs: _FakeObservabilityBackend(client=fake_langfuse, plugin=None),
    )
    runtime = await build_runtime(
        load_yaml_spec(
            _write_demo_yaml(
                tmp_path,
                lifecycle="""
                enabled: true
                progress: true
                cancellation: true
                review:
                  after_step: first
                  user_decisions:
                    send_email:
                      route: second
                """,
            )
        )
    )

    from yaml_demo_project.schemas import InputModel

    await runtime.execute_workflow(InputModel(value="start"), id="wf-lifecycle-traced")

    metadata = fake_langfuse.start_calls[0]["metadata"]
    assert metadata["typeflux"]["lifecycle"] == {
        "state": "pending",
        "current_step": None,
        "completed_units": 0,
        "total_units": 2,
        "cancellation_requested": False,
        "waiting_checkpoint": None,
        "terminal_status": None,
        "status_event_limit": 50,
        "review_after_step": "first",
    }


@pytest.mark.asyncio
async def test_yaml_runtime_query_lifecycle_status_records_operation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = _fake_bound_handle(fake_client, "wf-lifecycle-query")
    handle.lifecycle_status = WorkflowLifecycleStatus(
        state="waiting_for_review",
        current_step="first",
        completed_units=1,
        total_units=2,
        cancellation_reason="sensitive reason",
        waiting_checkpoint="first",
        event_count=3,
        events_truncated=False,
    )

    status = await runtime.query_lifecycle_status("wf-lifecycle-query")

    assert status.state == "waiting_for_review"
    assert handle.queries == [
        {
            "name": "typeflux_lifecycle_status",
            "kwargs": {"result_type": WorkflowLifecycleStatus},
        }
    ]
    start_operation = fake_langfuse.start_calls[-1]["metadata"]["typeflux"]["lifecycle_operation"]
    operation = fake_langfuse.observations[-1].updates[-1]["metadata"]["typeflux"][
        "lifecycle_operation"
    ]
    assert operation["operation_type"] == "query"
    assert operation["operation_name"] == "typeflux_lifecycle_status"
    assert operation["workflow_name"] == "DemoYamlWorkflow"
    assert operation["workflow_id"] == "wf-lifecycle-query"
    assert operation["run_id"] == "run-wf-lifecycle-query"
    assert "status" not in start_operation
    assert operation["status"] == {
        "state": "waiting_for_review",
        "current_step": "first",
        "completed_units": 1,
        "total_units": 2,
        "waiting_checkpoint": "first",
        "cancellation_requested": False,
        "event_count": 3,
        "events_truncated": False,
    }
    assert "sensitive reason" not in str(operation)
    assert set(fake_langfuse.start_calls[-1]["tags"]) >= {
        "typeflux",
        "typeflux.lifecycle",
        "typeflux.lifecycle.query:typeflux_lifecycle_status",
        "typeflux.workflow:DemoYamlWorkflow",
    }


@pytest.mark.asyncio
async def test_yaml_runtime_lifecycle_signals_record_safe_operation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = _fake_bound_handle(fake_client, "wf-lifecycle-review")

    await runtime.submit_lifecycle_review(
        "wf-lifecycle-review",
        ReviewCommand(
            user_decision="send_email",
            reviewer="reviewer@example.com",
            notes="call 555-123-4567",
        ),
    )
    await runtime.request_lifecycle_cancel("wf-lifecycle-review", "cancel 555-123-4567")

    assert handle.signals == [
        {
            "name": "typeflux_submit_review",
            "args": (
                ReviewCommand(
                    user_decision="send_email",
                    reviewer="reviewer@example.com",
                    notes="call 555-123-4567",
                ),
            ),
        },
        {
            "name": "typeflux_request_cancel",
            "args": ("cancel 555-123-4567",),
        },
    ]
    review_operation = fake_langfuse.start_calls[-2]["metadata"]["typeflux"]["lifecycle_operation"]
    cancel_operation = fake_langfuse.start_calls[-1]["metadata"]["typeflux"]["lifecycle_operation"]
    assert review_operation["review_user_decision"] == "send_email"
    assert review_operation["review_route_target"] == "second"
    assert "reviewer@example.com" not in str(review_operation)
    assert "555-123-4567" not in str(review_operation)
    assert cancel_operation["cancellation_requested"] is True
    assert "cancel 555-123-4567" not in str(cancel_operation)


@pytest.mark.asyncio
async def test_yaml_runtime_untraced_lifecycle_status_query_emits_no_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = _fake_bound_handle(fake_client, "wf-lifecycle-untraced")
    handle.lifecycle_status = WorkflowLifecycleStatus(state="waiting_for_review")
    baseline_start_calls = len(fake_langfuse.start_calls)

    status = await runtime.query_lifecycle_status("wf-lifecycle-untraced", trace=False)

    assert status.state == "waiting_for_review"
    assert handle.queries == [
        {
            "name": "typeflux_lifecycle_status",
            "kwargs": {"result_type": WorkflowLifecycleStatus},
        }
    ]
    assert len(fake_langfuse.start_calls) == baseline_start_calls


@pytest.mark.asyncio
async def test_lifecycle_op_dispatches_to_the_run_verified_by_describe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, _fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    id_only_handle = fake_client.get_workflow_handle("wf-reused-id")
    id_only_handle.describe_run_id = "run-verified"
    bound_handle = fake_client.get_workflow_handle("wf-reused-id", run_id="run-verified")
    bound_handle.lifecycle_status = WorkflowLifecycleStatus(state="waiting_for_review")

    status = await runtime.query_lifecycle_status("wf-reused-id", trace=False)

    assert status.state == "waiting_for_review"
    assert id_only_handle.queries == []
    assert bound_handle.queries == [
        {
            "name": "typeflux_lifecycle_status",
            "kwargs": {"result_type": WorkflowLifecycleStatus},
        }
    ]


@pytest.mark.asyncio
async def test_lifecycle_op_refuses_execution_with_wrong_workflow_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, _fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = fake_client.get_workflow_handle("wf-foreign-type")
    # An id collision: the execution at this id is a different workflow type.
    handle.describe_workflow_type = "SomeOtherWorkflow.deadbeef"

    with pytest.raises(LifecycleBindingError, match="not the bound type"):
        await runtime.query_lifecycle_status("wf-foreign-type")

    # Fail closed *before* the query — the foreign execution is never touched.
    assert handle.queries == []


@pytest.mark.asyncio
async def test_lifecycle_op_refuses_execution_from_another_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, _fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = fake_client.get_workflow_handle("wf-other-project")
    # Same workflow type, but the execution belongs to a different project.
    handle.describe_memo = {
        "typeflux_project": "some_other_project",
        "typeflux_workflow": "DemoYamlWorkflow",
    }

    with pytest.raises(LifecycleBindingError, match="not the bound project"):
        await runtime.query_lifecycle_status("wf-other-project")

    assert handle.queries == []


@pytest.mark.asyncio
async def test_lifecycle_op_refuses_execution_without_project_memo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, _fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = fake_client.get_workflow_handle("wf-no-memo")
    # Started outside the Typeflux runtime: no identity memo. Fail closed — no
    # legacy fallback (pre-adoption, #320).
    handle.describe_memo = {}

    with pytest.raises(LifecycleBindingError, match="not the bound project"):
        await runtime.request_lifecycle_cancel("wf-no-memo", "stop")

    assert handle.signals == []


@pytest.mark.asyncio
async def test_lifecycle_signals_verify_binding_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, _fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = fake_client.get_workflow_handle("wf-foreign-signal")
    handle.describe_workflow_type = "SomeOtherWorkflow.deadbeef"

    with pytest.raises(LifecycleBindingError):
        await runtime.submit_lifecycle_review(
            "wf-foreign-signal",
            ReviewCommand(user_decision="send_email"),
        )
    with pytest.raises(LifecycleBindingError):
        await runtime.request_lifecycle_cancel("wf-foreign-signal", "stop")

    assert handle.signals == []


@pytest.mark.asyncio
async def test_wait_for_lifecycle_state_fails_fast_on_binding_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, _fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = fake_client.get_workflow_handle("wf-wait-foreign")
    handle.describe_workflow_type = "SomeOtherWorkflow.deadbeef"

    # A binding mismatch is not transient: the wait fails immediately with the
    # binding error, not after polling to the timeout.
    with pytest.raises(LifecycleBindingError):
        await runtime.wait_for_lifecycle_state(
            "wf-wait-foreign",
            "waiting_for_review",
            timeout_seconds=5.0,
            poll_interval_seconds=0.01,
        )

    assert handle.queries == []


@pytest.mark.asyncio
async def test_yaml_runtime_wait_for_lifecycle_state_polls_untraced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = _fake_bound_handle(fake_client, "wf-lifecycle-wait")
    handle.lifecycle_status = WorkflowLifecycleStatus(state="waiting_for_review")
    # The worker may not be polling when the wait starts; transient query
    # failures must be tolerated until the deadline.
    original_query = handle.query
    failures = {"remaining": 1}

    async def flaky_query(name: str, **kwargs: Any) -> WorkflowLifecycleStatus:
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise RuntimeError("worker not ready")
        return await original_query(name, **kwargs)

    handle.query = flaky_query
    baseline_start_calls = len(fake_langfuse.start_calls)

    status = await runtime.wait_for_lifecycle_state(
        "wf-lifecycle-wait",
        "waiting_for_review",
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
    )

    assert status.state == "waiting_for_review"
    assert len(handle.queries) == 1
    assert len(fake_langfuse.start_calls) == baseline_start_calls


@pytest.mark.asyncio
async def test_yaml_runtime_wait_for_lifecycle_state_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    handle = _fake_bound_handle(fake_client, "wf-lifecycle-timeout")
    handle.lifecycle_status = WorkflowLifecycleStatus(state="running")
    baseline_start_calls = len(fake_langfuse.start_calls)

    with pytest.raises(TimeoutError, match="did not reach lifecycle state 'waiting_for_review'"):
        await runtime.wait_for_lifecycle_state(
            "wf-lifecycle-timeout",
            "waiting_for_review",
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )

    assert len(handle.queries) >= 1
    assert len(fake_langfuse.start_calls) == baseline_start_calls

    with pytest.raises(ValueError, match="poll_interval_seconds must be positive"):
        await runtime.wait_for_lifecycle_state(
            "wf-lifecycle-timeout",
            "waiting_for_review",
            poll_interval_seconds=0,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await runtime.wait_for_lifecycle_state(
            "wf-lifecycle-timeout",
            "waiting_for_review",
            timeout_seconds=0,
        )


@pytest.mark.asyncio
async def test_yaml_runtime_start_workflow_is_non_blocking_with_identity_memo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)

    from yaml_demo_project.schemas import InputModel

    handle = await runtime.start_workflow(InputModel(value="start"), id="wf-ops-start")

    assert handle.run_id == "run-wf-ops-start"
    start_call = fake_client.start_calls[0]
    assert start_call["kwargs"]["id"] == "wf-ops-start"
    assert start_call["kwargs"]["task_queue"] == runtime.spec.task_queue
    memo = start_call["kwargs"]["memo"]
    assert getattr(runtime.workflow_class, "__typeflux_spec_digest__") in str(memo)
    # Non-blocking start opens no root workflow observation.
    assert fake_langfuse.start_calls == []


@pytest.mark.asyncio
async def test_workflow_operations_start_status_review_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typeflux.project.operations import (
        RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS,
        WorkflowOperations,
    )

    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    ops = WorkflowOperations(runtime=runtime)

    from yaml_demo_project.schemas import InputModel

    receipt = await ops.start(InputModel(value="start"), workflow_id="wf-ops-1")

    assert receipt.workflow_id == "wf-ops-1"
    assert receipt.run_id == "run-wf-ops-1"
    assert receipt.workflow_name == "DemoYamlWorkflow"
    assert receipt.workflow_type.startswith("DemoYamlWorkflow.")
    assert receipt.spec_digest == getattr(runtime.workflow_class, "__typeflux_spec_digest__")
    assert receipt.task_queue == runtime.spec.task_queue
    assert receipt.trace_query_hint == {"workflow_id": "wf-ops-1", "limit": 1}

    handle = _fake_bound_handle(fake_client, "wf-ops-1")
    handle.lifecycle_status = WorkflowLifecycleStatus(state="waiting_for_review")
    baseline = len(fake_langfuse.start_calls)

    status = await ops.status("wf-ops-1")

    assert status.status.state == "waiting_for_review"
    assert status.valid_user_decisions == {"send_email": "second"}
    assert status.recommended_poll_interval_seconds == RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS
    # Default polling is untraced: no curated lifecycle operation recorded.
    assert len(fake_langfuse.start_calls) == baseline

    traced = await ops.status("wf-ops-1", trace=True)
    assert traced.status.state == "waiting_for_review"
    assert len(fake_langfuse.start_calls) == baseline + 1

    await ops.submit_review(
        "wf-ops-1",
        ReviewCommand(user_decision="send_email", reviewer="ops-ui", notes="approved"),
    )
    await ops.request_cancel("wf-ops-1", "operator requested")

    assert [signal["name"] for signal in handle.signals] == [
        "typeflux_submit_review",
        "typeflux_request_cancel",
    ]
    # Explicit actions stay traced.
    assert len(fake_langfuse.start_calls) == baseline + 3
    # Control-plane operations never enter workflow execution manifests, and
    # reviewer identity / freeform notes / cancellation reasons stay out of
    # typeflux.* metadata.
    for call in fake_langfuse.start_calls:
        typeflux = call["metadata"].get("typeflux", {})
        assert "execution_manifest" not in typeflux
        assert "ops-ui" not in str(call["metadata"])
        assert "approved" not in str(call["metadata"])
        assert "operator requested" not in str(call["metadata"])


@pytest.mark.asyncio
async def test_yaml_runtime_execute_workflow_marks_langfuse_root_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake_client, fake_langfuse = await _build_langfuse_yaml_runtime(tmp_path, monkeypatch)
    fake_client.error = RuntimeError("workflow exploded")

    from yaml_demo_project.schemas import InputModel

    with pytest.raises(RuntimeError, match="workflow exploded"):
        await runtime.execute_workflow(InputModel(value="start"), id="wf-error")

    # Status messages are sanitized to the exception type; raw exception text
    # can carry user or provider content outside the redaction boundary.
    assert fake_langfuse.observations[0].updates[-1] == {
        "level": "ERROR",
        "status_message": "RuntimeError",
    }
    assert fake_langfuse.flush_count == 1


@pytest.mark.asyncio
async def test_yaml_run_preflight_rejects_disallowed_provider_class(
    tmp_path: Path,
) -> None:
    spec_path = _write_demo_yaml(
        tmp_path,
        runtime="""
        temporal:
          address: localhost:7233
        registry:
          type: inline
          prompts:
            first: first {{value}}
            second: second {{value}}
        provider:
          type: custom
          class: yaml_demo_project.providers:RecordingProvider
        """,
    )

    with pytest.raises(ValueError, match="allow_provider_class"):
        await yaml_run._amain(str(spec_path), preflight=True)


@pytest.mark.asyncio
async def test_yaml_run_shutdowns_observability_writer_when_worker_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _RecordingTraceWriter()
    runtime = _RunRuntime(writer=writer, error=RuntimeError("worker stopped"))

    monkeypatch.setattr(yaml_run, "load_yaml_spec", lambda path: object())
    monkeypatch.setattr(yaml_run, "build_runtime", _async_return(runtime))

    with pytest.raises(RuntimeError, match="worker stopped"):
        await yaml_run._amain("typeflux.yaml")

    assert writer.shutdown_count == 1
    assert writer.flush_count == 0


def test_support_triage_langfuse_yaml_loads_and_generates_workflow() -> None:
    spec = load_yaml_spec("examples/support_triage_langfuse/typeflux.yaml")
    activities = collect_activities(spec)
    workflow_cls = create_workflow(spec, activities)

    assert list(activities) == [
        "classify_ticket",
        "route_ticket",
        "draft_response",
        "package_for_review",
    ]
    from temporalio import workflow

    definition = workflow._Definition.must_from_class(workflow_cls)
    spec_digest = workflow_cls.__typeflux_spec_digest__
    assert definition.name == f"SupportTriageYamlWorkflow.{spec_digest[:12]}"
    assert workflow_cls.__typeflux_workflow_name__ == "SupportTriageYamlWorkflow"


def _demo_spec_digest(tmp_path: Path, **demo_kwargs: Any) -> str:
    spec = load_yaml_spec(_write_demo_yaml(tmp_path, **demo_kwargs))
    workflow_cls = create_workflow(spec, collect_activities(spec))
    return workflow_cls.__typeflux_spec_digest__


def test_spec_digest_changes_for_replay_breaking_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    baseline = _demo_spec_digest(tmp_path)

    renamed_step = _demo_spec_digest(
        tmp_path,
        steps="""
        - id: first_renamed
          activity: first
        - id: second
          activity: second
        """,
    )
    removed_step = _demo_spec_digest(
        tmp_path,
        workflow_output="schemas:MiddleModel",
        steps="""
        - id: first
          activity: first
        """,
    )
    review_added = _demo_spec_digest(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: first
          user_decisions:
            send_email:
              route: second
        """,
    )
    review_rerouted = _demo_spec_digest(
        tmp_path,
        lifecycle="""
        enabled: true
        review:
          after_step: first
          user_decisions:
            resend:
              route: second
        """,
    )
    digests = [baseline, renamed_step, removed_step, review_added, review_rerouted]
    assert len(set(digests)) == len(digests)


def test_spec_digest_is_stable_for_replay_safe_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    baseline = _demo_spec_digest(tmp_path)

    different_task_queue = _demo_spec_digest(tmp_path, task_queue="other-task-queue")
    different_prompts = _demo_spec_digest(
        tmp_path,
        runtime="""
        temporal:
          address: temporal.internal:7233
        registry:
          type: inline
          prompts:
            first: rewritten first prompt {{value}}
            second: rewritten second prompt {{value}}
        provider:
          type: fake
        """,
    )
    lifecycle_observability_only = _demo_spec_digest(
        tmp_path,
        lifecycle="""
        enabled: true
        progress: false
        history:
          status_event_limit: 5
        """,
    )
    lifecycle_baseline_shape = _demo_spec_digest(
        tmp_path,
        lifecycle="""
        enabled: true
        """,
    )

    assert different_task_queue == baseline
    assert different_prompts == baseline
    # Progress accounting and status history bounds do not alter generated
    # control flow, so they share one digest; enabling lifecycle does change it.
    assert lifecycle_observability_only == lifecycle_baseline_shape
    assert lifecycle_baseline_shape != baseline


def test_spec_digest_changes_for_map_shape_edits(tmp_path: Path) -> None:
    _write_map_demo_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:

        def map_digest(steps: str) -> str:
            spec = load_yaml_spec(_write_map_demo_yaml(tmp_path, steps=steps))
            return create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

        baseline = map_digest(
            """
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            - id: consolidate
              activity: consolidate_reviews
            """
        )
        different_concurrency = map_digest(
            """
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 3
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
            - id: consolidate
              activity: consolidate_reviews
            """
        )
        different_collect_limit = map_digest(
            """
            - id: review_pages
              map:
                activity: review_page
                over: input.pages
                concurrency: 2
                collect:
                  output: schemas:PageReviewBatch
                  field: reviews
                  max_bytes: 64
            - id: consolidate
              activity: consolidate_reviews
            """
        )
        assert different_concurrency != baseline
        # The collect payload guard changes control flow for a history, so it
        # participates in graph identity.
        assert different_collect_limit != baseline
    finally:
        sys.path.remove(str(tmp_path))


def test_workflow_version_label_freezes_registered_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path, workflow_version="v7"))
    workflow_cls = create_workflow(spec, collect_activities(spec))

    from temporalio import workflow

    definition = workflow._Definition.must_from_class(workflow_cls)
    assert definition.name == "DemoYamlWorkflow.v7"
    assert workflow_cls.__typeflux_workflow_type__ == "DemoYamlWorkflow.v7"
    assert workflow_cls.__typeflux_workflow_version_label__ == "v7"
    assert workflow_cls.__typeflux_spec_digest__


def test_workflow_version_label_rejects_invalid_values(tmp_path: Path) -> None:
    path = _write_demo_yaml(tmp_path, workflow_version="'bad label!'")

    with pytest.raises(ValueError, match="workflow.version"):
        load_yaml_spec(path, load_dotenv=False)


class _FakeVersionedExecution:
    def __init__(self, memo: dict[str, Any]) -> None:
        self._memo = memo

    async def memo(self) -> dict[str, Any]:
        return self._memo


class _FakeVisibilityClient:
    def __init__(
        self,
        executions: list[_FakeVersionedExecution],
        *,
        error: Exception | None = None,
    ) -> None:
        self.executions = executions
        self.error = error
        self.queries: list[str] = []

    def list_workflows(self, query: str, *, limit: int | None = None):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return _aiter(self.executions)


async def _aiter(items: list[Any]):
    for item in items:
        yield item


def _fake_run_id(workflow_id: str) -> str:
    return f"run-{workflow_id}"


def _fake_bound_handle(fake_client: _FakeTemporalClient, workflow_id: str) -> _FakeWorkflowHandle:
    return fake_client.get_workflow_handle(workflow_id, run_id=_fake_run_id(workflow_id))


def _versioned_workflow_class(spec_digest: str = "digest-a") -> Any:
    return SimpleNamespace(
        __typeflux_workflow_type__="DemoYamlWorkflow.v7",
        __typeflux_spec_digest__=spec_digest,
        __typeflux_workflow_version_label__="v7",
    )


@pytest.mark.asyncio
async def test_frozen_version_label_rejects_reuse_with_changed_digest() -> None:
    from typeflux.yaml.runtime import _enforce_frozen_version_label

    client = _FakeVisibilityClient(
        [_FakeVersionedExecution({"typeflux_spec_digest": "digest-old"})]
    )

    with pytest.raises(ValueError, match="frozen to spec digest"):
        await _enforce_frozen_version_label(client, _versioned_workflow_class("digest-new"))

    assert client.queries == ["WorkflowType = 'DemoYamlWorkflow.v7'"]


@pytest.mark.asyncio
async def test_frozen_version_label_accepts_matching_digest_and_skips_unknown() -> None:
    from typeflux.yaml.runtime import _enforce_frozen_version_label

    matching = _FakeVisibilityClient(
        [_FakeVersionedExecution({"typeflux_spec_digest": "digest-a"})]
    )
    no_history = _FakeVisibilityClient([])
    no_memo = _FakeVisibilityClient([_FakeVersionedExecution({})])
    failing = _FakeVisibilityClient([], error=RuntimeError("no visibility"))

    await _enforce_frozen_version_label(matching, _versioned_workflow_class("digest-a"))
    await _enforce_frozen_version_label(no_history, _versioned_workflow_class("digest-a"))
    await _enforce_frozen_version_label(no_memo, _versioned_workflow_class("digest-a"))
    await _enforce_frozen_version_label(failing, _versioned_workflow_class("digest-a"))


class _FakeTemporalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.start_calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.handles: dict[tuple[str, str | None], _FakeWorkflowHandle] = {}
        # The identity every vended handle reports from describe() (#320). Set
        # via bind_to() once the runtime exists so lifecycle ops pass binding;
        # a test can override a single handle's describe_* to simulate a
        # foreign execution / id collision.
        self.bound_workflow_type: str | None = None
        self.bound_memo: dict[str, Any] = {}

    def bind_to(self, workflow_class: Any) -> None:
        self.bound_workflow_type = getattr(workflow_class, "__typeflux_workflow_type__", None)
        self.bound_memo = {
            "typeflux_project": getattr(workflow_class, "__typeflux_project__", None)
        }

    async def start_workflow(self, workflow, input_value, **kwargs):
        self.start_calls.append(
            {
                "workflow": workflow,
                "input_value": input_value,
                "kwargs": kwargs,
            }
        )
        if self.error is not None:
            raise self.error
        handle = self.get_workflow_handle(kwargs["id"], run_id=_fake_run_id(kwargs["id"]))
        handle.result_payload_type = kwargs.get("result_type")
        return handle

    async def execute_workflow(self, workflow, input_value, **kwargs):
        self.calls.append(
            {
                "workflow": workflow,
                "input_value": input_value,
                "kwargs": kwargs,
            }
        )
        if self.error is not None:
            raise self.error
        result_type = kwargs.get("result_type")
        return result_type(value="done")

    def get_workflow_handle(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
    ) -> _FakeWorkflowHandle:
        key = (workflow_id, run_id)
        if key not in self.handles:
            self.handles[key] = _FakeWorkflowHandle(
                workflow_id=workflow_id, run_id=run_id, client=self
            )
        return self.handles[key]


class _FakeWorkflowDescription:
    def __init__(
        self,
        *,
        workflow_type: str | None,
        memo: dict[str, Any],
        run_id: str | None,
    ) -> None:
        self.workflow_type = workflow_type
        self._memo = memo
        self.run_id = run_id

    async def memo(self) -> dict[str, Any]:
        return self._memo


class _FakeWorkflowHandle:
    def __init__(
        self,
        *,
        workflow_id: str,
        run_id: str | None,
        client: _FakeTemporalClient | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.id = workflow_id
        self.run_id = run_id
        self.result_payload_type: type | None = None
        self.lifecycle_status = WorkflowLifecycleStatus(state="running")
        self.queries: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []
        self._client = client
        # None => inherit the client's bound identity (the happy path); set
        # explicitly in a test to simulate a foreign execution / id collision.
        self.describe_workflow_type: str | None = None
        self.describe_memo: dict[str, Any] | None = None
        self.describe_run_id: str | None = None

    async def describe(self) -> _FakeWorkflowDescription:
        workflow_type = (
            self.describe_workflow_type
            if self.describe_workflow_type is not None
            else getattr(self._client, "bound_workflow_type", None)
        )
        memo = (
            self.describe_memo
            if self.describe_memo is not None
            else dict(getattr(self._client, "bound_memo", {}))
        )
        run_id = self.describe_run_id or self.run_id or _fake_run_id(self.workflow_id)
        return _FakeWorkflowDescription(workflow_type=workflow_type, memo=memo, run_id=run_id)

    async def result(self):
        # Mirrors the fake execute_workflow payload for the traced start path.
        assert self.result_payload_type is not None, "fake handle needs result_type"
        return self.result_payload_type(value="done")

    async def query(self, name: str, **kwargs: Any) -> WorkflowLifecycleStatus:
        self.queries.append({"name": name, "kwargs": kwargs})
        return self.lifecycle_status

    async def signal(self, name: str, *args: Any) -> None:
        self.signals.append({"name": name, "args": args})


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.trace_io_calls: list[dict[str, Any]] = []
        self.observations: list[_FakeLangfuseObservation] = []
        self.flush_count = 0

    def start_as_current_observation(self, **kwargs):
        self.start_calls.append(kwargs)
        observation = _FakeLangfuseObservation()
        self.observations.append(observation)
        return _FakeLangfuseContext(observation)

    def set_current_trace_io(self, **kwargs):
        self.trace_io_calls.append(kwargs)

    def flush(self) -> None:
        self.flush_count += 1


class _FakeLangfuseContext:
    def __init__(self, observation) -> None:
        self._observation = observation

    def __enter__(self):
        return self._observation

    def __exit__(self, exc_type, exc, traceback):
        return False


async def _wait_for_status(workflow_instance: Any, state: str) -> None:
    for _ in range(100):
        if workflow_instance.typeflux_lifecycle_status().state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow did not reach lifecycle state {state!r}")


class _FakeLangfuseObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _FakeObservabilityBackend:
    def __init__(self, *, client: Any, plugin: Any) -> None:
        self.writer = _FakeTraceWriter(client=client, plugin=plugin)
        self.reader = _FakeTraceReader()


class _FakeTraceWriter:
    enabled = True

    def __init__(self, *, client: Any, plugin: Any) -> None:
        self.client = client
        self._plugin = plugin
        self.metadata_contributors = ()

    def configure_temporal_plugin(self):
        return self._plugin

    def create_activity_observer(self, *, metadata_contributors=()):
        self.metadata_contributors = tuple(metadata_contributors)
        return NoOpObserver()

    def observe_workflow_invocation(self, **kwargs: Any):
        return observe_workflow_invocation(client=self.client, **kwargs)

    def observe_lifecycle_operation(self, **kwargs: Any):
        operation_type = kwargs["operation_type"]
        operation_name = kwargs["operation_name"]
        label = "Query" if operation_type == "query" else "Signal"
        self.client.start_calls.append(
            {
                "name": f"TypefluxLifecycle{label}:{operation_name}",
                "as_type": "span",
                "metadata": kwargs.get("metadata"),
                "tags": kwargs.get("tags"),
            }
        )
        observation = _FakeLangfuseObservation()
        self.client.observations.append(observation)
        return _FakeLifecycleOperationContext(observation)

    def flush(self) -> None:
        flush = getattr(self.client, "flush", None)
        if flush is not None:
            flush()

    def shutdown(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


class _FakeTraceReader:
    pass


class _FakeLifecycleOperationContext:
    def __init__(self, observation: _FakeLangfuseObservation) -> None:
        self._observation = observation

    def __enter__(self):
        return _FakeLifecycleOperationObservation(self._observation)

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeLifecycleOperationObservation:
    def __init__(self, observation: _FakeLangfuseObservation) -> None:
        self._observation = observation

    def update_output(self, output_value: Any) -> None:
        self._observation.update(output=output_value)

    def update_error(self, error: BaseException) -> None:
        self._observation.update(level="ERROR", status_message=str(error))

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        self._observation.update(metadata=metadata)


class _RecordingPromptRegistry:
    def __init__(self) -> None:
        self.refs: list[PromptRef] = []

    def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        self.refs.append(ref)
        return ResolvedPrompt(
            ref=ref,
            messages=(ChatMessage(role="user", content="Prompt"),),
            resolved_version=ref.version,
        )


class _RecordingTraceWriter:
    enabled = True

    def __init__(self) -> None:
        self.flush_count = 0
        self.shutdown_count = 0

    def flush(self) -> None:
        self.flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1


class _RunRuntime:
    langfuse_client = None

    def __init__(self, *, writer: _RecordingTraceWriter, error: Exception | None = None) -> None:
        self.worker = _RunWorker(error=error)
        self.observability = _RunObservability(writer=writer)


class _RunWorker:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    async def run(self) -> None:
        if self._error is not None:
            raise self._error


class _RunObservability:
    def __init__(self, *, writer: _RecordingTraceWriter) -> None:
        self.writer = writer


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


async def _build_langfuse_yaml_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overrides: dict[str, Any] | None = None,
    policy_guard: RuntimePolicyGuard | None = None,
) -> tuple[TypefluxYamlRuntime, _FakeTemporalClient, _FakeLangfuseClient]:
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    fake_client = _FakeTemporalClient()
    fake_langfuse = _FakeLangfuseClient()

    class FakeLangfuseRegistry:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    async def fake_connect(spec, *, plugin):
        assert plugin == "plugin"
        return fake_client

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeLangfuseRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime.LangfuseObservabilityBackend.from_env",
        lambda **kwargs: _FakeObservabilityBackend(client=fake_langfuse, plugin="plugin"),
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime="""
            temporal:
              address: localhost:7233
            registry:
              type: langfuse
            provider:
              type: openai
              model: test-model
            observability:
              type: langfuse
            """,
            lifecycle="""
            enabled: true
            review:
              after_step: first
              user_decisions:
                send_email:
                  route: second
            """,
        ),
        overrides=overrides,
    )
    runtime = await build_runtime(spec, policy_guard=policy_guard)
    # Every handle the fake client vends now reports this runtime's workflow
    # type + project from describe(), so lifecycle ops pass the binding (#320).
    fake_client.bind_to(runtime.workflow_class)
    return runtime, fake_client, fake_langfuse


def _write_demo_project(tmp_path: Path, *, export_name: str | None) -> None:
    for name in tuple(sys.modules):
        if name == "yaml_demo_project" or name.startswith("yaml_demo_project."):
            del sys.modules[name]
    package = tmp_path / "yaml_demo_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        dedent(
            """
            from pydantic import BaseModel

            class InputModel(BaseModel):
                value: str

            class MiddleModel(BaseModel):
                value: str

            class OutputModel(BaseModel):
                value: str

            class PlainModel:
                pass
            """
        ),
        encoding="utf-8",
    )
    export_line = "" if export_name is None else f"{export_name} = (first, second)"
    (package / "activities.py").write_text(
        dedent(
            f"""
            from typeflux.core import AIActivity, PromptRef
            from yaml_demo_project.schemas import InputModel, MiddleModel, OutputModel

            first = AIActivity(
                name="first",
                input_type=InputModel,
                output_type=MiddleModel,
                prompt_ref=PromptRef("first"),
            )
            second = AIActivity(
                name="second",
                input_type=MiddleModel,
                output_type=OutputModel,
                prompt_ref=PromptRef("second"),
            )
            {export_line}
            """
        ),
        encoding="utf-8",
    )


def _write_map_demo_project(tmp_path: Path) -> None:
    for name in tuple(sys.modules):
        if name == "yaml_map_project" or name.startswith("yaml_map_project."):
            del sys.modules[name]
    package = tmp_path / "yaml_map_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        dedent(
            """
            from pydantic import BaseModel

            class PageInput(BaseModel):
                value: str

            class InputModel(BaseModel):
                value: str
                pages: list[PageInput]

            class PageList(BaseModel):
                pages: list[PageInput]

            class PageReview(BaseModel):
                value: str

            class MiddleModel(BaseModel):
                value: str

            class PageReviewBatch(BaseModel):
                reviews: list[PageReview]

            class WrongReviewBatch(BaseModel):
                reviews: list[MiddleModel]

            class FinalReview(BaseModel):
                value: str
            """
        ),
        encoding="utf-8",
    )
    (package / "activities.py").write_text(
        dedent(
            """
            from typeflux.core import AIActivity, PromptRef
            from yaml_map_project.schemas import (
                FinalReview,
                PageInput,
                PageList,
                PageReview,
                PageReviewBatch,
                InputModel,
            )

            select_pages = AIActivity(
                name="select_pages",
                input_type=InputModel,
                output_type=PageList,
                prompt_ref=PromptRef("select_pages"),
            )
            review_page = AIActivity(
                name="review_page",
                input_type=PageInput,
                output_type=PageReview,
                prompt_ref=PromptRef("review_page"),
            )
            consolidate_reviews = AIActivity(
                name="consolidate_reviews",
                input_type=PageReviewBatch,
                output_type=FinalReview,
                prompt_ref=PromptRef("consolidate_reviews"),
            )
            ALL_ACTIVITIES = (select_pages, review_page, consolidate_reviews)
            """
        ),
        encoding="utf-8",
    )


def _write_map_demo_yaml(
    tmp_path: Path,
    *,
    workflow_output: str = "schemas:FinalReview",
    lifecycle: str | None = None,
    steps: str = """
    - id: review_pages
      map:
        activity: review_page
        over: input.pages
        concurrency: 2
        collect:
          output: schemas:PageReviewBatch
          field: reviews
    - id: consolidate
      activity: consolidate_reviews
    """,
) -> Path:
    lines = [
        "project: yaml_map_project",
        "name: map_demo_yaml",
        "task_queue: demo-task-queue",
        "runtime:",
        "  temporal:",
        "    address: localhost:7233",
        "  registry:",
        "    type: inline",
        "    prompts:",
        "      review_page: review {{value}}",
        "      consolidate_reviews: consolidate",
        "  provider:",
        "    type: fake",
        "activities:",
        "  modules:",
        "    - activities",
        "workflow:",
        "  name: DemoMapWorkflow",
        "  input: schemas:InputModel",
        f"  output: {workflow_output}",
    ]
    if lifecycle is not None:
        lines.extend(["  lifecycle:", indent(dedent(lifecycle).strip(), "    ")])
    lines.extend(["  steps:", indent(dedent(steps).strip(), "    ")])
    return _write_yaml(tmp_path, "\n".join(lines))


def _write_recording_provider(tmp_path: Path) -> None:
    package = tmp_path / "yaml_demo_project"
    (package / "providers.py").write_text(
        dedent(
            """
            from yaml_demo_project.schemas import MiddleModel, OutputModel
            from typeflux.providers.errors import ProviderRateLimitError

            class RecordingProvider:
                default_model = "recording-provider"

                def __init__(self) -> None:
                    self.calls = []

                def structured_call(
                    self,
                    *,
                    messages,
                    output_schema,
                    model=None,
                    temperature=None,
                    metadata=None,
                ):
                    rendered_messages = [message.content for message in messages]
                    self.calls.append(
                        {
                            "messages": rendered_messages,
                            "output_schema": output_schema.__name__,
                        }
                    )
                    if len(rendered_messages) != 1:
                        raise AssertionError(f"unexpected messages: {rendered_messages!r}")
                    content = rendered_messages[0]
                    if output_schema is MiddleModel and content.startswith("first "):
                        value = content.removeprefix("first ")
                        return MiddleModel(value=f"{value}:middle")
                    if output_schema is OutputModel and content.startswith("second "):
                        value = content.removeprefix("second ")
                        return OutputModel(value=f"{value}:output")
                    raise AssertionError(
                        f"unexpected schema or prompt: {output_schema.__name__} {content!r}"
                    )

            class RetryingProvider:
                default_model = "retrying-provider"

                def __init__(self) -> None:
                    self.calls = []

                def structured_call(
                    self,
                    *,
                    messages,
                    output_schema,
                    model=None,
                    temperature=None,
                    metadata=None,
                ):
                    rendered_messages = [message.content for message in messages]
                    self.calls.append(
                        {
                            "messages": rendered_messages,
                            "output_schema": output_schema.__name__,
                            "metadata": metadata,
                        }
                    )
                    if len(self.calls) == 1:
                        raise ProviderRateLimitError(
                            "rate limited",
                            provider="fake",
                            status_code=429,
                        )
                    content = rendered_messages[0]
                    if output_schema is MiddleModel and content.startswith("first "):
                        value = content.removeprefix("first ")
                        return MiddleModel(value=f"{value}:middle")
                    if output_schema is OutputModel and content.startswith("second "):
                        value = content.removeprefix("second ")
                        return OutputModel(value=f"{value}:output")
                    raise AssertionError(
                        f"unexpected schema or prompt: {output_schema.__name__} {content!r}"
                    )
            """
        ),
        encoding="utf-8",
    )


class _FakeLifecycleAuditConverter:
    payload_converter = None

    def __init__(self) -> None:
        self.payload_converter = self

    def from_payloads(self, payloads):
        return list(payloads)


def _history_event(event_id: int, event_type: str, **attrs: Any) -> SimpleNamespace:
    attr_name = event_type.lower() + "_event_attributes"
    return SimpleNamespace(
        event_id=event_id,
        event_type=SimpleNamespace(name=f"EVENT_TYPE_{event_type}"),
        event_time=None,
        **{attr_name: _history_attrs(event_type, attrs)},
    )


def _history_attrs(event_type: str, attrs: dict[str, Any]) -> SimpleNamespace:
    values = dict(attrs)
    if "activity_type" in values:
        values["activity_type"] = SimpleNamespace(name=values["activity_type"])
    if "payloads" in values:
        values["input"] = SimpleNamespace(payloads=values.pop("payloads"))
    return SimpleNamespace(**values)


def _write_demo_yaml(
    tmp_path: Path,
    *,
    yaml_name: str = "demo_yaml",
    risk_tier: str | None = None,
    workflow_name: str = "DemoYamlWorkflow",
    task_queue: str = "demo-task-queue",
    runtime: str = """
    temporal:
      address: localhost:7233
    registry:
      type: inline
      prompts:
        first: first {{value}}
        second: second {{value}}
    provider:
      type: fake
    """,
    modules: str | None = """
    - activities
    """,
    definitions: str | None = None,
    workflow_input: str = "schemas:InputModel",
    workflow_output: str = "schemas:OutputModel",
    workflow_version: str | None = None,
    subjects: str | None = None,
    lifecycle: str | None = None,
    steps: str = """
    - id: first
      activity: first
    - id: second
      activity: second
    """,
) -> Path:
    lines = [
        "project: yaml_demo_project",
        f"name: {yaml_name}",
        f"task_queue: {task_queue}",
        "runtime:",
        indent(dedent(runtime).strip(), "  "),
        "activities:",
    ]
    if modules is not None:
        lines.extend(
            [
                "  modules:",
                indent(dedent(modules).strip(), "    "),
            ]
        )
    if definitions is not None:
        lines.extend(
            [
                "  definitions:",
                indent(dedent(definitions).strip(), "    "),
            ]
        )
    lines.extend(
        [
            "workflow:",
            f"  name: {workflow_name}",
            f"  input: {workflow_input}",
            f"  output: {workflow_output}",
        ]
    )
    if workflow_version is not None:
        lines.insert(lines.index(f"  name: {workflow_name}") + 1, f"  version: {workflow_version}")
    if risk_tier is not None:
        lines.insert(lines.index(f"  name: {workflow_name}") + 1, f"  risk_tier: {risk_tier}")
    if subjects is not None:
        lines.extend(["  subjects:", indent(dedent(subjects).strip(), "    ")])
    if lifecycle is not None:
        lines.extend(["  lifecycle:", indent(dedent(lifecycle).strip(), "    ")])
    lines.extend(["  steps:", indent(dedent(steps).strip(), "    ")])
    content = "\n".join(lines)
    return _write_yaml(
        tmp_path,
        content,
    )


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "typeflux.yaml"
    path.write_text(dedent(content).strip() + "\n", encoding="utf-8")
    return path


def test_prepare_runtime_build_warns_on_unenforced_elevated_risk_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #788 (audit B1): the single-spec path warns — never fails — when an elevated
    # declared tier has no policy guard to enforce it.
    import logging

    from typeflux.yaml.runtime import prepare_runtime_build

    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path, risk_tier="human_gated"))
    with caplog.at_level(logging.WARNING, logger="typeflux.yaml.runtime"):
        prepare_runtime_build(spec, policy_guard=None)
    assert any("UNENFORCED" in record.message for record in caplog.records)

    caplog.clear()
    safe_spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    with caplog.at_level(logging.WARNING, logger="typeflux.yaml.runtime"):
        prepare_runtime_build(safe_spec, policy_guard=None)
    assert not any("UNENFORCED" in record.message for record in caplog.records)
