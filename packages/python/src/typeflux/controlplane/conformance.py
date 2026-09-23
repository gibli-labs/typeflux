"""Conformance of the emitted control-plane schema to the normative contract (#616).

The contract (``contracts/controlplane/openapi.v1.json``) is the interface;
the FastAPI-emitted schema must match it. This module produces the structured
divergence report CI fails with — a contract change the server does not
implement (or a server change the contract does not describe) is named
path-by-path, never a silent regen or an unreadable string diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typeflux.controlplane.api import openapi_spec, render_openapi_document

# Resolves inside the monorepo only (src layout: parents[5] = repo root). An
# installed package must pass an explicit contract path instead.
_DEFAULT_CONTRACT = (
    Path(__file__).resolve().parents[5] / "contracts" / "controlplane" / "openapi.v1.json"
)

_REPORT_LIMIT = 50


def _pointer(key: str) -> str:
    # JSON Pointer escaping (RFC 6901): API paths are keys, so "/" occurs.
    return key.replace("~", "~0").replace("/", "~1")


_JSON_TYPE_NAMES = {
    type(None): "null",
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    dict: "object",
    list: "array",
}


def _type_name(value: Any) -> str:
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def diff_documents(contract: Any, emitted: Any, *, _path: str = "") -> list[str]:
    """Structured divergence lines between two parsed JSON documents.

    The HTTP conformance runner (contracts/controlplane/conformance/runner.py)
    carries its own copy of this taxonomy on purpose: it must stay
    stdlib-only/standalone and cannot import this package. Taxonomy changes
    here should be mirrored there.

    Each line addresses one divergence by JSON-Pointer path and classifies it:
    ``missing from emitted schema`` (contract change the server does not
    implement), ``not in contract`` (server surface the contract does not
    describe), ``type differs``, or ``value differs``. Empty means conformant.
    """
    path = _path or "<root>"
    if type(contract) is not type(emitted):
        return [
            f"{path}: type differs (contract={_type_name(contract)}, emitted={_type_name(emitted)})"
        ]
    if isinstance(contract, dict):
        lines: list[str] = []
        for key in sorted(contract.keys() | emitted.keys()):
            child = f"{_path}/{_pointer(key)}"
            if key not in emitted:
                lines.append(f"{child}: missing from emitted schema")
            elif key not in contract:
                lines.append(f"{child}: not in contract")
            else:
                lines.extend(diff_documents(contract[key], emitted[key], _path=child))
        return lines
    if isinstance(contract, list):
        lines = []
        if len(contract) != len(emitted):
            lines.append(
                f"{path}: array length differs (contract={len(contract)}, emitted={len(emitted)})"
            )
        for index, (contract_item, emitted_item) in enumerate(zip(contract, emitted)):
            lines.extend(diff_documents(contract_item, emitted_item, _path=f"{_path}/{index}"))
        return lines
    if contract != emitted:
        return [f"{path}: value differs (contract={contract!r}, emitted={emitted!r})"]
    return []


@dataclass(frozen=True)
class ConformanceReport:
    """Outcome of one conformance check, renderable as the CI failure text."""

    contract_path: Path
    divergences: tuple[str, ...]

    @property
    def conformant(self) -> bool:
        return not self.divergences

    def render(self) -> str:
        if self.conformant:
            return f"conformant: emitted schema matches {self.contract_path}"
        lines = [
            f"emitted schema diverges from the contract {self.contract_path} "
            f"({len(self.divergences)} divergence(s)):"
        ]
        lines += [f"  {divergence}" for divergence in self.divergences[:_REPORT_LIMIT]]
        if len(self.divergences) > _REPORT_LIMIT:
            lines.append(f"  … and {len(self.divergences) - _REPORT_LIMIT} more")
        return "\n".join(lines)


def check_conformance(contract_path: Path | str | None = None) -> ConformanceReport:
    """Check the emitted schema against the normative contract.

    Two fail-closed layers, each with its own divergence prefix: the contract
    file must be in canonical rendering (``indent=2, sort_keys=True`` — the
    hand-governed document keeps deterministic formatting so contract diffs
    stay reviewable), and the parsed contract must match ``openapi_spec()``.
    """
    path = Path(contract_path) if contract_path is not None else _DEFAULT_CONTRACT
    if not path.is_file():
        raise FileNotFoundError(
            f"control-plane contract not found at {path}; pass the path to "
            "contracts/controlplane/openapi.v1.json explicitly (#616)"
        )
    text = path.read_text(encoding="utf-8")
    try:
        contract = json.loads(text)
    except json.JSONDecodeError as exc:
        return ConformanceReport(path, (f"<contract>: not valid JSON: {exc}",))
    divergences: list[str] = []
    if text != render_openapi_document(contract):
        divergences.append(
            "<contract>: file is not in canonical rendering "
            "(json.dumps indent=2 sort_keys + trailing newline) — re-render it"
        )
    divergences.extend(diff_documents(contract, openapi_spec()))
    return ConformanceReport(path, tuple(divergences))


__all__ = [
    "ConformanceReport",
    "check_conformance",
    "diff_documents",
]
