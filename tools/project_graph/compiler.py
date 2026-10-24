"""Compile reviewed declarations and extracted facts into a deterministic graph."""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import tomllib
from pathlib import PurePosixPath

import jsonschema
import yaml

from . import VERSION
from .repository import GraphError, Repository, digest, safe_path

API_VERSION = "typeflux.dev/graph/v1alpha1"
RELATIONS = {
    "part_of": ({"file"}, {"component"}),
    "implements": ({"component", "file"}, {"capability"}),
    "uses": ({"journey"}, {"capability"}),
    "requires": ({"capability", "journey"}, {"invariant"}),
    "verifies": ({"check"}, {"invariant", "capability", "journey"}),
    "documents": ({"file"}, {"capability", "journey", "component"}),
    "exemplifies": ({"file"}, {"capability", "journey"}),
    "defines": ({"file"}, {"api", "package", "check"}),
    "provides": ({"component"}, {"api"}),
    "consumes": ({"component"}, {"api"}),
    "depends_on": ({"component", "package"}, {"component", "package", "external"}),
}


def local_schema(value):
    """A checked-out schema must not fetch URLs or read filesystem references."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and not (
                isinstance(child, str) and child.startswith("#")
            ):
                raise GraphError("graph schemas permit local fragment references only")
            local_schema(child)
    elif isinstance(value, list):
        for child in value:
            local_schema(child)


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently losing a policy/mapping."""


def _mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise GraphError(f"duplicate or non-string YAML key: {key!r}")
        result[key] = value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def yaml_data(text: str):
    try:
        return yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise GraphError(str(error)) from error


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def owners(path: str, rules: list[tuple[str, list[str]]]) -> list[str]:
    """CODEOWNERS last matching rule wins; supported gitignore-style wildcards."""
    result = []
    for pattern, identities in rules:
        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")
        if pattern.endswith("/"):
            pattern += "**"
        if (
            pattern == "*"
            or fnmatch.fnmatchcase(path, pattern)
            or (
                not anchored
                and "/" not in pattern
                and any(fnmatch.fnmatchcase(part, pattern) for part in path.split("/"))
            )
        ):
            result = identities
    return result


