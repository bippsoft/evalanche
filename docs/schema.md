# Option schema

`src/evalanche/schema.nix` turns the option tree of a `modules` target into
JSON. The generator reads this JSON to choose a strategy for each option, and
`evalanche schema TARGET` prints it. The exporter was settled in #13 against
nixpkgs `2786a2a` and gives byte-identical output on CppNix 2.36.0 and Lix
2.95.4.

## Running it

`evalanche schema` takes any of the target forms from #15 and prints the JSON
on stdout.

```
evalanche schema module.nix
evalanche schema -f targets.nix name
evalanche schema --fingerprint-depth 4 .#name
```

It loads the target with `src/evalanche/targets.nix` and runs the exporter
under CppNix through the evaluation harness, with no timeout and no heap cap.
A target that cannot be loaded, or whose export fails, is a target error and
exits with code `2`. The export does not evaluate `config`, so a target whose
baseline evaluation fails can still export.

The exporter can also be run directly. It takes the target attrset from #15
and two optional arguments.

```
nix-instantiate --eval --strict --json -E \
  'import ./src/evalanche/schema.nix { target = { modules = [ ./module.nix ]; lib = import <nixpkgs/lib>; }; }'
```

- **`fingerprintDepth`**, default `3`: how many levels the repeat key looks
  down. It is described under [Ids](#ids), and `--fingerprint-depth` sets it.
- **`select`**, default `null`: a list of option-path prefixes such as
  `[ [ "services" "nginx" ] ]`. Only the selected subtrees are forced. A prefix
  stops at an option or at an attrset of options and does not reach into a
  type. Option selection in #26 builds on this, and the command line has no
  flag for it yet.

`tests/schema/run.sh` exports every fixture in `tests/schema/fixtures/` under
both pinned evaluators, compares the two outputs, and compares each with
`tests/schema/expected/`. `tests/schema/run.sh --update` rewrites the expected
files. The flake check `checks.<system>.schema` runs the same comparison.

## Shape

The top level holds `schemaVersion`, `fingerprintDepth`, `options`,
`freeformType` and `types`. `options` is the list of root options sorted by
path. `freeformType` is the id of the root freeform type or `null`. `types`
maps each id to a type node.

An option entry has exactly these fields.

| Field | Meaning |
|---|---|
| `path` | The option path relative to the enclosing node, with `*` and `<name>` placeholders. |
| `type` | The id of the option's type node. |
| `hasDefault` | Whether the declaration sets `default`. |
| `readOnly`, `internal`, `visible` | The declaration's values, defaulting to `false`, `false` and `true`. |

A type node carries `name`, `description`, `descriptionClass`,
`deprecationMessage`, `functor`, `payload`, `hasEmptyValue`,
`reachesTypeCycle` and `nested`. `functor` is `functor.name`. `payload` keeps
the scalars and lists of scalars from `functor.payload`, while `elemType`,
`modules` and `staticModules` are dropped since types reach the schema through
`nested`. An enum value that is not a JSON value becomes
`{ "unserialisable": "<typeOf>" }`. `hasEmptyValue` tells `nonEmptyListOf`
apart from `listOf` without forcing the empty value.

`nested` maps each edge of `nestedTypes` to an id. `either` keeps its `left`
and `right` edges as `lib` builds them, so `oneOf [ a b c ]` appears as
`either (either a b) c`, and the generator flattens the chain as #17 describes.
A freeform submodule has `nested.freeformType`.

Nodes for `submodule`, `deferredModuleWith` and `attrTag` also carry
`options`, a list of option entries read from `getSubOptions [ ]`. The internal
`_module` options and the `_freeformOptions` key are dropped. For every other
type `getSubOptions` only forwards to a nested type, so the options are found
through `nested`.

## Ids

An id is the type `name` up to its first space, a hyphen, and 12 hex digits of
a SHA-256, as in `listOf-a5192a525c7f`. The hash covers the node unrolled until
it repeats one of its ancestors, with each repeat written as the number of
levels up to the node it repeats. An id therefore depends only on the type and
not on where it appears, which keeps it stable across runs and evaluators and
makes identical nodes share one entry. The generator keys its deferred
strategies on these ids.

A repeat is found with a key that covers the node's fields, nested types and
sub-options `fingerprintDepth` levels deep. The walk does not expand a node
whose id equals the id of one of its ancestors, and this is what ends the walk
on recursive submodules. Two different nodes that would receive the same id
make the export fail with both nodes in the message, and raising the depth is
the remedy.

On the pinned NixOS option tree, without `services.autorandr`,
`specialisation` and `virtualisation`, the export holds 17,005 options and
3,737 types. Depth 3 takes 49 seconds and depth 4 takes 83 seconds with the
same output, while depth 2 takes 28 seconds and wrongly marks the settings type
of `services.pgadmin` as recursive. The default is therefore 3. The
depth is the flag `--fingerprint-depth` on both `schema` and `fuzz`, since
`fuzz` exports the schema internally and has to produce the same ids.

## Descriptions on recursive types

A type can contain itself through `nestedTypes` alone. The settings types of
`pkgs.formats` and `lib.types.json` do this and set a fixed `description` on
one node of the cycle. When no node on the cycle sets one, reading
`description` recurses forever inside `lib`. `services.forgejo-runner` at the
pinned revision has such a type, and an infinite recursion cannot be caught by
`builtins.tryEval`.

The exporter first looks for these cycles with a key that leaves
`description` out. A node that reaches a cycle gets `description = null` and
`reachesTypeCycle = true`, and every other node keeps its description. The
types in the built-in table from #17, which are `ints.*`, `port`, `numbers.*`,
`nonEmptyListOf`, `nonEmptyStr`, `singleLineStr`, `passwdEntry` and
`shellPackage`, are not recursive, so they always keep the description that
the table is checked against. The cost falls on the recursive types, since
`json`, `toml` and the `pkgs.formats` types lose texts such as `"JSON value"`.
Recovering them would need `builtins.unsafeGetAttrPos`, which the exporter does
not use.

## Fields left out

Each field of an option entry is read from every option, so a field belongs
there only when the generator or the report needs it and reading it is safe.

- **Default value**: reading it forces the default expression, which in NixOS
  typically reads `config` or `pkgs`. A function default aborts `toJSON`
  without a catchable error, and a path default is copied into the store. The
  generator uses a default by leaving the option undefined, which needs only
  `hasDefault`.
- **`defaultText` and `example`**: documentation only, and `example` can hold
  functions or derivations.
- **Option `description`**: Markdown for the manual and the largest field in a
  NixOS tree. Neither the generator nor the report reads it.
- **`declarations`**: store or checkout paths, which would make the same
  target export differently on two machines. A report looks up the
  declarations of a failing path when it needs them.
- **`isDefined`, `highestPrio` and `definitionsWithLocations`**: these force
  the target's own definitions and therefore `config`. The baseline evaluation
  already covers that.
- **`apply`**: a function. An `apply` that throws on a generated value is a
  target bug that the fuzzer finds without a flag in the schema.

## Errors the exporter cannot catch

A type whose metadata fails with an error that `tryEval` cannot catch aborts
the whole export. At the pinned revision `services.autorandr` calls
`lib.types.elemTypeFunctor`, which `lib.types` does not export, so reading its
`functor` fails with `attribute 'elemTypeFunctor' missing`. `specialisation`
and `virtualisation` embed the full NixOS module list and reach the same type.
For a `modules` target this is a target error. Option selection in #26 has to
let `exclude` match paths inside types, such as
`specialisation.<name>.configuration.services.autorandr`, since root prefixes
cannot reach them.

A recursive `attrTag` with no submodule on the cycle fails with infinite
recursion in `lib` itself on plain evaluation, so no working target contains
one.
