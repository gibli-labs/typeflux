"""Explicitly freeze, execute or summarize the authorized study; never run paid calls on import."""

import argparse
import json
from pathlib import Path

from .analysis import summarize
from .experiment import freeze, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    prep = sub.add_parser("freeze")
    prep.add_argument("--output", required=True, type=Path)
    prep.add_argument("--image", required=True)
    prep.add_argument("--cli", type=Path, default=Path("/opt/homebrew/bin/claude"))
    prep.add_argument("--controls", required=True, type=Path)
    prep.add_argument("--successor-controls", required=True, type=Path)
    prep.add_argument("--guard-report", required=True, type=Path)
    execution = sub.add_parser("run")
    execution.add_argument("--output", required=True, type=Path)
    execution.add_argument("--exercise-after-response-interruption", action="store_true")
    report = sub.add_parser("analyze")
    report.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if args.phase == "freeze":
        plan = freeze(
            root,
            args.output,
            args.image,
            args.cli,
            args.controls,
            args.guard_report,
            args.successor_controls,
        )
        print(
            json.dumps(
                {
                    "phase": "frozen, no paid calls",
                    "model": plan["model"],
                    "tasks": len(plan["tasks"]),
                }
            )
        )
    elif args.phase == "run":
        run(root, args.output, interrupt=args.exercise_after_response_interruption)
    else:
        print(json.dumps(summarize(root, args.output), indent=2))


if __name__ == "__main__":
    main()
