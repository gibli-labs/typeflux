#!/usr/bin/env python3
"""HTTP-level conformance runner for the Typeflux control plane (#617).

Black-box: point it at any control-plane server (or let it spawn one) serving
the canonical conformance fixture project, and it replays the golden requests
under ``fixtures/`` and asserts on normalized responses. Stdlib-only on
purpose — any Python 3.11+ runs it with no installs, so every server edition
(Python, TypeScript #563/#620) shares one harness.

Usage:
    python runner.py --base-url http://127.0.0.1:8400
    python runner.py --server-cmd '<command that serves the fixture project>' --port 8411
    python runner.py --base-url ... --record   # re-record goldens (baseline server)
    python runner.py --server-cmd ... --edition ts-cp   # run a server EDITION's lane

Editions (#620 slice 4): a case may carry an ``editions`` block naming, per server
edition, either a by-design VARIANT (``note`` + optional ``request``/``response``
overrides — the divergence is explicit and reviewed, never masked) or a SKIP with a
reason that must point at a live issue. ``--record --edition X`` records responses
ONLY into cases that already opt in with an ``editions.X`` entry. Without
``--edition`` the base goldens apply unchanged.

Exit codes: 0 all cases pass, 1 any failure, 2 usage/spawn error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MASK = "<normalized>"
READY_TIMEOUT_SECONDS = 60.0
DIFF_LIMIT = 25


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _pointer_escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _mask_keys(value: Any, keys: frozenset[str]) -> Any:
    """Replace the value of every field named in ``keys``, wherever it appears."""
    if isinstance(value, dict):
        return {
            k: MASK if k in keys else _mask_keys(v, keys) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_keys(item, keys) for item in value]
    return value


def _mask_pointer(value: Any, pointer: str) -> Any:
    """Replace the value at one JSON Pointer, if present. `*` matches every
    array index or object key at that position."""
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]

    def apply(node: Any, remaining: list[str]) -> Any:
        if not remaining:
            return MASK
        head, *rest = remaining
        if head == "*":
            if isinstance(node, dict):
                return {k: apply(v, rest) for k, v in node.items()}
            if isinstance(node, list):
                return [apply(v, rest) for v in node]
            return node
        if isinstance(node, dict) and head in node:
            return {**node, head: apply(node[head], rest)}
        if isinstance(node, list) and head.isdigit() and int(head) < len(node):
            index = int(head)
            return [apply(v, rest) if i == index else v for i, v in enumerate(node)]
        return node

    return apply(value, tokens)


def normalize(body: Any, *, global_keys: frozenset[str], pointers: list[str]) -> Any:
    normalized = _mask_keys(body, global_keys)
    for pointer in pointers:
        normalized = _mask_pointer(normalized, pointer)
    return normalized


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def diff(expected: Any, actual: Any, path: str = "") -> list[str]:
    """JSON-Pointer-addressed divergences, same taxonomy as the contract gate
    (typeflux.controlplane.conformance.diff_documents — duplicated
    deliberately: this runner is stdlib-only and cannot import the package;
    taxonomy changes must be mirrored both ways).

    Numbers compare numerically (1.0 == 1), matching the canonical-json
    number-parity rule — JSON.stringify can only emit integral floats as
    integers, so cross-language conformance must not distinguish them.
    """
    where = path or "<root>"
    if _is_number(expected) and _is_number(actual):
        if expected != actual:
            return [f"{where}: value differs (golden={expected!r}, actual={actual!r})"]
        return []
    if type(expected) is not type(actual):
        return [f"{where}: type differs (golden={type(expected).__name__}, actual={type(actual).__name__})"]
    if isinstance(expected, dict):
        lines: list[str] = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}/{_pointer_escape(key)}"
            if key not in actual:
                lines.append(f"{child}: missing from response")
            elif key not in expected:
                lines.append(f"{child}: not in golden")
            else:
                lines.extend(diff(expected[key], actual[key], child))
        return lines
    if isinstance(expected, list):
        lines = []
        for index in range(max(len(expected), len(actual))):
            child = f"{path}/{index}"
            if index >= len(actual):
                lines.append(f"{child}: missing from response")
            elif index >= len(expected):
                lines.append(f"{child}: not in golden")
            else:
                lines.extend(diff(expected[index], actual[index], child))
        return lines
    if expected != actual:
        return [f"{where}: value differs (golden={expected!r}, actual={actual!r})"]
    return []


def request(base_url: str, spec: dict[str, Any]) -> tuple[int, Any]:
    url = base_url.rstrip("/") + spec["path"]
    if spec.get("query"):
        url += "?" + urllib.parse.urlencode(spec["query"])
    body = None
    headers = dict(spec.get("headers", {}))
    if "body" in spec:
        body = json.dumps(spec["body"]).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=spec["method"])
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, _parse_body(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _parse_body(error.read())
    except OSError as error:
        # A dropped connection fails the case as a named divergence instead
        # of crashing the whole run.
        return 0, {"<transport-error>": str(error)}


def _parse_body(raw: bytes) -> Any:
    """Non-JSON bodies (proxy error pages, empty resets) fail the case as a
    named divergence instead of crashing the whole run."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"<non-json-body>": raw.decode("utf-8", errors="replace")[:500]}


