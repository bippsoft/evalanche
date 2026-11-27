# evalanche

evalanche is a differential fuzzer for Nix evaluators. It generates Nix
expressions, evaluates each one with CppNix and with Lix, and reports where the
two disagree.

The project is at an early stage. The current program prints its own version
and the versions of the two evaluators it runs.

## Running

Both evaluators come from the pinned `nixpkgs` input, so the `nix` on `PATH`
only needs flakes enabled.

```
nix run github:bippsoft/evalanche
```

From a checkout, `nix run` does the same. The development shell provides
Python with `hypothesis` and an editable install of the package.

```
nix develop -c evalanche
```

The evaluator `bin` directories are passed in `EVALANCHE_CPPNIX_BIN` and
`EVALANCHE_LIX_BIN`.

## Systems

`x86_64-linux`, `aarch64-linux` and `aarch64-darwin`. `nixos-unstable` no
longer supports `x86_64-darwin`.
