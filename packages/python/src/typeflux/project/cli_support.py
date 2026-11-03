"""Shared input and policy-hash handling for foreground project commands."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from typeflux.project.policy_enforcement import ProjectPolicyEnforcementError


class PolicyHashUsageError(Exception):
    """A malformed or self-conflicting --expect-policy-hash INPUT (#818): a caller
    mistake, exit 2 — distinct from an actual hash-drift verdict, which stays a
    ProjectPolicyEnforcementError and exits 3."""


def expected_policy_hash(cli_value: str | None) -> str | None:
    cli_hash = normalize_policy_hash(cli_value)
    env_hash = normalize_policy_hash(os.getenv("TYPEFLUX_EXPECTED_POLICY_HASH"))
    if cli_hash is not None and env_hash is not None and cli_hash != env_hash:
        raise PolicyHashUsageError(
            "conflicting expected project policy hashes from --expect-policy-hash "
            "and TYPEFLUX_EXPECTED_POLICY_HASH"
        )
    return cli_hash or env_hash


def normalize_policy_hash(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise PolicyHashUsageError(
            "expected project policy hash must be a 64-character sha256 hex digest"
        )
    return normalized


def verify_expected_policy_hash(
    expected_policy_hash: str | None,
    *,
    policy_hash: str | None,
) -> None:
    if expected_policy_hash is None:
        return
    if policy_hash is None:
        raise ProjectPolicyEnforcementError(
            "expected project policy hash was provided, but no project policy was selected"
        )
    if policy_hash != expected_policy_hash:
        raise ProjectPolicyEnforcementError(
            "selected project policy hash does not match expected deployment policy hash "
            f"(expected={expected_policy_hash}, actual={policy_hash})"
        )


def read_input_json(path: str) -> Any:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)
