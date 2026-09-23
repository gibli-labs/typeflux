from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from re import Match, Pattern
from typing import Any, Protocol

from pydantic import BaseModel


class Redactor(Protocol):
    def redact(self, data: Any, **kwargs: Any) -> Any: ...


class NoOpRedactor:
    def redact(self, data: Any, **kwargs: Any) -> Any:
        return data


class _RedactedMetadata(dict[str, Any]):
    pass


def _mark_redacted_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if isinstance(metadata, _RedactedMetadata):
        return metadata
    return _RedactedMetadata(metadata)


def _is_redacted_metadata(data: Any) -> bool:
    return isinstance(data, _RedactedMetadata)


@dataclass(frozen=True)
class RegexRedactionRule:
    name: str
    pattern: Pattern[str]
    replacement: str
    validator: Callable[[str], bool] | None = None


DEFAULT_EXCLUDED_PATHS = (
    "temporal.workflow_id",
    "temporal.run_id",
    "temporal.activity_id",
    "temporal.workflow_type",
    "temporal.activity_type",
    "typeflux.workflow.workflow_id",
    "typeflux.workflow.workflow_name",
    "typeflux.runtime_placement.platform",
    "typeflux.runtime_placement.kubernetes.namespace",
    "typeflux.runtime_placement.kubernetes.pod_name",
    "typeflux.runtime_placement.kubernetes.pod_uid",
    "typeflux.runtime_placement.kubernetes.node_name",
    "typeflux.runtime_placement.kubernetes.service_account",
    "typeflux.runtime_placement.kubernetes.deployment_name",
    "typeflux.runtime_placement.kubernetes.worker_name",
    "typeflux.runtime_placement.container_image",
    "typeflux.execution_manifest.workflow_id",
    "typeflux.execution_manifest.temporal_run_id",
    "typeflux.execution_manifest.workflow_contract_hash",
    "typeflux.execution_manifest.manifest_hash",
    "typeflux.execution_manifest.activities.manifest_hash",
    "typeflux.execution_manifest.activities.activity_manifest_hash",
    "typeflux.execution_manifest.activities.input_schema.hash",
    "typeflux.execution_manifest.activities.output_schema.hash",
    "typeflux.execution_manifest.activities.prompt_messages_hash",
    "typeflux.execution_manifest.activities.rendered_messages_hash",
    "typeflux.execution_manifest.activities.provider_params.*",
    "typeflux.execution_manifest.activities.start_to_close_timeout_seconds",
    "typeflux.execution_manifest.activities.definition_source.*",
    "typeflux.execution_manifest.activities.artifact_inputs.*",
    "typeflux.execution_manifest.activities.artifacts.*",
    "typeflux.execution_manifest.map_steps.*",
    "typeflux.execution_manifest.code_provenance.git_sha",
    "typeflux.execution_manifest.code_provenance.git_ref",
    "typeflux.execution_manifest.code_provenance.repo_url",
    "typeflux.yaml.map_steps.*",
    "typeflux.map.*",
    # Keep in sync with _SAFE_LIFECYCLE_FIELDS in typeflux.metadata;
    # arbitrary lifecycle values are not blanket-preserved.
    "typeflux.lifecycle.state",
    "typeflux.lifecycle.current_step",
    "typeflux.lifecycle.completed_units",
    "typeflux.lifecycle.total_units",
    "typeflux.lifecycle.cancellation_requested",
    "typeflux.lifecycle.waiting_checkpoint",
    "typeflux.lifecycle.terminal_status",
    "typeflux.lifecycle.status_event_limit",
    "typeflux.lifecycle.review_after_step",
    "typeflux.provider_controls.*",
    # Moderation verdict is a classification (decision/categories/score/moderator),
    # never raw output, so it is preserved as audit evidence (#158). Recorded as
    # its own top-level key (not nested under typeflux) because a backend
    # update_metadata replaces a top-level key rather than deep-merging it.
    "typeflux_moderation.*",
    "typeflux.activity.manifest_hash",
    "typeflux.activity.input_schema_hash",
    "typeflux.activity.output_schema_hash",
    "typeflux.activity.activity_manifest_hash",
    "typeflux.activity.input_schema.hash",
    "typeflux.activity.output_schema.hash",
    "typeflux.activity.prompt_messages_hash",
    "typeflux.activity.rendered_messages_hash",
    "typeflux.activity.provider_params.*",
    "typeflux.activity.definition_source.*",
    "typeflux.activity.artifact_inputs.*",
    "typeflux.activity.artifacts.*",
    "typeflux.join.*",
    "typeflux.manifest_hash",
    "typeflux.input_schema_hash",
    "typeflux.output_schema_hash",
    "typeflux.activity_execution_manifest_hash",
    "typeflux.activity_execution_manifest.manifest_hash",
    "typeflux.activity_execution_manifest.activity_manifest_hash",
    "typeflux.activity_execution_manifest.input_schema.hash",
    "typeflux.activity_execution_manifest.output_schema.hash",
    "typeflux.activity_execution_manifest.prompt_messages_hash",
    "typeflux.activity_execution_manifest.rendered_messages_hash",
    "typeflux.activity_execution_manifest.provider_params.*",
    "typeflux.activity_execution_manifest.start_to_close_timeout_seconds",
    "typeflux.activity_execution_manifest.definition_source.*",
    "typeflux.activity_execution_manifest.artifact_inputs.*",
    "typeflux.activity_execution_manifest.artifacts.*",
    "typeflux.registry.langfuse.prompt_config.typeflux.contracts.input_schema_hash",
    "typeflux.registry.langfuse.prompt_config.typeflux.contracts.output_schema_hash",
    "langfuse.prompt_config.typeflux.contracts.input_schema_hash",
    "langfuse.prompt_config.typeflux.contracts.output_schema_hash",
)


