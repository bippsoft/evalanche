"""Command line entry point."""

import subprocess
import sys

from evalanche import __version__
from evalanche.evaluators import EVALUATORS


def main() -> int:
    print(f"evalanche {__version__}")
    for evaluator in EVALUATORS:
        try:
            line = evaluator.version()
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"{evaluator.name}: {error}", file=sys.stderr)
            return 1
        print(f"{evaluator.name}: {line}")
    return 0