def wait_ready(base_url: str, server: subprocess.Popen[bytes] | None = None) -> None:
    # Any sub-500 response means the server is up — under an auth profile the
    # unauthenticated readiness probe legitimately answers 403. 5xx (a warming
    # proxy, a crashing app) keeps polling until the timeout. A spawned server
    # that exits (bad launcher, port already bound) fails fast instead of
    # burning the whole ready window.
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server is not None and server.poll() is not None:
            raise TimeoutError(
                f"server exited with code {server.returncode} before becoming ready"
            )
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/api/v1/meta", timeout=2):
                return
        except urllib.error.HTTPError as error:
            if error.code < 500:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"server at {base_url} not ready within {READY_TIMEOUT_SECONDS}s")


def _spawn(command: str) -> subprocess.Popen[bytes]:
    # New session so teardown can signal the whole process group — with
    # shell=True, terminating only the shell orphans the actual server.
    return subprocess.Popen(command, shell=True, start_new_session=True)


def _teardown(server: subprocess.Popen[bytes]) -> None:
    # PermissionError: macOS reports EPERM for a process group that already
    # died (e.g. the launcher failed at startup) — nothing left to signal.
    try:
        os.killpg(server.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(server.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_cases(
    base_url: str,
    fixtures: Path,
    cases: list[tuple[str, dict[str, Any]]],
    *,
    global_keys: frozenset[str],
    record: bool,
    edition: str | None,
) -> tuple[int, int]:
    """Returns (failures, skips, recorded)."""
    failures = 0
    skips = 0
    recorded = 0
    for case_file, case in cases:
        name = case["name"]
        variant = case.get("editions", {}).get(edition) if edition else None
        if variant is not None and "skip" in variant:
            # An edition scope-out is loud and issue-pointed (validated at load), never silent.
            skips += 1
            print(f"SKIP {name} [{edition}]: {variant['skip']}")
            continue
        spec = variant.get("request", case["request"]) if variant else case["request"]
        # Identity, not truthiness: a variant carrying an explicit EMPTY mask list means
        # "no masking for this edition", never a silent fallback to the base case's masks.
        variant_mask = variant.get("mask") if variant else None
        pointers = variant_mask if variant_mask is not None else case.get("mask", [])
        status, body = request(base_url, spec)
        actual = normalize(body, global_keys=global_keys, pointers=pointers)
        if record:
            if edition:
                if variant is None:
                    # Recording an edition run must not sprout variants for cases that already
                    # pass the shared golden — opting in is a reviewed, per-case decision.
                    print(f"UNCHANGED {name} (no editions.{edition} opt-in)")
                    continue
                variant["response"] = {"status": status, "body": actual}
            else:
                case["response"] = {"status": status, "body": actual}
            (fixtures / case_file).write_text(
                json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            recorded += 1
            print(f"RECORDED {name} ({status})")
            continue
        golden = variant.get("response", case["response"]) if variant else case["response"]
        lines: list[str] = []
        if status != golden["status"]:
            lines.append(f"<status>: golden={golden['status']}, actual={status}")
        expected = normalize(golden["body"], global_keys=global_keys, pointers=pointers)
        lines.extend(diff(expected, actual))
        if lines:
            failures += 1
            print(f"FAIL {name} ({len(lines)} divergence(s))")
            for line in lines[:DIFF_LIMIT]:
                print(f"  {line}")
            if len(lines) > DIFF_LIMIT:
                print(f"  … and {len(lines) - DIFF_LIMIT} more")
        else:
            print(f"PASS {name}" + (f" [{edition}]" if variant else ""))
    return failures, skips, recorded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Base URL of a running control-plane server.")
    parser.add_argument(
        "--server-cmd",
        help=(
            "Command template to spawn the server under test (shell). With auth "
            "profiles in play it must contain a {profile} placeholder; the "
            "runner spawns one server per profile group."
        ),
    )
    parser.add_argument("--port", type=int, default=8411, help="Port for --server-cmd (default 8411).")
    parser.add_argument("--fixtures", default=str(HERE / "fixtures"), help="Fixture directory.")
    parser.add_argument("--only", action="append", default=[], help="Run only the named case(s).")
    parser.add_argument("--record", action="store_true", help="Re-record goldens from the server.")
    parser.add_argument(
        "--profile",
        default=None,
        help="With --base-url: run only this profile's cases (default: open).",
    )
    parser.add_argument(
        "--edition",
        default=None,
        help="Server edition lane: apply the cases' editions.<name> variants/skips.",
    )
    args = parser.parse_args(argv)

    if not args.base_url and not args.server_cmd:
        parser.error("provide --base-url or --server-cmd")

    fixtures = Path(args.fixtures)
    suite = _load(fixtures / "_suite.json")
    global_keys = frozenset(suite["normalize"]["mask_keys"])
    known_profiles = set(suite.get("profiles", {"open": {}}))
    known_editions = set(suite.get("editions", {}))
    if args.edition is not None and args.edition not in known_editions:
        parser.error(
            f"unknown --edition {args.edition!r} (suite editions: {sorted(known_editions)})"
        )
    case_files = suite["cases"]
    if args.only:
        case_files = [c for c in case_files if Path(c).stem in set(args.only)]
        if not case_files:
            print(f"no cases match --only {args.only}", file=sys.stderr)
            return 2

    # Group cases by auth profile; fail closed on unknown profile tags and on
    # malformed editions blocks (unknown edition name, a skip without a live
    # issue pointer, a variant without its mandatory why-note).
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for case_file in case_files:
        case = _load(fixtures / case_file)
        profile = case.get("profile", "open")
        if profile not in known_profiles:
            print(
                f"error: case {case_file} declares unknown profile {profile!r} "
                f"(suite profiles: {sorted(known_profiles)})",
                file=sys.stderr,
            )
            return 2
        for edition_name, variant in case.get("editions", {}).items():
            problem: str | None = None
            if edition_name not in known_editions:
                problem = f"unknown edition {edition_name!r} (suite editions: {sorted(known_editions)})"
            elif "skip" in variant:
                if not re.search(r"#\d+", str(variant["skip"])):
                    problem = "an edition skip must point at a live issue (e.g. '… #568')"
            elif not str(variant.get("note", "")).strip():
                problem = "an edition variant must carry a non-empty 'note' naming why it diverges by design"
            if problem is not None:
                print(f"error: case {case_file}: {problem}", file=sys.stderr)
                return 2
        groups.setdefault(profile, []).append((case_file, case))

    total_failures = 0
    total_cases = 0
    total_skips = 0
    total_recorded = 0

    if args.base_url:
        profile = args.profile or "open"
        if profile not in known_profiles:
            parser.error(f"unknown --profile {profile!r} (suite profiles: {sorted(known_profiles)})")
        selected = groups.get(profile, [])
        if not selected:
            print(f"error: no selected case runs under profile {profile!r}", file=sys.stderr)
            return 2
        skipped = sum(len(v) for k, v in groups.items() if k != profile)
        if skipped and args.record:
            # Recording a subset silently would leave the other profiles'
            # goldens stale while reporting success — refuse.
            print(
                f"error: --record with --base-url would skip {skipped} case(s) in "
                f"other profiles; record via --server-cmd (all profiles) or narrow "
                "the set explicitly with --only",
                file=sys.stderr,
            )
            return 2
        if skipped:
            print(f"note: {skipped} case(s) outside profile {profile!r} skipped (--base-url mode)")
        try:
            wait_ready(args.base_url)
        except TimeoutError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        total_cases = len(selected)
        total_failures, total_skips, total_recorded = _run_cases(
            args.base_url,
            fixtures,
            selected,
            global_keys=global_keys,
            record=args.record,
            edition=args.edition,
        )
    else:
        if set(groups) - {"open"} and "{profile}" not in args.server_cmd:
            parser.error(
                "the selected cases need auth profiles; --server-cmd must "
                "contain a {profile} placeholder"
            )
        if len(groups) > 1 and "{port}" not in args.server_cmd:
            # Each profile gets its own port: a gracefully-draining previous
            # server can hold its socket past child exit, and a same-port
            # respawn then loses the bind race — the readiness probe would be
            # answered by the dying server and the cases would run against
            # the wrong authorizer.
            parser.error(
                "multiple profile groups need one port each; --server-cmd "
                "must contain a {port} placeholder"
            )
        for offset, profile in enumerate(sorted(groups)):
            port = args.port + offset
            base_url = f"http://127.0.0.1:{port}"
            selected = groups[profile]
            print(f"— profile {profile!r} ({len(selected)} case(s)) on port {port}")
            command = args.server_cmd.replace("{profile}", profile).replace(
                "{port}", str(port)
            )
            server = _spawn(command)
            try:
                try:
                    wait_ready(base_url, server)
                except TimeoutError as error:
                    print(f"error: {error}", file=sys.stderr)
                    return 2
                total_cases += len(selected)
                failures, skips, recorded = _run_cases(
                    base_url,
                    fixtures,
                    selected,
                    global_keys=global_keys,
                    record=args.record,
                    edition=args.edition,
                )
                total_failures += failures
                total_skips += skips
                total_recorded += recorded
            finally:
                _teardown(server)

    if args.record:
        # Count actual WRITES — an edition record run leaves non-opted-in cases untouched,
        # and the summary must never claim a write that didn't happen.
        print(f"recorded {total_recorded} of {total_cases} case(s)")
        return 0
    passed = total_cases - total_failures - total_skips
    skipped_note = (
        f" ({total_skips} skipped for edition {args.edition!r})" if total_skips else ""
    )
    print(f"{passed}/{total_cases - total_skips} conformance case(s) pass{skipped_note}")
    return 1 if total_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
