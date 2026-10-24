"""Bind exact artifact bytes to source and declared journey obligations."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .compiler import compile_graph
from .repository import GraphError, Repository, digest


def receipt(repo: Repository, artifacts: list[Path], environment: str = "unassigned") -> dict:
    if repo.dirty:
        raise GraphError("release evidence requires a clean source checkout")
    if not artifacts:
        raise GraphError("at least one built artifact is required")
    graph = compile_graph(repo)
    files = []
    for path in artifacts:
        if path.is_symlink() or not path.is_file():
            raise GraphError(f"release artifact must be a regular file: {path}")
        files.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    if len({f["name"] for f in files}) != len(files):
        raise GraphError("artifact names must be unique")
    requirements = [
        {"id": n["id"], "environment": n["environment"], "status": "not-recorded"}
        for n in graph["nodes"]
        if n["kind"] == "check"
    ]
    record = {
        "kind": "release-graph-receipt",
        "version": 1,
        "sourceRevision": repo.revision,
        "treeDigest": repo.tree_digest,
        "graphDigest": graph["digest"],
        "artifacts": sorted(files, key=lambda f: f["name"]),
        "environment": environment,
        "journeys": [n["id"] for n in graph["nodes"] if n["kind"] == "journey"],
        "journeyEvidence": requirements,
        "rollout": {
            "status": "pending",
            "requirements": [
                "target-environment journey smoke and observation window",
                "artifact installation/consumer validation",
                "verified recovery and worker version/drain procedures",
            ],
        },
        "meaning": "artifact identity and declared obligations; no deployment authorization or observed-success claim",
    }
    return {**record, "digest": digest(record)}
