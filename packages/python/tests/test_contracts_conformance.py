"""Suite-integrity gate for the cross-SDK conformance index (#392).

The per-fixture reproductions live in ``test_contracts_{cache,schema,shapes}.py``;
this gate keeps the index (``contracts/conformance.json``) honest: the version is
pinned, every referenced fixture exists and parses, and the index covers exactly
the contract directories on disk (so a new contract can't ship undocumented and a
fixture can't be removed without breaking the suite).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _index() -> dict:
    return json.loads((CONTRACTS / "conformance.json").read_text(encoding="utf-8"))


def test_contract_version_matches_bundle() -> None:
    version = (CONTRACTS / "CONTRACT_VERSION").read_text(encoding="utf-8").strip()
    assert _index()["contract_version"] == version


def test_every_referenced_fixture_exists_and_parses() -> None:
    for area in _index()["areas"]:
        for fixture in area["fixtures"]:
            for key in ("input", "golden"):
                rel = fixture.get(key)
                if rel is None:
                    continue
                path = CONTRACTS / rel
                assert path.is_file(), f"missing fixture {rel} for area {area['name']!r}"
                json.loads(path.read_text(encoding="utf-8"))


def test_index_partitions_contract_directories() -> None:
    # The index is the classification authority (#616): every contracts/
    # subdirectory is exactly one of an area (golden-reproduction parity) or
    # an interface (normative surface document), so a contract of either
    # class can't ship undocumented or be registered as both. Directory shape
    # does not classify — an interface may later gain fixtures (#617) without
    # becoming an area.
    index = _index()
    areas = {area["name"] for area in index["areas"]}
    interfaces = {interface["name"] for interface in index["interfaces"]}
    on_disk = {p.name for p in CONTRACTS.iterdir() if p.is_dir()}
    assert areas | interfaces == on_disk
    assert not (areas & interfaces)


def test_every_area_has_goldens() -> None:
    for area in _index()["areas"]:
        assert (CONTRACTS / area["name"] / "golden").is_dir(), (
            f"area {area['name']!r} has no golden/ directory"
        )


def test_every_interface_document_exists_and_parses() -> None:
    for interface in _index()["interfaces"]:
        path = CONTRACTS / interface["document"]
        assert path.is_file(), f"missing document for interface {interface['name']!r}"
        json.loads(path.read_text(encoding="utf-8"))


def test_resolver_response_schemas_exist_in_the_controlplane_contract() -> None:
    # The resolver interface returns exactly what the API serves (#619): every
    # operation's response_schema must be a component of the control-plane
    # OpenAPI document — no invented shapes, no dangling names after a rename.
    # One sanctioned exception (#642): an operation whose response is NOT an
    # API DTO (resolve_plan — the plan is a binding-profile artifact no route
    # serves) declares `response_schema: "inline …"` and must carry its full
    # inline `response` definition instead — explicit, never dangling.
    resolver = json.loads((CONTRACTS / "resolver" / "resolver.v1.json").read_text(encoding="utf-8"))
    source = json.loads(
        (CONTRACTS / resolver["response_schemas_source"]).read_text(encoding="utf-8")
    )
    components = set(source["components"]["schemas"])
    for name, operation in resolver["operations"].items():
        schema = operation["response_schema"]
        if schema.startswith("inline"):
            response = operation.get("response")
            assert isinstance(response, dict) and response, (
                f"resolver op {name!r}: an inline response_schema must define its `response` fields"
            )
            continue
        assert schema in components, f"resolver op {name!r}: unknown schema {schema!r}"


def test_interface_conformance_suites_index_their_fixtures() -> None:
    # An interface may carry an HTTP-level conformance suite (#617): the suite
    # index and every case it references must exist and parse, and every case
    # file on disk must be indexed — a case can't ship outside the suite.
    for interface in _index()["interfaces"]:
        suite_rel = interface.get("conformance")
        if suite_rel is None:
            continue
        suite_path = CONTRACTS / suite_rel
        assert suite_path.is_file(), f"missing conformance suite for {interface['name']!r}"
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        fixtures_dir = suite_path.parent
        for case_file in suite["cases"]:
            case_path = fixtures_dir / case_file
            assert case_path.is_file(), f"missing conformance case {case_file}"
            case = json.loads(case_path.read_text(encoding="utf-8"))
            assert {"name", "request", "response"} <= set(case), case_file
        on_disk = {p.name for p in fixtures_dir.glob("*.json")} - {suite_path.name}
        assert set(suite["cases"]) == on_disk, "conformance case files not indexed"


def test_every_area_declares_per_sdk_coverage() -> None:
    for area in _index()["areas"]:
        coverage = area["coverage"]
        assert coverage["python"] in {"full", "partial"}
        assert coverage["typescript"] in {"full", "partial"}


def test_index_references_every_fixture_file_per_area() -> None:
    # A new golden/input added to an existing area must enter the index too,
    # so a fixture can't ship outside the conformance suite.
    for area in _index()["areas"]:
        referenced = {
            Path(fixture[key]).name
            for fixture in area["fixtures"]
            for key in ("input", "golden")
            if fixture.get(key) is not None
        }
        on_disk = {p.name for p in (CONTRACTS / area["name"] / "golden").glob("*.json")}
        assert referenced == on_disk, f"index/golden file mismatch for {area['name']!r}"


def test_edition_blocks_are_wellformed_and_never_replace_the_base_golden() -> None:
    # Edition variants/skips (#620 slice 4) are EXPLICIT divergence records: every
    # editions key must be a suite-registered edition; a skip must point at a live
    # issue; a variant must say WHY it diverges; and the base golden stays intact —
    # an editions block narrows a lane, it never rewrites the shared recording.
    for interface in _index()["interfaces"]:
        suite_rel = interface.get("conformance")
        if suite_rel is None:
            continue
        suite_path = CONTRACTS / suite_rel
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        known_editions = set(suite.get("editions", {}))
        for case_file in suite["cases"]:
            case = json.loads((suite_path.parent / case_file).read_text(encoding="utf-8"))
            assert "response" in case, f"{case_file}: base golden missing"
            for edition, variant in case.get("editions", {}).items():
                assert edition in known_editions, f"{case_file}: unknown edition {edition!r}"
                if "skip" in variant:
                    assert re.search(r"#\d+", variant["skip"]), (
                        f"{case_file}: edition skip must cite a live issue"
                    )
                    assert "response" not in variant, (
                        f"{case_file}: a skipped edition must not also carry a response"
                    )
                else:
                    assert str(variant.get("note", "")).strip(), (
                        f"{case_file}: edition variant needs a why-note"
                    )
                    assert {"status", "body"} <= set(variant.get("response", {})), (
                        f"{case_file}: edition variant response must carry status+body"
                    )
