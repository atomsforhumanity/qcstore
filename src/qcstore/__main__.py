"""Maintenance commands: ``python -m qcstore verify URI [--repair]``."""

from __future__ import annotations

import argparse
import sys

from .store import Store
from .verify import verify


def main(argv: list[str] | None = None) -> int:
    """Run a maintenance command; return the exit status (1 if problems were found)."""
    parser = argparse.ArgumentParser(prog="python -m qcstore")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "verify",
        help="check every record: keys, completeness, blob digests, pointers",
    )
    check.add_argument("uri", help="the store: a directory or file:// URI")
    check.add_argument(
        "--repair",
        action="store_true",
        help="rewrite missing or stale current.json files (nothing else changes)",
    )
    args = parser.parse_args(argv)

    report = verify(Store(args.uri), repair=args.repair)
    for path in report.incomplete:
        print(f"incomplete (ignored): {path}")
    for path in report.repaired:
        print(f"repaired: {path}")
    for problem in report.problems:
        print(f"problem: {problem}")
    print(
        f"{report.keys} calculations, {report.runs} runs, "
        f"{len(report.incomplete)} incomplete, {len(report.problems)} problems"
        + (f", {len(report.repaired)} repaired" if report.repaired else "")
    )
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
