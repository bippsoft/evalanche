"""Locate the pinned evaluators and query them."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Evaluator:
    name: str
    env_var: str
    override: Path | None = None

    def bin_dir(self) -> Path:
        if self.override is not None:
            return self.override
        value = os.environ.get(self.env_var)
        if not value:
            raise RuntimeError(
                f"{self.env_var} is not set. Run evalanche through `nix run` "
                "or inside `nix develop`."
            )
        return Path(value)

    def version(self) -> str:
        result = subprocess.run(
            [str(self.bin_dir() / "nix"), "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.splitlines()[0].strip()


CPPNIX = Evaluator("cppnix", "EVALANCHE_CPPNIX_BIN")
LIX = Evaluator("lix", "EVALANCHE_LIX_BIN")
EVALUATORS = (CPPNIX, LIX)


def pinned_lib() -> Path:
    """The `lib` directory of the pinned nixpkgs."""
    value = os.environ.get("EVALANCHE_PINNED_LIB")
    if not value:
        raise RuntimeError(
            "EVALANCHE_PINNED_LIB is not set. Run evalanche through `nix run` "
            "or inside `nix develop`."
        )
    return Path(value)
