# evalanche

evalanche is a differential fuzzer for Nix evaluators. It generates Nix
expressions, evaluates each one with CppNix and with Lix, and reports where the
two disagree.

The project is at an early stage. The program reads targets, lists them,
prints their option schema and runs the baseline evaluation of a target under
both evaluators. Fuzzing and `replay` are not implemented yet and exit with
code `2`.

The terms used across the project are defined in the [glossary](docs/glossary.md).
The JSON that describes a target's options is documented in [docs/schema.md](docs/schema.md).

## Running

Both evaluators come from the pinned `nixpkgs` input, so the `nix` on `PATH`
only needs flakes enabled.

```
nix run github:bippsoft/evalanche -- --version
```

From a checkout, `nix run . --` does the same. The development shell provides
Python with `hypothesis`, `pytest` and an editable install of the package.

```
nix develop -c evalanche --version
```

## Targets

A target is an attrset `{ kind ? "modules"; modules; lib; specialArgs ? { }; class ? null; }`,
evaluated with `lib.evalModules`. Any other attribute is a target error. A
target that omits `lib` gets `inputs.nixpkgs.lib` when it comes from a flake
with a `nixpkgs` input, and the pinned `lib` otherwise.

The target argument takes three forms.

```
evalanche fuzz .#name                 # evalancheTargets.name of a flake
evalanche fuzz -f targets.nix name    # an attrset of targets in a file
evalanche fuzz a.nix b.nix            # module paths, with the pinned lib
```

`evalanche list` prints the target names of the flake in the current
directory, or of `FLAKEREF` or `-f FILE`. With `--json` it prints an array of
`{ name, kind, lib }`.

`evalanche schema TARGET` prints the option tree of a target as JSON. It takes
the same target forms and `--fingerprint-depth N`, which defaults to 3.

The exit code is `0` when no failure was found, `1` when at least one failure
was found, and `2` for a usage error or a target error. A target whose
baseline evaluation fails under either evaluator is a target error.

## Tests

```
nix flake check
nix develop -c pytest tests
```

`src/evalanche/harness.py` runs each evaluation as its own
`nix-instantiate --eval --strict` process with a heap cap and a timeout.
`tests/test_harness.py` runs its cases under both pinned evaluators.

The evaluator `bin` directories are passed in `EVALANCHE_CPPNIX_BIN` and
`EVALANCHE_LIX_BIN`.

## Systems

`x86_64-linux`, `aarch64-linux` and `aarch64-darwin`. `nixos-unstable` no
longer supports `x86_64-darwin`.
