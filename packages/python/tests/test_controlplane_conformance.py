"""The structured conformance check of the emitted schema vs the contract (#616).

``diff_documents`` unit coverage, both fail-closed layers of
``check_conformance`` (canonical rendering + semantic divergence), and the
``conformance`` CLI exit codes. The green-path gate against the real contract
lives in ``test_controlplane_api.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from typeflux.controlplane import __main__ as controlplane_cli
from typeflux.controlplane import render_openapi_spec
from typeflux.controlplane.conformance import (
    ConformanceReport,
    check_conformance,
    diff_documents,
)

# One emitted-schema build shared by the mutation tests below (each parses a
# fresh copy); check_conformance() itself still emits internally.
_SPEC_TEXT = render_openapi_spec()


def test_identical_documents_have_no_divergences() -> None:
    document = {"a": [1, {"b": "x"}], "c": None}
    assert diff_documents(document, document) == []


def test_key_missing_from_emitted_is_a_contract_side_divergence() -> None:
    assert diff_documents({"a": 1, "b": 2}, {"a": 1}) == ["/b: missing from emitted schema"]


def test_key_not_in_contract_is_an_emitted_side_divergence() -> None:
    assert diff_documents({"a": 1}, {"a": 1, "b": 2}) == ["/b: not in contract"]


def test_value_divergence_names_both_sides() -> None:
    assert diff_documents({"a": {"b": 1}}, {"a": {"b": 2}}) == [
        "/a/b: value differs (contract=1, emitted=2)"
    ]


def test_type_divergence_uses_json_type_names() -> None:
    assert diff_documents({"a": True}, {"a": 1}) == [
        "/a: type differs (contract=boolean, emitted=integer)"
    ]
    assert diff_documents({"a": None}, {"a": {}}) == [
        "/a: type differs (contract=null, emitted=object)"
    ]


def test_empty_string_and_missing_are_distinct() -> None:
    # The "" vs absent distinction is load-bearing across the SDKs; the diff
    # must never conflate them.
    assert diff_documents({"a": ""}, {}) == ["/a: missing from emitted schema"]
    assert diff_documents({"a": ""}, {"a": ""}) == []
    assert diff_documents({"a": ""}, {"a": None}) == [
        "/a: type differs (contract=string, emitted=null)"
    ]


def test_array_divergences_report_length_and_elements() -> None:
    assert diff_documents({"a": [1, 2, 3]}, {"a": [1, 9]}) == [
        "/a: array length differs (contract=3, emitted=2)",
        "/a/1: value differs (contract=2, emitted=9)",
    ]


def test_json_pointer_escaping_for_path_keys() -> None:
    # API paths ("/api/v1/...") and "~" both occur as object keys.
    divergences = diff_documents(
        {"paths": {"/api/v1/x": 1, "til~de": 2}},
        {"paths": {"/api/v1/x": 9, "til~de": 8}},
    )
    assert divergences == [
        "/paths/~1api~1v1~1x: value differs (contract=1, emitted=9)",
        "/paths/til~0de: value differs (contract=2, emitted=8)",
    ]


def test_check_conformance_is_green_on_the_real_contract() -> None:
    report = check_conformance()
    assert report.conformant
    assert "conformant" in report.render()


def test_mutated_contract_fails_with_structured_divergences(tmp_path: Path) -> None:
    contract = json.loads(_SPEC_TEXT)
    contract["info"]["title"] = "Mutated Title"
    del contract["paths"]["/api/v1/deployments"]
    mutated = tmp_path / "openapi.v1.json"
    mutated.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = check_conformance(mutated)
    assert not report.conformant
    assert "/info/title: value differs" in report.render()
    assert "/paths/~1api~1v1~1deployments: not in contract" in report.render()


def test_non_canonical_contract_rendering_fails_closed(tmp_path: Path) -> None:
    # Same semantic content, different formatting: the hand-governed document
    # must stay in the canonical rendering so contract diffs stay reviewable.
    contract = tmp_path / "openapi.v1.json"
    contract.write_text(json.dumps(json.loads(_SPEC_TEXT)), encoding="utf-8")

    report = check_conformance(contract)
    assert not report.conformant
    assert report.divergences == (
        "<contract>: file is not in canonical rendering "
        "(json.dumps indent=2 sort_keys + trailing newline) — re-render it",
    )


def test_invalid_contract_json_fails_closed(tmp_path: Path) -> None:
    contract = tmp_path / "openapi.v1.json"
    contract.write_text("{not json", encoding="utf-8")

    report = check_conformance(contract)
    assert not report.conformant
    assert report.divergences[0].startswith("<contract>: not valid JSON")


def test_missing_contract_raises_a_pointer_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="contracts/controlplane/openapi.v1.json"):
        check_conformance(tmp_path / "absent.json")


def test_report_render_caps_the_divergence_list(tmp_path: Path) -> None:
    report = ConformanceReport(
        contract_path=tmp_path / "openapi.v1.json",
        divergences=tuple(f"/{i}: missing from emitted schema" for i in range(60)),
    )
    rendered = report.render()
    assert "60 divergence(s)" in rendered
    assert "/49: missing from emitted schema" in rendered
    assert "/50" not in rendered
    assert "… and 10 more" in rendered


def test_cli_conformance_green_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert controlplane_cli.main(["conformance"]) == 0
    assert "conformant" in capsys.readouterr().out


def test_cli_conformance_divergence_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = json.loads(_SPEC_TEXT)
    contract["info"]["version"] = "999"
    mutated = tmp_path / "openapi.v1.json"
    mutated.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert controlplane_cli.main(["conformance", "--contract", str(mutated)]) == 1
    out = capsys.readouterr().out
    assert "/info/version: value differs" in out
