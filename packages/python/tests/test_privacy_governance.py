"""Privacy governance (#188 slice 2) — Python edition.

Covers the two governance controls layered on slice 1's payload codec:

* ``require_payload_codec`` — a policy (or a ``human_gated``/``prohibited`` risk tier)
  rejects a workflow whose ``runtime.temporal.payload_codec`` is absent (D188-2),
  fail-closed, OR-merging across composed policies, surfaced in the bundle.
* custom redaction rules — ``observability.redaction.custom_rules`` appends to the
  built-in catalog (D188-4); invalid patterns fail load; excluded paths still win; a
  policy can require named rules exist (union-merge).

Unit-level checks reuse the ``SimpleNamespace`` resolved-spec pattern from
``test_risk_tiers.py``; the redaction egress is exercised through a real loaded spec.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from typeflux.observability.redaction import (
    DEFAULT_EXCLUDED_PATHS,
    RegexPIIRedactor,
    RegexRedactionRule,
)
from typeflux.project.policy import (
    TypefluxProjectPolicySpec,
    _merge_policy_value,
)
from typeflux.project.policy_enforcement import (
    _validate_observability,
    _validate_temporal,
    evaluate_risk_tier,
)
from typeflux.yaml import load_yaml_spec
from typeflux.yaml.runtime import _build_redactor
from typeflux.yaml.spec import RedactionRuleSpec, RedactionSpec

# ── unit resolved-spec factory (mirrors test_risk_tiers.py) ─────────────────────


def _resolved(
    *,
    payload_codec: Any | None = None,
    redaction_enabled: bool = True,
    preserve_metadata: bool = True,
    custom_rule_names: tuple[str, ...] = (),
    backend: str | None = "langfuse",
) -> Any:
    redaction = SimpleNamespace(
        enabled=redaction_enabled,
        preserve_typeflux_metadata=preserve_metadata,
        custom_rules=[SimpleNamespace(name=name) for name in custom_rule_names],
    )
    spec = SimpleNamespace(
        workflow=SimpleNamespace(risk_tier=None),
        activities=SimpleNamespace(definitions=[]),
        runtime=SimpleNamespace(
            observability=SimpleNamespace(type=backend, redaction=redaction),
            temporal=SimpleNamespace(
                address="localhost:7233",
                namespace="default",
                tls=False,
                api_key=None,
                payload_codec=payload_codec,
            ),
            provider=SimpleNamespace(type="openai", model="gpt-4o-mini"),
        ),
    )
    return SimpleNamespace(spec=spec, workflow_id="w")


_CODEC = SimpleNamespace(type="aes", current="k1", keys=[SimpleNamespace(id="k1")])


# ── custom redaction rules: spec parse + validation ─────────────────────────────


def test_redaction_rule_spec_accepts_valid_pattern() -> None:
    rule = RedactionRuleSpec(name="case_id", pattern=r"CASE-\d{6}", replacement="[REDACTED_CASE]")
    assert rule.name == "case_id"


def test_redaction_rule_spec_rejects_invalid_regex() -> None:
    # Fail-closed: an unbalanced group is a load-time error, never a silent no-op.
    with pytest.raises(ValidationError, match="not a valid regex"):
        RedactionRuleSpec(name="bad", pattern=r"CASE-(\d{6}", replacement="[X]")


def test_redaction_rule_spec_rejects_empty_fields() -> None:
    with pytest.raises(ValidationError):
        RedactionRuleSpec(name="", pattern=r"x", replacement="[X]")
    with pytest.raises(ValidationError):
        RedactionRuleSpec(name="n", pattern="", replacement="[X]")


def test_redaction_rule_spec_rejects_untrimmed_name() -> None:
    # A trailing space would load here but fail admission (policy membership is trim-exact),
    # so reject it at the spec boundary with a pointer error.
    with pytest.raises(ValidationError, match="name must be non-empty and trimmed"):
        RedactionRuleSpec(name="case_reference ", pattern="x", replacement="[X]")


def test_redaction_rule_spec_allows_untrimmed_replacement() -> None:
    # Only the NAME is trim-enforced; leading/trailing spaces are legitimate in a replacement.
    rule = RedactionRuleSpec(name="n", pattern="x", replacement="  [X]  ")
    assert rule.replacement == "  [X]  "


def test_redaction_spec_rejects_duplicate_custom_rule_names() -> None:
    with pytest.raises(ValidationError, match="names must be unique"):
        RedactionSpec(
            custom_rules=[
                {"name": "dup", "pattern": "a", "replacement": "[A]"},
                {"name": "dup", "pattern": "b", "replacement": "[B]"},
            ]
        )


# ── custom redaction rules: redaction behaviour ─────────────────────────────────


def test_custom_rule_redacts_after_built_in_catalog() -> None:
    rule = RegexRedactionRule(
        name="case_id",
        pattern=re.compile(r"CASE-\d{6}"),
        replacement="[REDACTED_CASE]",
    )
    redactor = RegexPIIRedactor.default(custom_rules=(rule,))

    result = redactor.redact("email jane@example.com case CASE-123456")

    # Built-in email rule AND the custom rule both fire.
    assert result == "email [REDACTED_EMAIL] case [REDACTED_CASE]"


def test_custom_rule_respects_default_excluded_paths() -> None:
    # DEFAULT_EXCLUDED_PATHS (typeflux.* governance metadata) wins over a custom rule.
    rule = RegexRedactionRule(name="all", pattern=re.compile(r".+"), replacement="[X]")
    redactor = RegexPIIRedactor.default(custom_rules=(rule,))
    payload = {
        "typeflux": {"workflow": {"workflow_id": "wf-123"}},
        "user_note": "CASE-999999",
    }

    result = redactor.redact(payload)

    assert result["typeflux"]["workflow"]["workflow_id"] == "wf-123"
    assert result["user_note"] == "[X]"


def test_build_redactor_threads_spec_custom_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "privacy_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        "from pydantic import BaseModel\n\n\nclass In(BaseModel):\n    x: str\n\n\n"
        "class Out(BaseModel):\n    y: str\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    text = (
        "project: privacy_project\n"
        "name: demo\n"
        "task_queue: q\n"
        "runtime:\n"
        "  registry: { type: inline, prompts: { p: hi } }\n"
        "  provider: { type: fake }\n"
        "  observability:\n"
        "    type: langfuse\n"
        "    redaction:\n"
        "      custom_rules:\n"
        "        - { name: case_id, pattern: 'CASE-\\d{6}', replacement: '[REDACTED_CASE]' }\n"
        "activities:\n"
        "  definitions:\n"
        "    - { name: a, input: schemas:In, output: schemas:Out, prompt: p }\n"
        "workflow:\n"
        "  name: DemoWorkflow\n"
        "  input: schemas:In\n"
        "  output: schemas:Out\n"
        "  steps:\n"
        "    - { id: s1, activity: a }\n"
    )
    path = tmp_path / "typeflux.yaml"
    path.write_text(text, encoding="utf-8")
    spec = load_yaml_spec(path, load_dotenv=False)

    redactor = _build_redactor(spec)
    result = redactor.redact({"note": "ref CASE-777888 and jane@example.com"})

    assert result["note"] == "ref [REDACTED_CASE] and [REDACTED_EMAIL]"
    # Governance metadata still protected end-to-end.
    protected = redactor.redact({"typeflux": {"workflow": {"workflow_id": "CASE-111222"}}})
    assert protected["typeflux"]["workflow"]["workflow_id"] == "CASE-111222"


# ── policy: require_custom_rules ────────────────────────────────────────────────


def _observability_payload(**redaction: Any) -> dict[str, Any]:
    return {"observability": {"redaction": redaction}}


def test_require_custom_rules_passes_when_named_rules_present() -> None:
    resolved = _resolved(custom_rule_names=("case_id", "iban"))
    payload = _observability_payload(require_custom_rules=["case_id"])
    assert _validate_observability(resolved, payload).status == "passed"


def test_require_custom_rules_fails_listing_missing_names() -> None:
    resolved = _resolved(custom_rule_names=("case_id",))
    payload = _observability_payload(require_custom_rules=["case_id", "iban", "nino"])
    check = _validate_observability(resolved, payload)
    assert check.status == "failed"
    assert "iban" in check.message and "nino" in check.message
    assert "case_id" not in check.message


def test_require_custom_rules_union_merges_across_policies() -> None:
    # A non-allowlist list unions on composition (dedupe concat).
    path = "p.observability.redaction.require_custom_rules"
    merged = _merge_policy_value(["case_id"], ["iban", "case_id"], path=path)
    assert sorted(merged) == ["case_id", "iban"]


def test_require_custom_rules_rejects_disabled_redaction() -> None:
    # Requiring named rules means requiring they RUN: enabled:false no-ops the whole redactor,
    # so a required-but-disabled config must be REJECTED naming both knobs (fail-closed).
    resolved = _resolved(redaction_enabled=False, custom_rule_names=("case_id",))
    payload = _observability_payload(require_custom_rules=["case_id"])
    check = _validate_observability(resolved, payload)
    assert check.status == "failed"
    assert "enabled is false" in check.message
    assert "case_id" in check.message


def test_disabled_redaction_unaffected_without_require_custom_rules() -> None:
    # No require_custom_rules → the enabled:false implication does not fire (existing behavior).
    resolved = _resolved(redaction_enabled=False)
    payload = _observability_payload()
    assert _validate_observability(resolved, payload).status == "passed"


# ── policy: require_payload_codec ───────────────────────────────────────────────


def test_require_payload_codec_passes_when_codec_declared() -> None:
    resolved = _resolved(payload_codec=_CODEC)
    payload = {"runtime": {"temporal": {"require_payload_codec": True}}}
    assert _validate_temporal(resolved, payload).status == "passed"


def test_require_payload_codec_fails_when_codec_absent() -> None:
    resolved = _resolved(payload_codec=None)
    payload = {"runtime": {"temporal": {"require_payload_codec": True}}}
    check = _validate_temporal(resolved, payload)
    assert check.status == "failed"
    assert "payload_codec" in check.message


def test_require_payload_codec_or_merges_across_policies() -> None:
    path = "p.runtime.temporal.require_payload_codec"
    assert _merge_policy_value(False, True, path=path) is True
    assert _merge_policy_value(False, False, path=path) is False


# ── risk-tier implication + bundle surfacing ────────────────────────────────────


def _tier_resolved(*, risk_tier: str, payload_codec: Any | None) -> Any:
    resolved = _resolved(payload_codec=payload_codec)
    resolved.spec.workflow.risk_tier = risk_tier
    resolved.spec.workflow.lifecycle = None
    return resolved


def test_human_gated_tier_implies_require_payload_codec() -> None:
    # A policy makes human_gated imply encryption-at-rest (same mechanism as
    # require_redaction). A human_gated workflow without a codec fails.
    payload = TypefluxProjectPolicySpec(
        name="p",
        risk_tiers={"human_gated": {"require_payload_codec": True}},
    ).to_payload()

    without = _tier_resolved(risk_tier="human_gated", payload_codec=None)
    evaluation = evaluate_risk_tier(without.spec, payload)
    assert evaluation is not None
    codec_req = next(r for r in evaluation.requirements if r.name == "require_payload_codec")
    assert codec_req.satisfied is False

    with_codec = _tier_resolved(risk_tier="human_gated", payload_codec=_CODEC)
    evaluation2 = evaluate_risk_tier(with_codec.spec, payload)
    assert evaluation2 is not None
    assert all(r.satisfied for r in evaluation2.requirements)


def test_safe_tier_does_not_imply_payload_codec_when_only_human_gated_requires_it() -> None:
    payload = TypefluxProjectPolicySpec(
        name="p",
        risk_tiers={"human_gated": {"require_payload_codec": True}},
    ).to_payload()
    resolved = _tier_resolved(risk_tier="safe", payload_codec=None)
    evaluation = evaluate_risk_tier(resolved.spec, payload)
    assert evaluation is not None
    assert not any(r.name == "require_payload_codec" for r in evaluation.requirements)


def test_default_excluded_paths_is_non_empty_guardrail() -> None:
    # Sanity: the allowlist a custom rule cannot override is real.
    assert "temporal.workflow_id" in DEFAULT_EXCLUDED_PATHS
