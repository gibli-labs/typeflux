"""Bounded context and conservative, explainable impact over base and head."""

from __future__ import annotations

from collections import defaultdict, deque

from .repository import GraphError


def adjacency(graph: dict, *, impact=False) -> dict:
    result = defaultdict(list)
    forward = {"part_of", "defines", "implements", "requires", "verifies"}
    reverse = {"depends_on", "consumes", "provides", "uses", "verifies"}
    for edge in graph["edges"]:
        if not impact or edge["relation"] in forward:
            result[edge["source"]].append((edge["target"], edge))
        if not impact or edge["relation"] in reverse:
            result[edge["target"]].append((edge["source"], edge))
    return result


def walk(graph: dict, seeds: list[str], *, impact=False, max_depth=8) -> dict:
    adjacent = adjacency(graph, impact=impact)
    paths = {seed: [] for seed in sorted(seeds)}
    queue = deque((seed, 0) for seed in sorted(seeds))
    while queue:
        current, depth = queue.popleft()
        if not impact and depth >= max_depth:
            continue
        for target, edge in adjacent[current]:
            if target in paths:
                continue
            # Context includes a component's declarations, not every sibling file.
            if not impact and edge["relation"] == "part_of" and target.startswith("file:"):
                continue
            paths[target] = [*paths[current], edge]
            queue.append((target, depth + 1))
    return paths


def context(graph: dict, identifier: str, *, limit=60, max_chars=24000) -> dict:
    nodes = {n["id"]: n for n in graph["nodes"]}
    if identifier not in nodes:
        raise GraphError(f"unknown entity: {identifier}")
    if not 1 <= limit <= 500 or not 1000 <= max_chars <= 100000:
        raise GraphError("context bounds: limit 1..500, max-chars 1000..100000")
    paths = walk(graph, [identifier], max_depth=3)
    from .repository import canonical

    selected = []
    used = 0
    ordered = sorted(paths, key=lambda key: (len(paths[key]), key))
    for key in ordered:
        item = {"entity": nodes[key], "via": paths[key]}
        size = len(canonical(item))
        if len(selected) >= limit or used + size > max_chars:
            continue
        selected.append(item)
        used += size
    return {
        "revision": graph["revision"],
        "graphDigest": graph["digest"],
        "dirty": graph["dirty"],
        "query": identifier,
        "items": selected,
        "omittedCount": len(paths) - len(selected),
        "coverage": "declared/extracted; verification results are separate",
        "gaps": graph["gaps"],
        "expand": "context <entity-id> or explain <source-id> <target-id>",
    }


def explain(graph: dict, source: str, target: str) -> dict:
    ids = {n["id"] for n in graph["nodes"]}
    if source not in ids or target not in ids:
        raise GraphError("explain requires two existing entity IDs")
    paths = walk(graph, [source], impact=True)
    return {
        "revision": graph["revision"],
        "source": source,
        "target": target,
        "reachable": target in paths,
        "path": paths.get(target),
        "meaning": "modeled impact path; absence is not proof of independence",
    }


def impact(base: dict, head: dict) -> dict:
    before, after = base["inputs"], head["inputs"]
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    deleted = {p: before[p] for p in changed if p not in after}
    added = {p: after[p] for p in changed if p not in before}
    # Exact-content rename hints only. Both paths are still evaluated independently.
    renames = [
        {"from": old, "to": new}
        for old, value in deleted.items()
        for new, new_value in added.items()
        if value == new_value
    ]
    affected = {}
    checks = set()
    unknown = set()
    broad_reasons = []
    for label, graph in (("base", base), ("head", head)):
        nodes = {n["id"]: n for n in graph["nodes"]}
        seeds = ["file:" + p for p in changed if "file:" + p in nodes]
        paths = walk(graph, seeds, impact=True)
        unknown.update(set(changed) & set(graph["unknownFiles"]))
        for identifier, path in paths.items():
            kind = nodes[identifier]["kind"]
            if kind not in {"file", "external"}:
                affected.setdefault(identifier, {"id": identifier, "kind": kind, "via": {}})["via"][
                    label
                ] = path
            if kind == "check":
                checks.add(identifier)
        if graph["gaps"]:
            broad_reasons.append(f"{label} extraction has unresolved gaps")
    sensitive = [
        p
        for p in changed
        if p.startswith((".github/", "project-graph/", "tools/", "scripts/"))
        or p.rsplit("/", 1)[-1]
        in {
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "pyproject.toml",
            "uv.lock",
            "Dockerfile",
        }
        or p.startswith("contracts/")
    ]
    if sensitive:
        broad_reasons.append("shared contract, tool, policy, build or dependency input changed")
    if unknown:
        broad_reasons.append("changed files lack a primary component")
    return {
        "base": {"revision": base["revision"], "digest": base["digest"]},
        "head": {
            "revision": head["revision"],
            "digest": head["digest"],
            "dirty": head["dirty"],
        },
        "changedFiles": changed,
        "renameHints": renames,
        "affected": sorted(affected.values(), key=lambda n: n["id"]),
        "recommendedChecks": sorted(checks),
        "unknownFiles": sorted(unknown),
        "broadValidation": bool(broad_reasons),
        "reasons": sorted(set(broad_reasons)),
        "mode": "advisory; all existing mandatory CI still required",
    }
