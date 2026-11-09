"""Reproduce offline baseline/reference controls before freezing any paid protocol."""

import argparse
import json
from pathlib import Path

from .context import git, snapshot
from .experiment import source_hashes
from .validate import execute_native, link_environment, overlay_heldout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    args.output.mkdir()
    bindings = source_hashes(root)
    (args.output / "source-hashes.json").write_text(json.dumps(bindings, indent=2) + "\n")
    tasks = json.loads((Path(__file__).parent / "tasks.json").read_text())
    results = []
    successors = []
    for task in tasks:
        if "predecessor" in task or task["kind"] == "docs":
            continue
        for label, revision in [
            ("baseline", task["startingRevision"]),
            ("reference", task["referenceRevision"]),
        ]:
            directory = args.output / (task["id"] + "-" + label)
            directory.mkdir()
            identity = snapshot(root, revision, directory / "source", args.output / "packs")
            heldout = overlay_heldout(root, directory / "source", task)
            link_environment(directory / "source")
            result = execute_native(directory / "source", directory / "execution", args.image, task)
            results.append(
                {
                    "task": task["id"],
                    "label": label,
                    "identity": identity,
                    "heldout": heldout,
                    "result": result,
                }
            )
            (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "task": task["id"],
                        "label": label,
                        "passed": result["passed"],
                        "cleanupFailures": result["cleanupFailures"],
                    }
                ),
                flush=True,
            )
    parent = next(t for t in tasks if t["id"] == "graph-ci-ids")
    successor = {
        **next(t for t in tasks if t["id"] == "graph-ci-whitespace"),
        "startingRevision": parent["referenceRevision"],
        "referenceRevision": parent["referenceRevision"],
    }
    for label in ["baseline", "reference"]:
        directory = args.output / ("successor-" + label)
        directory.mkdir()
        identity = snapshot(
            root, parent["referenceRevision"], directory / "source", args.output / "packs"
        )
        if label == "reference":
            compiler = directory / "source/tools/project_graph/compiler.py"
            source = compiler.read_text()
            candidate = source.replace(
                "            identifier\n", "            identifier.strip()\n"
            ).replace("and entry[field] for field", "and entry[field].strip() for field")
            if candidate == source:
                raise RuntimeError("prospective positive control did not change source")
            compiler.write_text(candidate)
            (args.output / "successor-reference.patch").write_bytes(
                git(directory / "source", "diff")
            )
        heldout = overlay_heldout(root, directory / "source", successor)
        link_environment(directory / "source")
        result = execute_native(
            directory / "source", directory / "execution", args.image, successor
        )
        successors.append(
            {"label": label, "identity": identity, "heldout": heldout, "result": result}
        )
        (args.output / "successor-results.json").write_text(json.dumps(successors, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "task": successor["id"],
                    "label": label,
                    "passed": result["passed"],
                    "cleanupFailures": result["cleanupFailures"],
                }
            ),
            flush=True,
        )
    if source_hashes(root) != bindings:
        raise RuntimeError("source changed during native controls; do not freeze")


if __name__ == "__main__":
    main()
