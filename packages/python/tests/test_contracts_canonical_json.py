"""Cross-SDK canonical_json number-formatting parity (#420).

The fixture pairs a value with Python's `canonical_json(value)`; the TypeScript
SDK reproduces the same string byte-for-byte (test/canonical-json.test.ts). The
edge magnitudes are where JS `JSON.stringify` and Python `repr`/`json.dumps`
diverge (non-integral floats <1e-4 -> scientific; integral floats >=1e21 ->
full int), so this pins the `formatNumber` reconciliation.
"""

from __future__ import annotations

import json
from pathlib import Path

from typeflux.manifests._common import canonical_json

GOLDEN = (
    Path(__file__).resolve().parents[3] / "contracts" / "canonical-json" / "golden" / "numbers.json"
)


def test_canonical_json_number_fixture_reproduces() -> None:
    pairs = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert pairs, "fixture must not be empty"
    for pair in pairs:
        assert canonical_json(pair["value"]) == pair["canonical"]
