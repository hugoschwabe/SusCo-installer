"""Command dispatcher for ``python -m susco``."""

from __future__ import annotations

import sys

from . import data
from .analysis import report
from . import preflight
from . import runner

COMMANDS = {
    "run": runner.main,
    "data": data.main,
    "analyze": report.main,
    "preflight": preflight.main,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("usage: python -m susco {run,data,analyze,preflight} [options]")
        return 0
    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        print(f"unknown command: {command}", file=sys.stderr)
        print("usage: python -m susco {run,data,analyze,preflight} [options]", file=sys.stderr)
        return 2
    return int(COMMANDS[command](rest))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
