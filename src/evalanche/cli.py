"""Command line entry point.

Exit codes: `0` when no failure was found, `1` when at least one failure
signature was found, and `2` for a usage error or a target error.
"""

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from evalanche import __version__, harness, targets
from evalanche.evaluators import CPPNIX, EVALUATORS, Evaluator

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_ERROR = 2

PROPERTIES = ("valid-input", "merge", "differential")

DEFAULT_FINGERPRINT_DEPTH = 3
SCHEMA_OUTPUT_LIMIT = 2**30


def property_name(value: str) -> str:
    if value in PROPERTIES or re.fullmatch(r"merge\.[A-Za-z0-9_-]+", value):
        return value
    raise argparse.ArgumentTypeError(
        f"unknown property `{value}`, expected one of "
        + ", ".join(f"`{name}`" for name in PROPERTIES)
        + " or `merge.<relation>`"
    )


def nix_bin(value: str) -> tuple[str, Path]:
    name, sep, directory = value.partition("=")
    names = [evaluator.name for evaluator in EVALUATORS]
    if not sep or name not in names or not directory:
        raise argparse.ArgumentTypeError(
            f"expected NAME=DIR with NAME one of {', '.join(names)}, got `{value}`"
        )
    return name, Path(directory).expanduser()


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value}")
    return number


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="evalanche",
        description="Differential fuzzer for the CppNix and Lix evaluators.",
    )
    top.add_argument(
        "--version",
        action="store_true",
        help="print the evalanche version and both evaluator versions",
    )

    evaluator_flags = argparse.ArgumentParser(add_help=False)
    evaluator_flags.add_argument(
        "--nix-bin",
        type=nix_bin,
        action="append",
        default=[],
        metavar="NAME=DIR",
        help="use the evaluator in DIR instead of the pinned one, NAME is cppnix or lix",
    )

    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", help="print JSON")

    run_flags = argparse.ArgumentParser(add_help=False)
    run_flags.add_argument("--time-budget", type=positive_float, metavar="SECONDS")
    run_flags.add_argument("--max-examples", type=positive_int, metavar="N")
    run_flags.add_argument("--jobs", type=positive_int, metavar="N")
    run_flags.add_argument(
        "--property",
        type=property_name,
        action="append",
        default=[],
        metavar="NAME",
        help="check only this property, repeatable",
    )

    schema_flags = argparse.ArgumentParser(add_help=False)
    schema_flags.add_argument(
        "--fingerprint-depth",
        type=positive_int,
        default=DEFAULT_FINGERPRINT_DEPTH,
        metavar="N",
        help="levels the schema exporter looks down to find a repeated type, "
        f"{DEFAULT_FINGERPRINT_DEPTH} by default",
    )

    target_args = argparse.ArgumentParser(add_help=False)
    target_args.add_argument(
        "target",
        nargs="+",
        metavar="TARGET",
        help="FLAKEREF#name, a target name with -f, or one or more module paths",
    )
    target_args.add_argument(
        "-f", "--file", metavar="FILE", help="read the target from a target file"
    )

    commands = top.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser(
        "fuzz",
        parents=[target_args, evaluator_flags, run_flags, schema_flags, json_flag],
        help="fuzz one target",
    )
    commands.add_parser(
        "replay",
        parents=[evaluator_flags, run_flags, json_flag],
        help="replay recorded failures",
    )
    list_parser = commands.add_parser(
        "list", parents=[evaluator_flags, json_flag], help="list the targets"
    )
    list_parser.add_argument(
        "flake", nargs="?", metavar="FLAKEREF", help="the flake to read, `.` by default"
    )
    list_parser.add_argument(
        "-f", "--file", metavar="FILE", help="read the targets from a target file"
    )
    commands.add_parser(
        "schema",
        parents=[target_args, evaluator_flags, schema_flags],
        help="print the option tree of a target as JSON",
    )
    return top


def evaluators(args: argparse.Namespace) -> tuple[Evaluator, ...]:
    overrides = dict(args.nix_bin)
    return tuple(
        dataclasses.replace(evaluator, override=overrides.get(evaluator.name))
        for evaluator in EVALUATORS
    )


def error(kind: str, message: str) -> int:
    print(f"evalanche: {kind}: {message}", file=sys.stderr)
    return EXIT_ERROR


def not_implemented(what: str) -> int:
    return error("not implemented", what)