def compile_graph(repo: Repository) -> dict:
    schema_path = "project-graph/schema/catalog.schema.json"
    try:
        schema = json.loads(repo.text(schema_path))
        local_schema(schema)
        jsonschema.Draft202012Validator.check_schema(schema)
    except (ValueError, jsonschema.SchemaError) as error:
        raise GraphError(f"invalid graph schema: {error}") from error
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    declarations = []
    gaps = []

    def provenance(source: str, evidence: str) -> dict:
        return {
            "source": source,
            "revision": repo.revision,
            "extractor": VERSION,
            "evidence": evidence,
        }

    def node(identifier: str, kind: str, source: str, evidence="extracted", **fields):
        if identifier in nodes:
            raise GraphError(f"duplicate entity: {identifier}")
        nodes[identifier] = {
            "id": identifier,
            "kind": kind,
            **fields,
            "provenance": provenance(source, evidence),
        }

    def edge(source: str, relation: str, target: str, origin: str, evidence="extracted"):
        edges[source, relation, target] = {
            "source": source,
            "relation": relation,
            "target": target,
            "provenance": provenance(origin, evidence),
        }

    for path in sorted(repo.files):
        if path.startswith("project-graph/") and path.endswith(".yaml") and "/schema/" not in path:
            document = yaml_data(repo.text(path))
            # Policy/evaluation files are independent records, not declarations.
            if path == "project-graph/policy.yaml":
                continue
            if not isinstance(document, dict) or "entities" not in document:
                raise GraphError(f"{path}: expected a graph catalog with entities")
            try:
                jsonschema.validate(document, schema)
            except jsonschema.ValidationError as error:
                raise GraphError(f"{path}: {error.message}") from error
            for entity in document["entities"]:
                fields = {k: v for k, v in entity.items() if k not in {"id", "kind", "links"}}
                node(entity["id"], entity["kind"], path, "declared", **fields)
                declarations.append((path, entity))

    components = [n for n in nodes.values() if n["kind"] == "component"]
    rules = []
    for line in repo.text(".github/CODEOWNERS").splitlines():
        parts = line.split("#", 1)[0].split()
        if parts:
            if parts[0].startswith("!") or "[" in parts[0]:
                raise GraphError(f"unsupported CODEOWNERS pattern: {parts[0]}")
            rules.append((parts[0], parts[1:]))
    for component in components:
        for selector in component["paths"]:
            if not safe_path(selector) or not any(matches(p, [selector]) for p in repo.files):
                raise GraphError(f"{component['id']}: unsafe or empty path selector {selector}")
    unknown = []
    for path in sorted(repo.files):
        assigned = [
            c["id"]
            for c in components
            if matches(path, c["paths"]) and not matches(path, c.get("exclude", []))
        ]
        if len(assigned) > 1:
            raise GraphError(f"ambiguous primary component for {path}: {assigned}")
        node(
            "file:" + path,
            "file",
            path,
            path=path,
            owners=owners(path, rules),
            component=assigned[0] if assigned else None,
        )
        if assigned:
            edge(
                "file:" + path,
                "part_of",
                assigned[0],
                nodes[assigned[0]]["provenance"]["source"],
                "declared",
            )
        else:
            unknown.append(path)
    unknown.extend(repo.omitted)
    for path, reason in repo.omitted.items():
        gaps.append({"source": path, "reason": reason})

    packages = {}
    pending_dependencies = []
    for path in sorted(repo.files):
        name = PurePosixPath(path).name
        if name == "package.json":
            manifest = json.loads(repo.text(path))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
                continue
            ecosystem = "npm"
            dependencies = [
                (k, v, group)
                for group in (
                    "dependencies",
                    "devDependencies",
                    "peerDependencies",
                    "optionalDependencies",
                )
                for k, v in manifest.get(group, {}).items()
            ]
        elif name == "pyproject.toml":
            data = tomllib.loads(repo.text(path))
            manifest = data.get("project", {})
            if not manifest.get("name"):
                continue
            ecosystem = "pypi"
            groups = {
                "dependencies": manifest.get("dependencies", []),
                **{f"extra:{k}": v for k, v in manifest.get("optional-dependencies", {}).items()},
                **{f"group:{k}": v for k, v in data.get("dependency-groups", {}).items()},
            }
            dependencies = []
            for group, requirements in groups.items():
                for requirement in requirements:
                    if isinstance(requirement, str):
                        match = re.match(r"[A-Za-z0-9_.-]+", requirement)
                        if match:
                            dependencies.append((match[0], requirement, group))
        else:
            continue
        identifier = "package:" + path
        node(
            identifier,
            "package",
            path,
            name=manifest["name"],
            version=manifest.get("version"),
            ecosystem=ecosystem,
            private=manifest.get("private", False),
        )
        edge("file:" + path, "defines", identifier, path)
        packages.setdefault((ecosystem, manifest["name"]), []).append(identifier)
        pending_dependencies.append((identifier, path, ecosystem, dependencies))
    for identifier, path, ecosystem, dependencies in pending_dependencies:
        for name, version, group in dependencies:
            candidates = packages.get((ecosystem, name), [])
            # A published version is not the source in this checkout.
            local = isinstance(version, str) and version.startswith(
                ("workspace:", "file:", "link:")
            )
            if local and len(candidates) == 1:
                target = candidates[0]
                own = nodes["file:" + path]["component"]
                other = nodes["file:" + target.removeprefix("package:")]["component"]
                if own and other and own != other:
                    edge(own, "depends_on", other, path)
            else:
                target = f"external:{ecosystem}:{name}"
                if target not in nodes:
                    node(target, "external", path, name=name, ecosystem=ecosystem)
                if local:
                    gaps.append(
                        {
                            "source": path,
                            "reason": f"unresolved local dependency: {name}",
                        }
                    )
            edge(identifier, "depends_on", target, path)
            # Preserve every constraint/group without collapsing optional/dev relationships.
            nodes[identifier].setdefault("dependencies", []).append(
                {"target": target, "constraint": version, "group": group}
            )

    contract = "contracts/controlplane/openapi.v1.json"
    spec = json.loads(repo.text(contract))
    for route, operations in sorted(spec["paths"].items()):
        for method, operation in sorted(operations.items()):
            if method not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
            }:
                continue
            identifier = "api:" + operation["operationId"]
            node(
                identifier,
                "api",
                contract,
                method=method.upper(),
                route=route,
                description=operation.get("summary", ""),
            )
            edge("file:" + contract, "defines", identifier, contract)

    for path, entity in declarations:
        if not entity["id"].startswith(entity["kind"] + "."):
            raise GraphError(f"entity ID/kind mismatch: {entity['id']}")
        for reference in entity.get("implementation", []):
            edge("file:" + reference, "implements", entity["id"], path, "declared")
        for link in entity.get("links", []):
            edge(entity["id"], link["relation"], link["target"], path, "declared")
        for field, relation in (
            ("documentation", "documents"),
            ("examples", "exemplifies"),
        ):
            for reference in entity.get(field, []):
                edge("file:" + reference, relation, entity["id"], path, "declared")
        for test in entity.get("tests", []):
            source = test["path"]
            text = repo.text(source)
            if source.endswith(".py"):
                names = {
                    n.name
                    for n in ast.walk(ast.parse(text))
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                found = test["name"] in names
            else:
                # Literal declarations only; collected IDs/results are separate evidence.
                found = bool(
                    re.search(
                        r"\b(?:it|test)(?:\.\w+)*\s*\(\s*(['\"])" + re.escape(test["name"]) + r"\1",
                        text,
                    )
                )
            if not found:
                raise GraphError(
                    f"{entity['id']}: missing static test declaration {source}::{test['name']}"
                )
            edge("file:" + source, "defines", entity["id"], path, "declared")
        for step in entity.get("steps", []):
            for reference in step["capabilities"] + step["checks"]:
                if reference not in nodes:
                    raise GraphError(f"{entity['id']}: missing step reference {reference}")

    for item in edges.values():
        source, relation, target = item["source"], item["relation"], item["target"]
        if source not in nodes or target not in nodes:
            raise GraphError(f"unresolved edge: {source} {relation} {target}")
        allowed = RELATIONS.get(relation)
        if (
            not allowed
            or nodes[source]["kind"] not in allowed[0]
            or nodes[target]["kind"] not in allowed[1]
        ):
            raise GraphError(f"invalid typed edge: {source} {relation} {target}")
    body = {
        "apiVersion": API_VERSION,
        "extractorVersion": VERSION,
        "revision": repo.revision,
        "dirty": repo.dirty,
        "treeDigest": repo.tree_digest,
        "inputs": repo.inputs,
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": sorted(edges.values(), key=lambda e: (e["source"], e["relation"], e["target"])),
        "unknownFiles": sorted(unknown),
        "gaps": sorted(gaps, key=lambda g: (g["source"], g["reason"])),
    }
    return {**body, "digest": digest(body)}


def compile_for_impact(repo: Repository) -> dict:
    """Pre-graph history stays unknown; never project future declarations backwards."""
    if "project-graph/schema/catalog.schema.json" in repo.files:
        return compile_graph(repo)
    body = {
        "apiVersion": API_VERSION,
        "extractorVersion": VERSION,
        "revision": repo.revision,
        "dirty": repo.dirty,
        "treeDigest": repo.tree_digest,
        "inputs": repo.inputs,
        "nodes": [
            {
                "id": "file:" + path,
                "kind": "file",
                "path": path,
                "provenance": {
                    "source": path,
                    "revision": repo.revision,
                    "extractor": VERSION,
                    "evidence": "extracted",
                },
            }
            for path in sorted(repo.files)
        ],
        "edges": [],
        "unknownFiles": sorted(repo.inputs),
        "gaps": [{"source": "project-graph/", "reason": "no graph declarations at this revision"}],
    }
    return {**body, "digest": digest(body)}