class RegexPIIRedactor:
    def __init__(
        self,
        rules: tuple[RegexRedactionRule, ...],
        *,
        exclude_paths: Sequence[str] = (),
    ) -> None:
        self.rules = rules
        self.exclude_paths = tuple(exclude_paths)

    @classmethod
    def default(
        cls,
        *,
        emails: bool = True,
        phones: bool = True,
        ssn: bool = True,
        credit_cards: bool = True,
        exclude_paths: Sequence[str] = (),
        preserve_typeflux_metadata: bool = True,
        custom_rules: Sequence[RegexRedactionRule] = (),
    ) -> RegexPIIRedactor:
        rules: list[RegexRedactionRule] = []
        if emails:
            rules.append(
                RegexRedactionRule(
                    name="email",
                    pattern=re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
                    replacement="[REDACTED_EMAIL]",
                )
            )
        if ssn:
            rules.append(
                RegexRedactionRule(
                    name="ssn",
                    pattern=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
                    replacement="[REDACTED_SSN]",
                )
            )
        if credit_cards:
            rules.append(
                RegexRedactionRule(
                    name="credit_card",
                    pattern=re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"),
                    replacement="[REDACTED_CARD]",
                    validator=_is_luhn_valid_candidate,
                )
            )
        if phones:
            rules.append(
                RegexRedactionRule(
                    name="phone",
                    pattern=re.compile(
                        r"(?<!\d)(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?!\d)"
                    ),
                    replacement="[REDACTED_PHONE]",
                )
            )
        # Custom rules (#188 D188-4) append AFTER the built-in catalog: the built-ins
        # redact known PII first, then jurisdiction/domain rules run over the result.
        # DEFAULT_EXCLUDED_PATHS still wins — exclusion is checked per node in
        # `_redact_value` BEFORE any rule (built-in or custom) touches a string.
        rules.extend(custom_rules)
        resolved_exclude_paths = (
            (*DEFAULT_EXCLUDED_PATHS, *exclude_paths)
            if preserve_typeflux_metadata
            else tuple(exclude_paths)
        )
        return cls(tuple(rules), exclude_paths=resolved_exclude_paths)

    def redact(self, data: Any, **kwargs: Any) -> Any:
        return self._redact_value(data, path=())

    def _redact_value(self, value: Any, *, path: tuple[str, ...]) -> Any:
        if self._is_excluded(path):
            return value
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, BaseModel):
            return self._redact_value(value.model_dump(mode="python"), path=path)
        if isinstance(value, dict):
            return {
                key: self._redact_value(item, path=(*path, str(key))) for key, item in value.items()
            }
        # Sequence item paths intentionally remain index-less for compatibility with
        # existing wildcard excludes such as typeflux.execution_manifest.activities.*.
        if isinstance(value, list):
            return [self._redact_value(item, path=path) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, path=path) for item in value)
        if isinstance(value, set):
            return {self._redact_value(item, path=path) for item in value}
        if isinstance(value, frozenset):
            return frozenset(self._redact_value(item, path=path) for item in value)
        return value

    def _redact_string(self, value: str) -> str:
        redacted = value
        for rule in self.rules:
            redacted = rule.pattern.sub(_replacement(rule), redacted)
        return redacted

    def _is_excluded(self, path: tuple[str, ...]) -> bool:
        if not path:
            return False
        dotted_path = ".".join(path)
        return any(fnmatchcase(dotted_path, pattern) for pattern in self.exclude_paths)


def _replacement(rule: RegexRedactionRule) -> Callable[[Match[str]], str]:
    validator = rule.validator
    if validator is None:
        # LITERAL replacement. Return a function so `re.sub` treats `rule.replacement` as
        # a literal string and never interprets `\1` / `\g<name>` / backslash escapes in
        # it. A plain string repl (the previous behavior) would, for a YAML custom rule
        # like `replacement: '[CASE \1]'`, re-inject the CAPTURED PII into the "redacted"
        # output, and a stray backslash (e.g. `\C`) would raise re.error AT TRACE EMISSION.
        # Built-in rules contain no backslashes, so their behavior is unchanged. We do NOT
        # re.escape the replacement — that would emit doubled backslashes into the output.
        # This also matches the TS edition, whose `applyRules` returns the replacement from
        # a replacer FUNCTION (never `$`-interpreted), so identical YAML redacts identically.
        return lambda _match: rule.replacement

    def replace(match: Match[str]) -> str:
        candidate = match.group(0)
        return rule.replacement if validator(candidate) else candidate

    return replace


def _is_luhn_valid_candidate(candidate: str) -> bool:
    digits = candidate.replace(" ", "").replace("-", "")
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    return _luhn_checksum(digits) == 0


def _luhn_checksum(digits: str) -> int:
    total = 0
    reverse_digits = digits[::-1]
    for index, char in enumerate(reverse_digits):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10


__all__ = [
    "DEFAULT_EXCLUDED_PATHS",
    "NoOpRedactor",
    "Redactor",
    "RegexPIIRedactor",
    "RegexRedactionRule",
]
