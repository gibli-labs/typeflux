from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import compile_for_impact, compile_graph
from .queries import context, explain, impact
from .repository import GraphError, Repository


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Typeflux repository graph (development tool)")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "validate", "context", "explain", "collect", "benchmark"):
        sub = commands.add_parser(command)
        sub.add_argument("--revision", help="Commit/ref; omitted reads the working tree")
        if command == "context":
            sub.add_argument("entity")
            sub.add_argument("--limit", type=int, default=60)
            sub.add_argument("--max-chars", type=int, default=24000)
        if command == "explain":
            sub.add_argument("source")
            sub.add_argument("target")
    sub = commands.add_parser("impact")
    sub.add_argument("--base", required=True)
    sub.add_argument("--head", help="Commit/ref; omitted reads the working tree")
    from .integration import register

    register(commands)
    args = parser.parse_args(argv)
    try:
        if args.command in {"report", "explore", "aggregate", "release", "task", "verify"}:
            from .integration import execute

            result = execute(args)
        elif args.command == "impact":
            result = impact(
                compile_for_impact(Repository(args.root, args.base)),
                compile_for_impact(Repository(args.root, args.head)),
            )
        else:
            graph = compile_graph(Repository(args.root, args.revision))
            if args.command == "build":
                result = graph
            elif args.command == "validate":
                result = {
                    "valid": True,
                    "revision": graph["revision"],
                    "digest": graph["digest"],
                    "dirty": graph["dirty"],
                    "nodes": len(graph["nodes"]),
                    "edges": len(graph["edges"]),
                    "unknownFiles": graph["unknownFiles"],
                    "gaps": graph["gaps"],
                    "testCoverage": "static declarations; no test execution claimed",
                }
            elif args.command == "context":
                result = context(graph, args.entity, limit=args.limit, max_chars=args.max_chars)
            elif args.command == "explain":
                result = explain(graph, args.source, args.target)
            elif args.command == "collect":
                if args.revision is not None:
                    raise GraphError(
                        "collect runs the working tree only; use an isolated checkout for historical revisions"
                    )
                from .collection import collect_tests

                result = collect_tests(args.root, graph)
            else:
                from .benchmark import benchmark

                repo = Repository(args.root, args.revision)
                tasks = json.loads(repo.text("project-graph/evaluation/navigation.json"))
                result = benchmark(repo, graph, tasks)
        rendered = (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, sort_keys=True) + "\n"
        )
        if args.format == "text" and isinstance(result, dict):
            rendered = render_text(result)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        else:
            print(rendered, end="")
        if args.command == "aggregate" and not result["passed"]:
            return 1
        if args.command == "verify" and result["payload"]["result"] != "passed":
            return 1
        return 0
    except (GraphError, ValueError, OSError) as error:
        print(f"project-graph: {error}", file=sys.stderr)
        return 2


def render_text(result: dict) -> str:
    if "items" in result:
        lines = [
            f"Context: {result['query']} @ {result['revision']}",
            result["coverage"],
        ]
        for item in result["items"]:
            entity = item["entity"]
            lines.append(f"- {entity['id']}: {entity.get('description', entity.get('path', ''))}")
            lines.append(
                f"  Source: {entity['provenance']['source']} ({entity['provenance']['evidence']})"
            )
        lines.append(f"Omitted: {result['omittedCount']}. {result['expand']}")
        return "\n".join(lines) + "\n"
    return json.dumps(result, indent=2) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
