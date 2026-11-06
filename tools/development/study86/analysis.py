"""Predeclared descriptive pilot analysis; no population speedup or selective-CI claim."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from tools.project_graph.records import Ledger

from .billing import effects, total_cost
from .experiment import read, sha


def family(task: str) -> str:
    if task in {"python-engine", "typescript-engine"}:
        return "mixed-engine"
    if task in {"graph-ci-ids", "graph-ci-whitespace"}:
        return "graph-ci"
    if task in {"skip-evidence", "skip-evidence-docs"}:
        return "skip-evidence"
    return task


def median(values):
    return statistics.median(values) if values else None


def summarize(root: Path, directory: Path) -> dict:
    plan = read(directory / "protocol.json")
    requested = len(plan["tasks"]) * len(plan["arms"]) * plan["repeats"]
    ledger = Ledger(root, "issue-86")
    paid = effects(ledger)
    pending = [key for key, item in paid.items() if item["state"] == "pending"]
    rows = [read(p) for p in sorted((directory / "trials").glob("*/result.json"))]
    main = [r for r in rows if not r.get("perturbation")]
    infrastructure = [
        {"trial": p.parent.parent.name, "generation": p.parent.name, **read(p)}
        for p in sorted((directory / "trials").glob("*/generation-*/infrastructure-failure.json"))
    ]
    tasks = {task["id"]: task for task in plan["tasks"]}
    for row in rows:
        if row.get("requestId"):
            record = paid.get("study86-" + row["requestId"])
            if (
                not record
                or record["state"] != "confirmed"
                or record["costUSD"] != row["reportedEstimateUSD"]
            ):
                raise RuntimeError("trial estimate does not reconcile with confirmed invocation")
    summary = {
        "protocolSha256": sha(directory / "protocol.json"),
        "authority": "descriptive localized patch pilot; not human PR acceptance",
        "recoverableInfrastructureEvents": infrastructure,
        "requestedPrimaryCells": requested,
        "observedPrimaryOutcomes": len(main),
        "unreachedPrimaryCells": requested - len(main),
        "pendingPaidOutcomes": pending,
        "reportedEstimateUSDAllStudyInvocations": total_cost(ledger),
        "reportedInvocationCount": len([v for v in paid.values() if v["state"] == "confirmed"]),
        "costBasis": plan["costBasis"],
        "arms": {},
        "robustness": [r for r in rows if r.get("perturbation")],
        "uncertainty": "Retrospective localized component scopes, only two repeats and correlated task families. Sensitivity ranges are not confidence intervals or population effects.",
        "unmeasured": [
            "human labor and maintenance time",
            "Codex orchestration charges",
            "provider invoice",
            "unobserved regressions beyond held-outs",
            "full interactive-agent productivity",
        ],
        "ciDecision": "Keep every mandatory CI/review/release obligation; no autonomy expansion.",
        "decisionRule": "Assembly is only a positive pilot signal if it wins at least two complete task families in both repeats, the declared deterministic policy guards pass, and it costs at most 1.25x search per accepted patch. These are bounded checks, not arbitrary-adversary proof. Even then it remains experimental; no broader adoption or CI change is authorized.",
    }
    for arm in plan["arms"]:
        selected = [r for r in main if r["arm"] == arm]
        accepted = [r for r in selected if r["passed"]]
        costs = []
        for row in selected:
            identifier = row.get("requestId")
            row_cost = row.get("reportedEstimateUSD", 0) or 0
            extra = paid.get("study86-grade-" + str(identifier))
            if extra:
                row_cost += extra.get("costUSD", 0)
            costs.append(row_cost)
        summary["arms"][arm] = {
            "outcomes": len(selected),
            "accepted": len(accepted),
            "acceptanceOnObservedSlots": len(accepted) / len(selected) if selected else None,
            "repeatAccepted": [
                sum(r["passed"] for r in selected if r["repeat"] == n)
                for n in range(plan["repeats"])
            ],
            "reportedEstimateUSD": sum(costs),
            "estimatePerAcceptedPatch": sum(costs) / len(accepted) if accepted else None,
            "cliReportedMedianSeconds": median(
                [
                    r["reportedModelDurationMilliseconds"] / 1000
                    for r in selected
                    if isinstance(r.get("reportedModelDurationMilliseconds"), (int, float))
                ]
            ),
            "contextPreparationMedianSeconds": median(
                [r["context"]["preparationSeconds"] for r in selected if "context" in r]
            ),
            "nativeValidationMedianSeconds": median(
                [r["validationSeconds"] for r in selected if "validationSeconds" in r]
            ),
            "reviewMedianSeconds": median(
                [r["reviewSeconds"] for r in selected if "reviewSeconds" in r]
            ),
            "failureClasses": {
                name: sum(r.get("failureClass") == name for r in selected)
                for name in sorted(
                    {r.get("failureClass") for r in selected if r.get("failureClass")}
                )
            },
            "declarationEdits": 0,
            "declarationMetricLimit": "Edit scopes are frozen in this component study; zero is by design, not zero human graph-maintenance cost.",
        }
        source_characters = outside = 0
        consumers = {}
        tokens = {}
        for row in selected:
            task = tasks[row["task"]]
            relevant = (
                set(task["editable"]) | set(task.get("contextPaths", [])) | set(task["heldout"])
            )
            for item in row.get("context", {}).get("included", []):
                source_characters += item["characters"]
                if item["path"] not in relevant:
                    outside += item["characters"]
            for check in row.get("native", {}).get("checks", {}).get("checks", []):
                if not check["passed"]:
                    consumers[check["kind"]] = consumers.get(check["kind"], 0) + 1
            for model, usage in (row.get("usage") or {}).items():
                bucket = tokens.setdefault(model, {})
                for field in [
                    "inputTokens",
                    "outputTokens",
                    "cacheReadInputTokens",
                    "cacheCreationInputTokens",
                ]:
                    bucket[field] = bucket.get(field, 0) + usage.get(field, 0)
        summary["arms"][arm].update(
            sourceCharacters=source_characters,
            outsideAssignedScopeSourceCharacters=outside,
            contextProxyLimit="Outside assigned implementation/context/held-out paths, not a semantic irrelevance label; dependencies can still be useful.",
            knownConsumersNotSatisfied=consumers,
            observedModelTokens=tokens,
        )
    complete = {}
    for row in main:
        complete.setdefault((row["task"], row["repeat"]), {})[row["arm"]] = row
    complete = {key: value for key, value in complete.items() if set(value) == set(plan["arms"])}
    summary["completeMatchedTaskRepeats"] = len(complete)
    families = sorted({family(key[0]) for key in complete})
    summary["pairedSensitivity"] = {}
    for arm in ["graph", "assembly"]:
        differences = [
            int(v[arm]["passed"]) - int(v["search"]["passed"]) for v in complete.values()
        ]
        ranges = []
        for omitted in families:
            remaining = [
                int(v[arm]["passed"]) - int(v["search"]["passed"])
                for key, v in complete.items()
                if family(key[0]) != omitted
            ]
            if remaining:
                ranges.append(sum(remaining) / len(remaining))
        summary["pairedSensitivity"][arm] = {
            "observedDifference": sum(differences) / len(differences) if differences else None,
            "leaveOneFamilyOutRange": [min(ranges), max(ranges)] if ranges else None,
        }
    summary["complete"] = (
        len(main) == requested
        and not pending
        and all(
            (directory / "trials" / f"graph-ci-ids-{arm}-r0-{probe}" / "result.json").exists()
            for arm in ["graph", "assembly"]
            for probe in ["stale", "deleted", "injection"]
        )
    )
    wins = []
    for group in families:
        grouped = [(key, value) for key, value in complete.items() if family(key[0]) == group]
        if all(
            any(key[1] == n for key, _ in grouped)
            and sum(
                int(v["assembly"]["passed"]) - int(v["search"]["passed"])
                for key, v in grouped
                if key[1] == n
            )
            > 0
            for n in range(plan["repeats"])
        ):
            wins.append(group)
    baseline_cost = summary["arms"]["search"]["estimatePerAcceptedPatch"]
    assembly_cost = summary["arms"]["assembly"]["estimatePerAcceptedPatch"]
    summary["assemblyFamiliesWinningBothRepeats"] = wins
    summary["positiveAssemblyPilotSignal"] = bool(
        summary["complete"]
        and plan.get("declaredPolicyGuardsPassed") is True
        and len(wins) >= 2
        and baseline_cost is not None
        and assembly_cost is not None
        and assembly_cost <= 1.25 * baseline_cost
    )
    summary["featureDecision"] = (
        "Retain the existing advisory graph and full verification controls. Keep source assembly experimental: "
        + (
            "there is a localized positive signal, requiring independent validation before broader use."
            if summary["positiveAssemblyPilotSignal"]
            else "the predeclared adoption signal was not demonstrated; no default or autonomy expansion."
        )
    )
    summary["robustness"] = [
        {key: r.get(key) for key in ["id", "passed", "failureClass", "reportedEstimateUSD"]}
        for r in summary["robustness"]
    ]
    (directory / "analysis.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
