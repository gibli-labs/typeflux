from __future__ import annotations

from pathlib import Path

import pytest

from typeflux.yaml import load_yaml_spec
from typeflux.yaml.imports import collect_activities

_RUNTIME_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "LANGFUSE_PROMPT_LABEL",
    "TEMPORAL_ADDRESS",
    "TEMPORAL_API_KEY",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_TASK_QUEUE",
    "TEMPORAL_TLS",
    "TYPEFLUX_ANTHROPIC_MODEL",
)


@pytest.mark.parametrize(
    ("relative_path", "expected_name", "expected_task_queue"),
    (
        (
            "examples/contract_risk_review/typeflux.anthropic.yaml",
            "contract_risk_review_anthropic",
            "contract-risk-review-anthropic-typeflux",
        ),
        (
            "examples/support_triage_langfuse/typeflux.anthropic.yaml",
            "support_triage_langfuse_anthropic",
            "support-triage-anthropic-typeflux",
        ),
    ),
)
def test_anthropic_example_yamls_are_isolated_from_shell_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative_path: str,
    expected_name: str,
    expected_task_queue: str,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    for name in _RUNTIME_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    spec = load_yaml_spec(Path(relative_path))

    assert spec.name == expected_name
    assert spec.task_queue == expected_task_queue
    assert spec.runtime.temporal.address == "localhost:7233"
    assert spec.runtime.temporal.namespace == "default"
    assert spec.runtime.temporal.tls is False
    assert spec.runtime.temporal.api_key is not None
    assert spec.runtime.temporal.api_key.value_from.env == "TEMPORAL_API_KEY"
    assert spec.runtime.temporal.api_key.value_from.required is False
    assert spec.runtime.provider.type == "anthropic"
    assert spec.runtime.provider.model == "claude-sonnet-4-6"
    assert spec.runtime.provider.api_key is not None
    assert spec.runtime.provider.api_key.value_from.env == "ANTHROPIC_API_KEY"


@pytest.mark.parametrize(
    "example_directory",
    ("examples/contract_risk_review", "examples/support_triage_langfuse"),
)
def test_anthropic_variant_is_isolated_from_base_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    example_directory: str,
) -> None:
    # Provider variants must not share a task queue or workflow type with the
    # base spec; otherwise a fake/OpenAI worker can pick up executions meant
    # for the Anthropic worker (and vice versa).
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    for name in _RUNTIME_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TYPEFLUX_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    base = load_yaml_spec(Path(example_directory) / "typeflux.yaml")
    variant = load_yaml_spec(Path(example_directory) / "typeflux.anthropic.yaml")

    assert base.task_queue != variant.task_queue
    assert base.workflow.name != variant.workflow.name
    assert base.name != variant.name


def test_contract_anthropic_yaml_keeps_retry_bounds_and_large_token_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    for name in _RUNTIME_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    spec = load_yaml_spec(Path("examples/contract_risk_review/typeflux.anthropic.yaml"))
    retry = spec.runtime.provider_retry
    activities = collect_activities(spec)

    assert retry is not None
    assert retry.max_attempts == 2
    assert retry.initial_backoff_seconds == 1.0
    assert retry.max_backoff_seconds == 4.0
    assert retry.backoff_multiplier == 2.0
    assert spec.runtime.provider.provider_params().to_dict()["max_tokens"] == 16000
    assert activities["analyze_contract"].provider_params.to_dict()["max_tokens"] == 16000
