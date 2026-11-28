"""Read targets from flakes, target files and module paths."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from evalanche import oneshot
from evalanche.evaluators import Evaluator, pinned_lib

TARGETS_NIX = Path(__file__).resolve().parent / "targets.nix"
ERROR_MARKER = "evalanche target error: "


class TargetError(Exception):
    """A target that cannot be read or that fails its baseline evaluation."""


class UsageError(Exception):
    """A command line that names no valid target."""


def nix_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def nix_path(path: Path) -> str:
    """An absolute path literal that survives spaces and other characters."""
    return f"(/. + {nix_string(str(path))})"


def loader() -> str:
    return f"(import {nix_path(TARGETS_NIX)} {{ pinnedLib = import {nix_path(pinned_lib())}; }})"


@dataclass(frozen=True)
class Source:
    """Where targets come from, as a Nix expression and a label."""

    label: str
    flakes: bool


@dataclass(frozen=True)
class FlakeSource(Source):
    ref: str

    def flake(self) -> str:
        return f"(builtins.getFlake {nix_string(self.ref)})"


@dataclass(frozen=True)
class FileSource(Source):
    path: Path


@dataclass(frozen=True)
class Target:
    """A target before loading. `expression` yields the normalised attrset."""

    name: str
    expression: str
    flakes: bool


@dataclass(frozen=True)
class LoadedTarget:
    target: Target
    kind: str
    lib_source: str
    lib_version: str | None

    @property
    def name(self) -> str:
        return self.target.name


def resolve_flake_ref(ref: str) -> str:
    """Make a local flake reference absolute, since `getFlake` needs that."""
    if ref == "":
        ref = "."
    if ref.startswith((".", "/", "~")):
        path = Path(ref).expanduser().resolve()
        if not path.is_dir():
            raise TargetError(f"flake `{ref}` is not a directory")
        if not (path / "flake.nix").is_file():
            raise TargetError(f"flake `{ref}` has no `flake.nix`")
        return str(path)
    return ref


def flake_source(ref: str) -> FlakeSource:
    return FlakeSource(label=ref or ".", flakes=True, ref=resolve_flake_ref(ref))


def file_source(file: str) -> FileSource:
    path = Path(file).expanduser().resolve()
    if not path.is_file():
        raise TargetError(f"target file `{file}` does not exist")
    return FileSource(label=file, flakes=False, path=path)


def from_flake(reference: str) -> Target:
    ref, _, name = reference.partition("#")
    if name == "":
        raise UsageError(f"`{reference}` names no target after `#`")
    if "." in name:
        raise UsageError(
            f"`{reference}` is an attribute path. Only `FLAKEREF#name` for "
            "`evalancheTargets.<name>` is supported."
        )
    source = flake_source(ref)
    expression = (
        f"({loader()}.fromFlake {nix_string(source.label)} "
        f"{source.flake()} {nix_string(name)})"
    )
    return Target(name=reference, expression=expression, flakes=True)


def from_file(file: str, name: str) -> Target:
    source = file_source(file)
    expression = (
        f"({loader()}.fromFile {nix_path(source.path)} {nix_string(name)})"
    )
    return Target(name=name, expression=expression, flakes=False)


def from_paths(paths: list[str]) -> Target:
    resolved = []
    for path in paths:
        absolute = Path(path).expanduser().resolve()
        if not absolute.exists():
            raise TargetError(f"module `{path}` does not exist")
        resolved.append(absolute)
    name = " ".join(paths)
    modules = " ".join(nix_path(path) for path in resolved)
    expression = f"({loader()}.fromPaths {nix_string(name)} [ {modules} ])"
    return Target(name=name, expression=expression, flakes=False)


def from_arguments(positional: list[str], file: str | None) -> Target:
    """Read the positional target forms of `fuzz` and `schema`."""
    if not positional:
        raise UsageError("no target given")
    if file is not None:
        if len(positional) != 1:
            raise UsageError("`-f FILE` takes exactly one target name")
        return from_file(file, positional[0])
    if any("#" in value for value in positional):
        if len(positional) != 1:
            raise UsageError("`FLAKEREF#name` takes no other arguments")
        return from_flake(positional[0])
    return from_paths(positional)


def error_message(stderr: str) -> str:
    """The target error text, or the final error of an evaluator."""
    for line in stderr.splitlines():
        index = line.find(ERROR_MARKER)
        if index >= 0:
            return line[index + len(ERROR_MARKER) :].strip()
    return last_error(stderr)


def last_error(stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if not line.startswith("fetching ")]
    starts = [
        i for i, line in enumerate(lines) if re.match(r"\s*error: \S", line)
    ]
    if not starts:
        return "\n".join(lines).strip()
    return "\n".join(line.strip() for line in lines[starts[-1] :]).strip()


def evaluate_json(evaluator: Evaluator, expression: str, flakes: bool):
    result = oneshot.evaluate(evaluator, expression, json=True, flakes=flakes)
    if not result.ok:
        raise TargetError(error_message(result.stderr))
    return json.loads(result.stdout)


def load(evaluator: Evaluator, target: Target) -> LoadedTarget:
    """Validate a target and read its kind and `lib`."""
    info = evaluate_json(
        evaluator, f"{loader()}.describe {target.expression}", target.flakes
    )
    return LoadedTarget(
        target=target,
        kind=info["kind"],
        lib_source=info["lib"]["source"],
        lib_version=info["lib"]["version"],
    )


def list_targets(evaluator: Evaluator, source: Source) -> list[dict]:
    if isinstance(source, FlakeSource):
        expression = (
            f"{loader()}.listFlake {nix_string(source.label)} {source.flake()}"
        )
    elif isinstance(source, FileSource):
        expression = f"{loader()}.listFile {nix_path(source.path)}"
    else:
        raise TypeError(source)
    return evaluate_json(evaluator, expression, source.flakes)


def evaluation_expression(target: Target, generated: str = "[ ]") -> str:
    """The configuration of `target` with the modules in `generated` added."""
    return f"{loader()}.evaluate {target.expression} {generated}"


def schema_expression(target: Target, fingerprint_depth: int) -> str:
    """The option schema of `target`, exported by `schema.nix`."""
    return (
        f"{loader()}.schema {target.expression} "
        f"{{ fingerprintDepth = {fingerprint_depth}; }}"
    )


@dataclass(frozen=True)
class Baseline:
    results: tuple[oneshot.EvalResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def describe(self) -> str:
        lines = []
        for result in self.results:
            if result.ok:
                lines.append(f"{result.evaluator}: evaluated")
            else:
                message = error_message(result.stderr)
                lines.append(f"{result.evaluator}: exit {result.returncode}")
                lines += [f"  {line}" for line in message.splitlines()]
        return "\n".join(lines)


def baseline(evaluators: tuple[Evaluator, ...], target: Target) -> Baseline:
    """Evaluate `target` with no generated definitions under each evaluator."""
    expression = evaluation_expression(target)
    return Baseline(
        tuple(
            oneshot.evaluate(evaluator, expression, flakes=target.flakes)
            for evaluator in evaluators
        )
    )