def print_version() -> int:
    print(f"evalanche {__version__}")
    for evaluator in EVALUATORS:
        try:
            line = evaluator.version()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(f"{evaluator.name}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"{evaluator.name}: {line}")
    return EXIT_OK


def describe_lib(loaded: targets.LoadedTarget) -> str:
    version = loaded.lib_version or "unknown version"
    return f"{loaded.lib_source} ({version})"


def run_list(args: argparse.Namespace) -> int:
    if args.file is not None and args.flake is not None:
        raise targets.UsageError("give either FLAKEREF or `-f FILE`, not both")
    if args.file is not None:
        source = targets.file_source(args.file)
    else:
        ref = args.flake if args.flake is not None else "."
        if "#" in ref:
            ref, _, rest = ref.partition("#")
            if rest:
                raise targets.UsageError("`list` takes a FLAKEREF without a target name")
        source = targets.flake_source(ref)
    entries = targets.list_targets(evaluators(args)[0], source)
    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        for entry in entries:
            print(entry["name"])
    return EXIT_OK


def run_fuzz(args: argparse.Namespace) -> int:
    target = targets.from_arguments(args.target, args.file)
    chosen = evaluators(args)
    loaded = targets.load(chosen[0], target)
    print(
        f"target {loaded.name}: kind {loaded.kind}, lib {describe_lib(loaded)}",
        file=sys.stderr,
    )
    baseline = targets.baseline(chosen, target)
    if not baseline.ok:
        raise targets.TargetError(
            f"baseline evaluation of `{loaded.name}` failed\n{baseline.describe()}"
        )
    print(f"baseline evaluation passed\n{baseline.describe()}", file=sys.stderr)
    return not_implemented("`fuzz` stops after the baseline evaluation")


def evaluator(args: argparse.Namespace, name: str) -> Evaluator:
    return next(e for e in evaluators(args) if e.name == name)


def run_schema(args: argparse.Namespace) -> int:
    """Export the option tree of a target under CppNix and print it."""
    target = targets.from_arguments(args.target, args.file)
    cppnix = evaluator(args, CPPNIX.name)
    targets.load(cppnix, target)
    options = (
        (("extra-experimental-features", "nix-command flakes"),) if target.flakes else ()
    )
    # The export reads the target through the same store and configuration
    # as loading it, so it gets the caller's Nix variables. A large target
    # takes minutes and hundreds of megabytes to export, so the export runs
    # without a timeout or a heap cap.
    env = {
        name: value
        for name, value in os.environ.items()
        if name.startswith("NIX_") and name != "NIX_PATH"
    }
    result = harness.evaluate(
        harness.Evaluation(
            cppnix,
            harness.Expression(targets.schema_expression(target, args.fingerprint_depth)),
            limits=harness.Limits(timeout=None, heap=None),
            options=options,
            env=env,
            json=True,
            output_limit=SCHEMA_OUTPUT_LIMIT,
        )
    )
    if result.exit_code != 0:
        raise targets.TargetError(
            f"schema export of `{target.name}` failed\n{export_error(result)}"
        )
    if result.stdout_truncated:
        raise RuntimeError(
            f"the schema of `{target.name}` is larger than {SCHEMA_OUTPUT_LIMIT} bytes"
        )
    sys.stdout.write(result.stdout)
    return EXIT_OK


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def export_error(result: harness.RawResult) -> str:
    """The error of a failed export with the position closest to it."""
    if result.signal is not None:
        return f"{result.evaluator}: killed by signal {result.signal}"
    error = result.error
    if error is None or error.raw_msg is None:
        return f"{result.evaluator}: exit {result.exit_code}\n{result.stderr.strip()}"
    message = ANSI.sub("", error.raw_msg)
    if message.startswith(targets.ERROR_MARKER):
        message = message[len(targets.ERROR_MARKER) :]
    lines = [f"{result.evaluator}: {message}"]
    # The trace runs from the innermost frame outwards. CppNix 2.36.0 puts
    # the line and column into `file` as well.
    frame = next((frame for frame in error.trace if frame.file is not None), None)
    if frame is not None:
        position = frame.file
        suffix = f":{frame.line}:{frame.column}"
        if frame.line is not None and not position.endswith(suffix):
            position += suffix
        lines.append(f"  at {position}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    top = parser()
    args = top.parse_args(argv)
    if args.version:
        return print_version()
    if args.command is None:
        top.print_usage(sys.stderr)
        return error("usage error", "no command given")
    try:
        if args.command == "list":
            return run_list(args)
        if args.command == "fuzz":
            return run_fuzz(args)
        if args.command == "schema":
            return run_schema(args)
        return not_implemented(f"`{args.command}`")
    except targets.UsageError as exc:
        return error("usage error", str(exc))
    except targets.TargetError as exc:
        return error("target error", str(exc))
    except (RuntimeError, OSError) as exc:
        return error("error", str(exc))
