from __future__ import annotations

from pathlib import Path

from examples.regulated_disclosure_review.schemas import DisclosureInput

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "typeflux.yaml"


def sample_input() -> DisclosureInput:
    return DisclosureInput(
        case_id="DISC-2026-0042",
        customer_name="Avery Morgan",
        request="Disclose a material change in account terms ahead of renewal.",
        risk_notes=["regulated", "manual review"],
    )
