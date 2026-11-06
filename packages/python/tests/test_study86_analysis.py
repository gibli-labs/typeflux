"""Missing observations never become an adoption claim, even when observed patches pass."""

import json

import pytest
from test_study86_billing import study as study
from tools.development.study86.analysis import summarize


def test_incomplete_pilot_cannot_claim_complete_or_expand_autonomy(tmp_path):
    directory = tmp_path / "experiment"
    directory.mkdir()
    trials = directory / "trials" / "one"
    trials.mkdir(parents=True)
    (directory / "protocol.json").write_text(
        json.dumps(
            {
                "tasks": [{"id": "fixture", "editable": [], "heldout": []}],
                "arms": ["search", "graph", "assembly"],
                "repeats": 2,
                "costBasis": "estimate only",
                "declaredPolicyGuardsPassed": True,
            }
        )
    )
    (trials / "result.json").write_text(
        json.dumps({"id": "one", "task": "fixture", "arm": "assembly", "repeat": 0, "passed": True})
    )
    result = summarize(tmp_path, directory)
    assert not result["complete"] and not result["positiveAssemblyPilotSignal"]
    assert result["unreachedPrimaryCells"] == 5
    assert result["arms"]["search"]["acceptanceOnObservedSlots"] is None
    assert result["reportedInvocationCount"] == 0
    assert "no autonomy expansion" in result["ciDecision"]


def test_analysis_refuses_unaccounted_model_cost(study):
    directory = study["directory"]
    directory.mkdir()
    (directory / "protocol.json").write_text(
        json.dumps({"tasks": [], "arms": [], "repeats": 2, "costBasis": "estimate"})
    )
    trial = directory / "trials" / "one"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"requestId": "unaccounted", "reportedEstimateUSD": 0.1})
    )
    with pytest.raises(RuntimeError, match="does not reconcile"):
        summarize(study["root"], directory)
