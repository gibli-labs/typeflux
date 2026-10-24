"""Explicit test collection, separate from static graph compilation.

Collection imports proposed test code. Run in the same unprivileged environment as
tests; the resulting local record is not a trusted CI pass or release attestation.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from .repository import GraphError, Repository


def collection_environment() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY"))
        and not k.upper().startswith(("ACTIONS_", "INPUT_", "GITHUB_"))
    }


def collect_tests(root: Path, graph: dict) -> dict:
    root = root.resolve()
    groups = defaultdict(set)
    references = []
    for node in graph["nodes"]:
        if node["kind"] != "check":
            continue
        for test in node["tests"]:
            references.append({"check": node["id"], **test})
            path = root / test["path"]
            if path.suffix == ".py":
                groups[("pytest", root / "packages/python")].add(path)
            else:
                package = path.parent
                while package != root and not (package / "package.json").is_file():
                    package = package.parent
                if package == root:
                    raise GraphError(f"no test package for {test['path']}")
                runner = "playwright" if path.name.endswith(".spec.ts") else "vitest"
                groups[(runner, package)].add(path)
    collected = []
    commands = []
    env = collection_environment()
    with tempfile.TemporaryDirectory(prefix="typeflux-graph-collect-") as directory:
        for (runner, cwd), paths in sorted(groups.items()):
            output = Path(directory) / f"{runner}.json"
            if runner == "pytest":
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-p",
                    "tools.project_graph.pytest_inventory",
                    *map(str, sorted(paths)),
                ]
                run_env = {
                    **env,
                    "PYTHONPATH": str(root),
                    "TYPEFLUX_GRAPH_ROOT": str(root),
                    "TYPEFLUX_GRAPH_INVENTORY": str(output),
                }
            elif runner == "vitest":
                # --json accepts an optional output filename: keep it LAST so a
                # test path cannot be consumed as the output and overwritten.
                command = [
                    "pnpm",
                    "exec",
                    "vitest",
                    "list",
                    "--no-static-parse",
                    *map(str, sorted(paths)),
                    "--json",
                ]
                # Collect gated Temporal test identities without executing hooks.
                run_env = {**env, "TYPEFLUX_LIVE_TEMPORAL": "1"}
            else:
                command = [
                    "npm",
                    "--silent",
                    "exec",
                    "--",
                    "playwright",
                    "test",
                    "--list",
                    "--reporter=json",
                    *map(str, sorted(paths)),
                ]
                run_env = env
            result = subprocess.run(
                command, cwd=cwd, env=run_env, capture_output=True, text=True, timeout=120
            )
            if result.returncode:
                raise GraphError(
                    f"{runner} collection failed: {result.stderr[-2000:]} {result.stdout[-2000:]}"
                )
            commands.append(
                {
                    "runner": runner,
                    "cwd": str(cwd.relative_to(root)),
                    "files": [str(p.relative_to(root)) for p in sorted(paths)],
                }
            )
            if runner == "pytest":
                collected.extend(json.loads(output.read_text()))
            elif runner == "vitest":
                for item in json.loads(result.stdout):
                    collected.append(
                        {
                            "path": str(Path(item["file"]).relative_to(root)),
                            "name": item["name"].split(" > ")[-1],
                            "id": item["name"],
                        }
                    )
            else:
                report = json.loads(result.stdout)

                def visit(suite):
                    for spec in suite.get("specs", []):
                        path = Path(spec["file"])
                        if not path.is_absolute():
                            path = Path(report["config"]["rootDir"]) / path
                        collected.append(
                            {
                                "path": str(path.relative_to(root)),
                                "name": spec["title"],
                                "id": spec["id"],
                            }
                        )
                    for child in suite.get("suites", []):
                        visit(child)

                for suite in report["suites"]:
                    visit(suite)
    identities = {(t["path"], t["name"]) for t in collected}
    missing = [r for r in references if (r["path"], r["name"]) not in identities]
    if missing:
        raise GraphError(f"declared tests absent from collection: {missing}")
    if Repository(root).tree_digest != graph["treeDigest"]:
        raise GraphError("test collection changed repository inputs; inventory is invalid")
    return {
        "kind": "test-collection",
        "revision": graph["revision"],
        "treeDigest": graph["treeDigest"],
        "graphDigest": graph["digest"],
        "dirty": graph["dirty"],
        "python": platform.python_version(),
        "commands": commands,
        "references": references,
        "collected": collected,
        "meaning": "test identities collected; tests have not run",
    }
