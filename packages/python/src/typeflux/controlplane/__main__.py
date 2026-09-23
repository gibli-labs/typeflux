"""Serve the control-plane API, check contract conformance, or export the schema.

Usage:
    python -m typeflux.controlplane serve <typeflux.project.yaml>
    python -m typeflux.controlplane conformance [--contract PATH]
    python -m typeflux.controlplane openapi [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from typeflux.controlplane.api import (
    create_app,
    create_app_from_registry,
    render_openapi_spec,
)
from typeflux.controlplane.auth import build_authorizer
from typeflux.controlplane.conformance import check_conformance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m typeflux.controlplane")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve_parser = subcommands.add_parser(
        "serve", help="Run the control-plane API over one project, or a registry of several."
    )
    serve_parser.add_argument(
        "project",
        nargs="?",
        help="Path to a single typeflux.project.yaml manifest (omit when using --registry).",
    )
    serve_parser.add_argument(
        "--registry",
        help=(
            "Path to a typeflux.projects.yaml registry to serve several projects "
            "(#256). Mutually exclusive with the positional manifest."
        ),
    )
    serve_parser.add_argument(
        "--clone-cache",
        help=(
            "Directory for Git-sourced project clones (default: .typeflux-clones "
            "next to the registry file). Only used with --registry."
        ),
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8400)
    serve_parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "Allow this browser origin (repeatable). Off by default; the dev "
            "console proxies /api and needs none."
        ),
    )
    serve_parser.add_argument(
        "--auth-token",
        action="append",
        default=[],
        metavar="NAME:PERMS:TOKEN",
        help=(
            "Grant a bearer token a permission set (repeatable). PERMS is a "
            "comma-separated list of inspect,start,review,cancel,project.refresh "
            "(or '*'). Without any token the server is open (#292). Mutually "
            "exclusive with --trust-proxy-auth."
        ),
    )
    serve_parser.add_argument(
        "--temporal-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Bound for each Temporal-tier call (pin/start/status/review/cancel/"
            "executions/workers/versions); a timed-out call answers 503 (#581). "
            "Default 10, or the TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS "
            "environment variable."
        ),
    )
    serve_parser.add_argument(
        "--auth-token-file",
        metavar="PATH",
        help="Read additional NAME:PERMS:TOKEN lines (one per line; '#' comments) from a file.",
    )
    serve_parser.add_argument(
        "--trust-proxy-auth",
        action="store_true",
        help=(
            "Trust an authenticating reverse proxy that sets X-Typeflux-Actor and "
            "X-Typeflux-Permissions. Only use behind a proxy that strips those headers."
        ),
    )
    serve_parser.add_argument(
        "--ts-resolver-cmd",
        metavar="COMMAND",
        help=(
            "Command spawning the TypeScript subprocess resolver (#642), e.g. "
            "'node …/temporal-controlplane/dist/resolver-stdio.js'. Makes "
            "runtime:typescript projects resolvable (and plan-argument starts + "
            "policy selections honorable) alongside python ones. Off by default: "
            "typescript projects then answer 501 UnsupportedRuntime on "
            "resolution-dependent routes, exactly as before."
        ),
    )

    conformance_parser = subcommands.add_parser(
        "conformance",
        help=(
            "Check the emitted schema conforms to the normative contract "
            "(#616); exit 1 with a structured divergence report otherwise."
        ),
    )
    conformance_parser.add_argument(
        "--contract",
        default=None,
        metavar="PATH",
        help=(
            "Path to the contract document (default: "
            "contracts/controlplane/openapi.v1.json resolved in the monorepo)."
        ),
    )

    openapi_parser = subcommands.add_parser(
        "openapi",
        help=(
            "Export the emitted OpenAPI schema (inspection; the normative "
            "contract lives at contracts/controlplane/openapi.v1.json, #616)."
        ),
    )
    openapi_parser.add_argument(
        "--out",
        default="-",
        help="Output path for the JSON spec, or '-' for stdout (default).",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        # Validate the source selection before importing uvicorn so bad args
        # fail cleanly even where the optional server dependency is absent.
        if bool(args.project) == bool(args.registry):
            serve_parser.error("provide exactly one of a project manifest or --registry")
        if args.clone_cache and not args.registry:
            serve_parser.error("--clone-cache only applies with --registry")
        token_specs = list(args.auth_token)
        if args.auth_token_file:
            try:
                file_text = Path(args.auth_token_file).read_text(encoding="utf-8")
            except OSError as exc:
                serve_parser.error(f"--auth-token-file: {exc}")
            token_specs += [
                line.strip()
                for line in file_text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        try:
            authorizer = build_authorizer(token_specs, trust_proxy=args.trust_proxy_auth)
        except ValueError as exc:
            serve_parser.error(str(exc))
        # Per-runtime resolver multiplexing (#642): the in-process Python
        # resolver always serves; --ts-resolver-cmd adds the typescript one.
        resolvers = None
        if args.ts_resolver_cmd:
            from typeflux.controlplane.resolver import (
                InProcessPythonResolver,
                SubprocessResolver,
            )

            resolvers = [
                InProcessPythonResolver(),
                SubprocessResolver(args.ts_resolver_cmd, runtime="typescript"),
            ]
        if args.registry:
            app = create_app_from_registry(
                args.registry,
                cors_origins=tuple(args.cors_origin),
                clone_cache=args.clone_cache,
                authorizer=authorizer,
                resolvers=resolvers,
                temporal_tier_timeout_seconds=args.temporal_timeout,
            )
        else:
            app = create_app(
                args.project,
                cors_origins=tuple(args.cors_origin),
                authorizer=authorizer,
                resolvers=resolvers,
                temporal_tier_timeout_seconds=args.temporal_timeout,
            )

        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "conformance":
        try:
            report = check_conformance(args.contract)
        except FileNotFoundError as exc:
            conformance_parser.error(str(exc))
        sys.stdout.write(report.render() + "\n")
        return 0 if report.conformant else 1
    payload = render_openapi_spec()
    if args.out == "-":
        sys.stdout.write(payload)
    else:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
