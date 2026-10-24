"""Diagnostic retrieval comparison; does not claim agent productivity."""

from __future__ import annotations

import re
import time

from .queries import context
from .repository import Repository


def benchmark(repo: Repository, graph: dict, tasks: list[dict]) -> dict:
    results = []
    for task in tasks:
        start = time.perf_counter()
        pattern = re.compile(task["search"], re.IGNORECASE)
        search = [
            p
            for p, data in sorted(repo.files.items())
            if not p.startswith(("project-graph/", "tools/project_graph/"))
            and pattern.search(data.decode(errors="replace"))
        ][: task["limit"]]
        search_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        response = context(graph, task["entity"], limit=task["limit"])
        found = [i["entity"]["path"] for i in response["items"] if i["entity"]["kind"] == "file"]
        graph_ms = (time.perf_counter() - start) * 1000
        expected = set(task["expected"])
        results.append(
            {
                "task": task["id"],
                "expected": sorted(expected),
                "search": {
                    "hits": sorted(expected & set(search)),
                    "returned": len(search),
                    "milliseconds": round(search_ms, 3),
                },
                "graph": {
                    "hits": sorted(expected & set(found)),
                    "returned": len(response["items"]),
                    "milliseconds": round(graph_ms, 3),
                },
            }
        )
    return {
        "kind": "retrieval-diagnostic",
        "revision": repo.revision,
        "graphDigest": graph["digest"],
        "results": results,
        "limitations": [
            "curated navigation tasks, not agent development trials",
            "graph declarations were authored with this repository available",
            "lexical file-search baseline; no interactive refinement",
            "no correctness, productivity, cost or CI-selection safety claim",
        ],
    }
