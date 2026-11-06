"""Prepare a source-free consumer image context; does not start or publish anything."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import yaml
from tools.project_graph.compiler import compile_graph
from tools.project_graph.repository import Repository


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path, artifacts: Path, accepted_path: Path, output: Path) -> None:
    """Bind locally built bytes to a clean producer and an already fetched CI receipt.

    The maintained release_evidence command must fetch/validate the receipt first;
    this local inventory does not independently grant trusted CI or publication.
    """
    repo = Repository(root)
    accepted = json.loads(accepted_path.read_text())
    if (
        repo.dirty
        or accepted.get("kind") != "ci-verification"
        or accepted.get("passed") is not True
        or accepted.get("headRevision") != repo.revision
        or accepted.get("testedRevision") != repo.revision
        or accepted.get("treeDigest") != repo.tree_digest
    ):
        raise ValueError(
            "candidate requires clean exact producer and validated accepted-main receipt"
        )
    paths = sorted(path for path in artifacts.iterdir() if not path.name.startswith("."))
    if len(paths) != 8 or any(not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError("expected eight regular candidate artifact files")
    record = {
        "producerRevision": repo.revision,
        "producerInputs": repo.tree_digest,
        "graphDigest": compile_graph(repo)["digest"],
        "acceptedMain": {
            "run": accepted["runId"],
            "attempt": accepted["attempt"],
            "receiptSha256": digest(accepted_path),
        },
        "artifacts": [
            {"name": p.name, "bytes": p.stat().st_size, "sha256": digest(p)} for p in paths
        ],
        "scope": "local artifact inventory; not independent CI, publication or deployment acceptance",
    }
    with output.open("x") as destination:
        destination.write(json.dumps(record, indent=2) + "\n")


def prepare(root: Path, artifacts: Path, destination: Path) -> None:
    destination.mkdir()  # Never overwrite an earlier attempt or its evidence.
    (destination / "artifacts").mkdir()
    for path in sorted(artifacts.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            shutil.copyfile(path, destination / "artifacts" / path.name)
    here = Path(__file__).parent
    for path in here.iterdir():
        if path.suffix in {".Dockerfile", ".mjs", ".py", ".conf"}:
            shutil.copyfile(path, destination / path.name)
    python = destination / "python"
    python.mkdir()
    for name in ["pyproject.toml", "uv.lock", "README.md", "LICENSE"]:
        shutil.copyfile(root / "packages/python" / name, python / name)
    shutil.copytree(
        root / "packages/python/examples/local_runtime",
        destination / "local-python",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        root / "packages/typescript/yaml/examples/local-runtime", destination / "local-typescript"
    )
    for edition in ["python", "typescript"]:
        source = root / "contracts/controlplane/conformance/project" / edition
        target = destination / edition
        target.mkdir(exist_ok=True)
        if edition == "python":
            shutil.copytree(
                source / "conformance_project",
                target / "conformance_project",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copyfile(here / "provider.py", target / "conformance_project/provider.py")
        spec = yaml.safe_load((source / "workflow.yaml").read_text())
        spec["runtime"]["temporal"] = {
            "address": "temporal:7233",
            "namespace": "default",
            "tls": False,
        }
        provider = "custom" if edition == "python" else "fake"
        spec["runtime"]["provider"] = {"type": provider, "model": "offline"}
        if edition == "python":
            spec["runtime"]["provider"].update(
                {
                    "class": "conformance_project.provider:DeployedProvider",
                    "config": {"mode": "normal"},
                }
            )
            spec["runtime"]["imports"] = {"allow_provider_class": True}
        spec["workflow"]["name"] = "DeployedReview" + edition.title()
        spec["workflow"]["version"] = "v1"
        spec["task_queue"] = edition + "-v1"
        specs = {"workflow": spec}
        for name in ["next", "incompatible", "wrong", "retry"]:
            changed = json.loads(json.dumps(spec))
            if name in {"next", "incompatible"}:
                changed["workflow"]["steps"].insert(
                    2, {"id": "repeat_summary", "activity": "summarize"}
                )
            if name == "next":
                changed["workflow"]["version"] = "v2"
                changed["task_queue"] = edition + "-v2"
            if name == "wrong":
                changed["project"] = "different_project"
                changed["workflow"]["name"] += "WrongProject"
                # Python import resolution must still use the real fixture module.
                if edition == "python":
                    shutil.copytree(target / "conformance_project", target / "different_project")
                    activity_path = target / "different_project/activities.py"
                    activity_path.write_text(
                        activity_path.read_text().replace(
                            "from conformance_project.schemas", "from different_project.schemas"
                        )
                    )
                    changed["runtime"]["provider"]["class"] = (
                        "different_project.provider:DeployedProvider"
                    )
            if name == "retry":
                changed["workflow"]["name"] += "Retry"
                changed["task_queue"] = edition + "-retry"
                changed["runtime"]["provider_retry"] = {"max_attempts": 1}
                changed["runtime"]["activity_retry"] = {
                    "maximum_attempts": 2,
                    "initial_interval_seconds": 5,
                    "maximum_interval_seconds": 5,
                    "backoff_coefficient": 1,
                }
                if edition == "python":
                    changed["runtime"]["provider"]["config"]["mode"] = "retry"
            specs[name] = changed
        for name, value in specs.items():
            (target / f"{name}.yaml").write_text(yaml.safe_dump(value, sort_keys=False))
        policy = yaml.safe_load((source / "policies/base.yaml").read_text())
        allowed = {provider: {"models": ["offline"]}}
        policy["providers"]["allowed"] = allowed
        policy["risk_tiers"]["policy_gated"]["constrain_providers"] = allowed
        (target / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
        environment = {
            "version": "1",
            "name": "local",
            "overrides": {
                "runtime": {
                    "temporal": spec["runtime"]["temporal"],
                    "observability": {"type": "none"},
                }
            },
        }
        (target / "environment.yaml").write_text(yaml.safe_dump(environment))
        manifest = {
            "version": "1",
            "name": "docker-review",
            "workflows": [{"id": "workflow", "path": "workflow.yaml"}],
            "environments": {"local": "environment.yaml"},
            "policies": {"base": "policy.yaml"},
            "validation": {
                "targets": {
                    "local": {
                        "workflows": ["workflow"],
                        "environment": "local",
                        "policies": ["base"],
                    }
                }
            },
        }
        (target / "typeflux.project.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        entries = [{"id": "deployment", "manifest": "typeflux.project.yaml", "runtime": edition}]
        for name in ["next", "incompatible", "wrong", "retry"]:
            variant = target / name
            variant.mkdir()
            for file in ["environment.yaml", "policy.yaml", "typeflux.project.yaml"]:
                shutil.copyfile(target / file, variant / file)
            (variant / "workflow.yaml").write_text(yaml.safe_dump(specs[name], sort_keys=False))
            if edition == "python":
                for module in ["conformance_project", "different_project"]:
                    shutil.copytree(target / module, variant / module)
            entries.append(
                {"id": name, "manifest": f"{name}/typeflux.project.yaml", "runtime": edition}
            )
        registry = {"version": "1", "default": "deployment", "projects": entries}
        (target / "typeflux.projects.yaml").write_text(yaml.safe_dump(registry))
    files = {
        str(p.relative_to(destination)): digest(p)
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }
    (destination / "prepared.json").write_text(
        json.dumps(
            {
                "files": files,
                "scope": "prepared inputs only; not installed, frozen, deployed or passing",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--accepted-main", type=Path)
    parser.add_argument("--inventory-output", type=Path)
    args = parser.parse_args()
    if bool(args.accepted_main) != bool(args.inventory_output):
        parser.error("--accepted-main and --inventory-output must be supplied together")
    if args.accepted_main:
        inventory(args.root, args.artifacts, args.accepted_main, args.inventory_output)
    prepare(args.root, args.artifacts, args.destination)
