"""Run one evaluation as a single evaluator process.

This is a stand-in for the evaluation harness. It has no timeout, heap cap or
process group handling, and callers use only `evaluate` and `EvalResult` so
that the harness can replace it.
"""

import os
import subprocess
from dataclasses import dataclass

from evalanche.evaluators import Evaluator

FLAKE_FEATURES = ["--extra-experimental-features", "nix-command flakes"]


@dataclass(frozen=True)
class EvalResult:
    evaluator: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def evaluate(
    evaluator: Evaluator,
    expression: str,
    *,
    json: bool = False,
    flakes: bool = False,
) -> EvalResult:
    """Evaluate `expression` strictly with `nix-instantiate --eval`."""
    command = [str(evaluator.bin_dir() / "nix-instantiate"), "--eval", "--strict"]
    if json:
        command.append("--json")
    if flakes:
        command += FLAKE_FEATURES
    command += ["-E", expression]
    env = dict(os.environ, NO_COLOR="1", NIX_PATH="")
    result = subprocess.run(
        command, capture_output=True, text=True, env=env, check=False
    )
    return EvalResult(evaluator.name, result.returncode, result.stdout, result.stderr)
