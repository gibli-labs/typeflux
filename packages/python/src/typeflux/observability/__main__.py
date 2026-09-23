from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typeflux.env import load_env
from typeflux.observability.diff import diff_traces
from typeflux.observability.inspect import (
    TraceListQuery,
    TraceSearchQuery,
    TraceSummaryView,
    _as_utc_aware,
    export_execution_manifest,
    inspect_trace,
)
from typeflux.observability.langfuse import LangfuseObservabilityBackend

_RELATIVE_TIME_RE = re.compile(r"^([1-9]\d*)([mhd])$")
_LANGFUSE_DEFAULT_WINDOW = "24h"


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="python -m typeflux.observability")
    subcommands = parser.add_subparsers(dest="resource", required=True)
    trace = subcommands.add_parser("trace")
    trace_subcommands = trace.add_subparsers(dest="command", required=True)

    list_parser = trace_subcommands.add_parser("list")
    _add_backend_arg(list_parser)
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--cursor", help="Backend pagination cursor from a previous page")
    list_parser.add_argument("--workflow-name")
    list_parser.add_argument("--workflow-id")
    list_parser.add_argument("--status", choices=["ok", "error"])
    list_parser.add_argument("--since")
    list_parser.add_argument("--until")
    list_parser.add_argument("--json", action="store_true", help="Print JSON")

    search_parser = trace_subcommands.add_parser("search")
    _add_backend_arg(search_parser)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--cursor", help="Backend pagination cursor from a previous page")
    search_parser.add_argument("--scan-pages", type=int, default=5)
    search_parser.add_argument("--since")
    search_parser.add_argument("--until")
    search_parser.add_argument("--status", choices=["ok", "error"])
    search_parser.add_argument("--workflow-name")
    search_parser.add_argument("--workflow-id")
    search_parser.add_argument("--activity-name")
    search_parser.add_argument("--prompt-ref")
    search_parser.add_argument("--resolved-prompt-version")
    search_parser.add_argument("--input-schema-hash")
    search_parser.add_argument("--output-schema-hash")
    search_parser.add_argument("--activity-manifest-hash")
    search_parser.add_argument("--execution-manifest-hash")
    search_parser.add_argument("--workflow-contract-hash")
    search_parser.add_argument("--provider-model")
    search_parser.add_argument("--git-sha")
    search_parser.add_argument("--deployment-id")
    search_parser.add_argument("--environment")
    search_parser.add_argument("--temporal-address")
    search_parser.add_argument("--temporal-namespace")
    search_parser.add_argument("--temporal-region")
    search_parser.add_argument("--runtime-platform")
    search_parser.add_argument("--k8s-namespace")
    search_parser.add_argument("--k8s-deployment-name")
    search_parser.add_argument("--k8s-pod-name")
    search_parser.add_argument("--container-image")
    search_parser.add_argument("--policy-id")
    search_parser.add_argument("--policy-name")
    search_parser.add_argument("--policy-hash")
    search_parser.add_argument("--no-untagged-fallback", action="store_true")
    backend_filter = search_parser.add_mutually_exclusive_group()
    backend_filter.add_argument("--backend-filter", help="Backend-native JSON trace filter")
    backend_filter.add_argument(
        "--backend-filter-file", help="Path to backend-native JSON trace filter"
    )
    search_parser.add_argument("--json", action="store_true", help="Print JSON")

    # One --json convention across the trace CLI (#813): --json always means "machine
    # output". list/search toggle table-vs-JSON with it; inspect/diff/export are ALWAYS
    # JSON, so they accept it as a no-op for consistency, and verbosity gets its own
    # flag (--full) instead of overloading --json.
    inspect_parser = trace_subcommands.add_parser("inspect")
    _add_backend_arg(inspect_parser)
    inspect_parser.add_argument("--since")
    inspect_parser.add_argument("--until")
    inspect_parser.add_argument("--max-detail-pages", type=int)
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#813)",
    )
    inspect_parser.add_argument(
        "--full", action="store_true", help="Print the full public JSON instead of the summary"
    )
    inspect_parser.add_argument("trace_id")

    export_parser = trace_subcommands.add_parser("export")
    _add_backend_arg(export_parser)
    export_parser.add_argument("--since")
    export_parser.add_argument("--until")
    export_parser.add_argument("--max-detail-pages", type=int)
    export_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#813)",
    )
    export_parser.add_argument("trace_id")

    diff_parser = trace_subcommands.add_parser("diff")
    _add_backend_arg(diff_parser)
    diff_parser.add_argument("--since")
    diff_parser.add_argument("--until")
    diff_parser.add_argument("--max-detail-pages", type=int)
    diff_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#813)",
    )
    diff_parser.add_argument(
        "--full", action="store_true", help="Print the full public JSON instead of the summary"
    )
    diff_parser.add_argument("left_trace_id")
    diff_parser.add_argument("right_trace_id")

    args = parser.parse_args(argv)
    backend = _backend(args.backend)
    if args.command == "list":
        since = _bounded_langfuse_since(args.backend, args.since, parser)
        page = backend.reader.list_traces(
            TraceListQuery(
                limit=args.limit,
                cursor=args.cursor,
                workflow_name=args.workflow_name,
                workflow_id=args.workflow_id,
                status=args.status,
                since=since,
                until=_parse_cli_datetime(args.until, parser),
            )
        )
        if args.json:
            print(json.dumps(page.to_summary_dict(), indent=2, sort_keys=True))
        else:
            _print_trace_table(page.traces)
            _print_pagination_hint(page)
        return 0
    if args.command == "search":
        default_since_applied = args.backend == "langfuse" and args.since is None
        since = _bounded_langfuse_since(args.backend, args.since, parser)
        page = backend.reader.search_traces(
            TraceSearchQuery(
                limit=args.limit,
                cursor=args.cursor,
                scan_pages=args.scan_pages,
                since=since,
                until=_parse_cli_datetime(args.until, parser),
                status=args.status,
                workflow_name=args.workflow_name,
                workflow_id=args.workflow_id,
                activity_name=args.activity_name,
                prompt_ref=args.prompt_ref,
                resolved_prompt_version=args.resolved_prompt_version,
                input_schema_hash=args.input_schema_hash,
                output_schema_hash=args.output_schema_hash,
                activity_manifest_hash=args.activity_manifest_hash,
                execution_manifest_hash=args.execution_manifest_hash,
                workflow_contract_hash=args.workflow_contract_hash,
                provider_model=args.provider_model,
                git_sha=args.git_sha,
                deployment_id=args.deployment_id,
                environment=args.environment,
                temporal_address=args.temporal_address,
                temporal_namespace=args.temporal_namespace,
                temporal_region=args.temporal_region,
                runtime_platform=args.runtime_platform,
                k8s_namespace=args.k8s_namespace,
                k8s_deployment_name=args.k8s_deployment_name,
                k8s_pod_name=args.k8s_pod_name,
                container_image=args.container_image,
                policy_id=args.policy_id,
                policy_name=args.policy_name,
                policy_hash=args.policy_hash,
                include_untagged_fallback=not args.no_untagged_fallback,
                backend_filter=_parse_backend_filter(
                    args.backend_filter, args.backend_filter_file, parser
                ),
            )
        )
        if args.json:
            print(json.dumps(page.to_summary_dict(), indent=2, sort_keys=True))
        else:
            if default_since_applied:
                print(
                    f"Using default --since {_LANGFUSE_DEFAULT_WINDOW} for Langfuse trace search.",
                    file=sys.stderr,
                )
            _print_trace_table(page.traces)
            _print_page_warnings(page)
            _print_pagination_hint(page)
        return 0
    if args.command == "inspect":
        # Exact trace-id lookups take only caller-supplied bounds; the default
        # Langfuse lookback window applies to list/search scans only.
        inspection = inspect_trace(
            backend.reader,
            args.trace_id,
            since=_parse_cli_datetime(args.since, parser),
            until=_parse_cli_datetime(args.until, parser),
            max_detail_pages=args.max_detail_pages,
        )
        payload = inspection.to_json_dict() if args.full else inspection.to_summary_dict()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "export":
        print(
            json.dumps(
                export_execution_manifest(
                    backend.reader,
                    args.trace_id,
                    since=_parse_cli_datetime(args.since, parser),
                    until=_parse_cli_datetime(args.until, parser),
                    max_detail_pages=args.max_detail_pages,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "diff":
        diff = diff_traces(
            backend.reader,
            args.left_trace_id,
            args.right_trace_id,
            since=_parse_cli_datetime(args.since, parser),
            until=_parse_cli_datetime(args.until, parser),
            max_detail_pages=args.max_detail_pages,
        )
        payload = diff.to_json_dict() if args.full else diff.to_summary_dict()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    return 1


def _add_backend_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend", choices=["langfuse", "langsmith", "none"], default=_default_backend()
    )


def _default_backend() -> str:
    # The backend's own configuredness check (colocated with from_env), never a
    # hand-rolled env read that can drift from what construction requires.
    if LangfuseObservabilityBackend.is_configured():
        return "langfuse"
    if os.getenv("LANGSMITH_API_KEY"):
        return "langsmith"
    return "none"


def _backend(name: str):
    if name == "langfuse":
        return LangfuseObservabilityBackend.from_env()
    if name == "langsmith":
        from typeflux.observability.langsmith import LangSmithObservabilityBackend

        return LangSmithObservabilityBackend.from_env()
    raise RuntimeError("trace reading is not configured for backend 'none'")


def _print_trace_table(traces) -> None:
    rows = [TraceSummaryView.from_trace(trace).to_row_dict() for trace in traces]
    _print_table(
        rows,
        [
            "trace_id",
            "status",
            "workflow",
            "workflow_id",
            "git",
            "contract",
            "manifest",
            "activities",
            "prompts",
        ],
    )


def _print_pagination_hint(page) -> None:
    if page.next_cursor:
        print(f"More results available; rerun with --cursor {page.next_cursor}", file=sys.stderr)


def _print_page_warnings(page) -> None:
    for warning in getattr(page, "warnings", ()):
        print(warning, file=sys.stderr)


def _print_table(rows: list[dict[str, str]], columns: list[str]) -> None:
    if not rows:
        print("No traces found.")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    print("  ".join(column.ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def _parse_datetime(value: str | None, *, now: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    relative = _RELATIVE_TIME_RE.fullmatch(value)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        current = _as_utc_aware(now or _utc_now())
        if current is None:
            return None
        if unit == "m":
            return current - timedelta(minutes=amount)
        if unit == "h":
            return current - timedelta(hours=amount)
        return current - timedelta(days=amount)
    return _as_utc_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _parse_cli_datetime(
    value: str | None,
    parser: argparse.ArgumentParser,
) -> datetime | None:
    try:
        return _parse_datetime(value)
    except ValueError:
        parser.error("invalid datetime; use ISO timestamp or relative window like 15m, 24h, or 7d")


def _bounded_langfuse_since(
    backend: str,
    value: str | None,
    parser: argparse.ArgumentParser,
) -> datetime | None:
    if value is None and backend == "langfuse":
        return _parse_datetime(_LANGFUSE_DEFAULT_WINDOW)
    return _parse_cli_datetime(value, parser)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_backend_filter(
    inline: str | None,
    file_path: str | None,
    parser: argparse.ArgumentParser,
):
    if inline is None and file_path is None:
        return None
    raw = inline
    if file_path is not None:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"failed to read --backend-filter-file: {exc}")
    assert raw is not None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        parser.error(f"invalid backend filter JSON: {exc}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
