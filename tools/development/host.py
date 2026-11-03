"""Local host observations, never evidence that a failed command passed."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

GAP_SECONDS = 5
MAX_GAPS = 8
UNCERTAINTY = (
    "Sampling gaps or clock divergence may indicate suspension, CPU contention, "
    "debugger pauses or clock adjustment; they do not establish the cause or "
    "change command/test outcomes. No observed gap does not exclude suspension."
)


class LocalPowerControlError(ValueError):
    """Invalid use of an explicitly local verification option."""


class ClockGaps:
    """Bound memory while sampling independently of command stdout and deadlines."""

    def __init__(self):
        self.available = True
        self.previous = None
        self.count = 0
        self.gaps = []

    def sample(self, wall, monotonic):
        previous, self.previous = self.previous, (wall, monotonic)
        if previous is None:
            return
        wall_delta, monotonic_delta = wall - previous[0], monotonic - previous[1]
        if (
            monotonic_delta >= GAP_SECONDS
            or wall_delta >= GAP_SECONDS
            or abs(wall_delta - monotonic_delta) >= GAP_SECONDS
        ):
            self.count += 1
            if len(self.gaps) < MAX_GAPS:
                self.gaps.append(
                    {
                        "wallSeconds": round(wall_delta, 3),
                        "monotonicSeconds": round(monotonic_delta, 3),
                    }
                )

    def result(self):
        return {
            "available": self.available,
            "possibleHostInterruption": bool(self.count),
            "gapCount": self.count,
            "samples": list(self.gaps),
            "omittedSamples": max(0, self.count - len(self.gaps)),
            "thresholdSeconds": GAP_SECONDS,
            "uncertainty": UNCERTAINTY,
        }


@contextmanager
def observe_host():
    gaps, done = ClockGaps(), threading.Event()

    def sample():
        try:
            gaps.sample(time.time(), time.monotonic())
        except Exception:
            # Diagnostic collection must degrade explicitly rather than silently
            # losing the watcher or preventing the actual command from running.
            gaps.available = False
            done.set()

    def watch():
        while not done.wait(1):
            sample()

    sample()
    thread = threading.Thread(target=watch, name="typeflux-host-observer", daemon=True)
    started = False
    if gaps.available:
        try:
            thread.start()
            started = True
        except RuntimeError:
            gaps.available = False
    try:
        yield gaps
    finally:
        done.set()
        if started:
            thread.join()
        if gaps.available:
            sample()


@contextmanager
def keep_awake(requested):
    """Optional macOS idle-sleep prevention for this invocation, not power settings."""
    record = {"requested": requested, "status": "disabled"}
    process = None
    if requested:
        if any(
            os.environ.get(key, "").strip().lower() not in {"", "0", "false"}
            for key in ("CI", "GITHUB_ACTIONS")
        ):
            raise LocalPowerControlError("--keep-awake is a local option and is unavailable in CI")
        if platform.system() != "Darwin":
            record["status"] = "unsupported-platform"
        else:
            try:
                process = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                record["status"] = "started"
            except OSError:
                record["status"] = "unavailable"
        print(f"Local keep-awake: {record['status']} (idle sleep only)", file=sys.stderr)
    try:
        yield record
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            except OSError:
                print(
                    "Local keep-awake helper cleanup unavailable; it also watches this PID",
                    file=sys.stderr,
                )
