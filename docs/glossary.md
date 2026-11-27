# Glossary

Terms used in the evalanche issues and documentation. Each entry links to the
issue comment where the term was settled. Configuration targets and option
selection arrive in M2.

[c15a]: https://github.com/bippsoft/evalanche/issues/15#issuecomment-7225554488
[c15b]: https://github.com/bippsoft/evalanche/issues/15#issuecomment-7225965009
[c17]: https://github.com/bippsoft/evalanche/issues/17#issuecomment-7225931834

## Baseline evaluation

The evaluation of a target with no generated definitions. `fuzz` runs it under
both evaluators before fuzzing, and a baseline that fails under either one is a
target error. Settled in [#15][c15a].

## Built-in table

The mapping from standard constrained type names to their bounds or rules. It
recognises `ints.*`, `port`, `numbers.*`, `nonEmptyListOf`, `nonEmptyStr`,
`singleLineStr`, `passwdEntry` and `shellPackage` by `name`, checked against
`description`. Settled in [#17][c17].

## Configuration target

A target of kind `"configuration"` that extends an existing `evalModules`
result with `extendModules`. It covers `nixosConfigurations.<name>`,
home-manager configurations and flake-parts. This kind arrives in M2. Settled
in [#15][c15b].

## Coverage list

The per-run list of option paths that have no strategy or a high rejection
rate. Each entry carries a path, a type, a reason and a consequence. The list
is printed at the top of every report, and `--strict` turns any entry into a
nonzero exit. Settled in [#17][c17].

## Edge value

A value drawn with extra weight because target code often mishandles it.
An edge value is included only when it exercises the target module's logic,
and scalar draws pick one with weight 1 in 4. Settled in [#17][c17].

## Fixture directory

The run-owned directory that generated path values point into. It holds a
file, a subdirectory, a name with a space, and one path that does not exist.
Settled in [#17][c17].

## Invalid mode

The optional mode that draws near misses at a set rate and expects their
rejection. The rate is 0 by default and 0.05 when the flag is given without a
value. A near miss that evaluates without error produces a finding with the
signature `invalid value accepted`. Settled in [#17][c17].

## Option selection

The `options` and `exclude` prefixes that limit generation and export to part
of the option tree. Each prefix is a list of strings such as
`[ "services" "nginx" ]`, and the `--option` and `--exclude` flags override the
fields. Option selection arrives in M2. Settled in [#15][c15b].

## Path shorthand

Module paths on the command line that stand for a `"modules"` target with the
pinned `lib`. The paths form one target in the order given and also serve as
its name. Settled in [#15][c15a].

## Probe module

A module that evalanche adds with `extendModules` to read values such as
`lib.version` from a configuration. Settled in [#15][c15b].

## Rejection rate

The share of candidates in which a type check rejects the value generated for
an option path. A path whose rate passes a threshold gets a `high-rejection`
entry in the coverage list. Settled in [#17][c17].

## Stub derivation

A `derivation` call that passes `package` checks and is never built. It varies
`name` and `outputs` and includes or omits `meta.mainProgram`, and with
`shellPath` set it also passes `shellPackage`. Settled in [#17][c17].

## Target

The attrset that describes one module evaluation to fuzz. Targets come from
the flake output `evalancheTargets.<name>`, from a target file, or from the
path shorthand. Settled in [#15][c15a].

## Target error

A target that cannot be read or that fails its baseline evaluation. It is
reported with exit code `2` and never as a failure signature. Settled in
[#15][c15a].

## Target file

A Nix file passed with `-f` that holds an attrset of targets by name. Settled
in [#15][c15a].

## Target kind

The value of a target's `kind` attribute, which selects its fields and how it
is evaluated. M1 has only `"modules"`, and M2 adds `"configuration"`. Settled
in [#15][c15a].

## Value tree

The Python representation of a generated value, rendered to Nix text by a
single printer. Its nodes cover `null`, bool, int, float, string, path literal,
list, attrset, lambda and stub derivation. Settled in [#17][c17].
