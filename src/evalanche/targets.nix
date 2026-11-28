# Target loading. Every function here returns a normalised target
#
#   { name; kind; modules; lib; libSource; specialArgs; class; }
#
# or throws a message that starts with `errorMarker`, which the Python side
# reports as a target error.
{ pinnedLib }:
let
  errorMarker = "evalanche target error: ";

  fail = name: message: throw "${errorMarker}target `${name}`: ${message}";

  # Arguments that `lib.evalModules` passes to every module itself.
  moduleSystemArgs = [
    "lib"
    "config"
    "options"
    "specialArgs"
    "_class"
    "_prefix"
  ];

  modulesFields = [
    "kind"
    "modules"
    "lib"
    "specialArgs"
    "class"
  ];

  typeName = builtins.typeOf;

  # Validate a target attrset of the `modules` kind. `defaultLib` is used when
  # the target omits `lib`, and `defaultLibSource` names where it came from.
  normalise =
    {
      name,
      raw,
      defaultLib,
      defaultLibSource,
    }:
    let
      kind = raw.kind or "modules";
      unknown = builtins.filter (attr: !builtins.elem attr modulesFields) (builtins.attrNames raw);
      lib = raw.lib or defaultLib;
    in
    if !builtins.isAttrs raw then
      fail name "expected an attrset, got a ${typeName raw}"
    else if !builtins.isString kind then
      fail name "attribute `kind` must be a string, got a ${typeName kind}"
    else if kind != "modules" then
      fail name "attribute `kind` has the unknown value \"${kind}\", expected \"modules\""
    else if unknown != [ ] then
      fail name "unknown attribute `${builtins.head unknown}`, expected one of ${
        builtins.concatStringsSep ", " (map (attr: "`${attr}`") modulesFields)
      }"
    else if !(raw ? modules) then
      fail name "missing attribute `modules`"
    else if !builtins.isList raw.modules then
      fail name "attribute `modules` must be a list, got a ${typeName raw.modules}"
    else if !builtins.isAttrs lib then
      fail name "attribute `lib` must be an attrset, got a ${typeName lib}"
    else if !(lib ? evalModules) then
      fail name "attribute `lib` has no `evalModules`"
    else if !builtins.isAttrs (raw.specialArgs or { }) then
      fail name "attribute `specialArgs` must be an attrset, got a ${typeName raw.specialArgs}"
    else if !(builtins.isNull (raw.class or null) || builtins.isString raw.class) then
      fail name "attribute `class` must be a string or null, got a ${typeName raw.class}"
    else
      {
        inherit name kind lib;
        inherit (raw) modules;
        libSource = if raw ? lib then "target" else defaultLibSource;
        specialArgs = raw.specialArgs or { };
        class = raw.class or null;
      };

  flakeDefaults =
    flake:
    if flake.inputs ? nixpkgs then
      {
        defaultLib = flake.inputs.nixpkgs.lib;
        defaultLibSource = "inputs.nixpkgs";
      }
    else
      {
        defaultLib = pinnedLib;
        defaultLibSource = "pinned";
      };

  pinnedDefaults = {
    defaultLib = pinnedLib;
    defaultLibSource = "pinned";
  };

  flakeTargets =
    ref: flake:
    if flake ? evalancheTargets then
      flake.evalancheTargets
    else
      throw "${errorMarker}flake `${ref}` has no output `evalancheTargets`";

  fileTargets =
    file:
    let
      targets = import file;
    in
    if builtins.isAttrs targets then
      targets
    else
      throw "${errorMarker}target file `${toString file}` must hold an attrset of targets, got a ${typeName targets}";

  pick =
    where: targets: name:
    if targets ? ${name} then
      targets.${name}
    else
      throw "${errorMarker}${where} has no target `${name}`. Available: ${
        if targets == { } then "none" else builtins.concatStringsSep ", " (builtins.attrNames targets)
      }";

  # Shorthand modules get a fallback for every required argument they declare
  # that the module system does not supply. The fallback throws only when the
  # argument is used, and any definition of `_module.args` overrides it.
  missingArgModule =
    path:
    let
      module = import path;
      required = builtins.attrNames (
        builtins.removeAttrs (pinnedLib.filterAttrs (_: hasDefault: !hasDefault) (
          builtins.functionArgs module
        )) moduleSystemArgs
      );
    in
    {
      _file = "evalanche path shorthand";
      _module.args = builtins.listToAttrs (
        map (arg: {
          name = arg;
          value = pinnedLib.mkOptionDefault (
            throw "${errorMarker}module `${toString path}` needs the argument `${arg}`, which the path shorthand does not provide. Write a target file that sets `specialArgs.${arg}` and run it with `-f FILE NAME`."
          );
        }) (if builtins.isFunction module then required else [ ])
      );
    };

  describe = target: {
    inherit (target) name kind;
    lib = {
      source = target.libSource;
      version = target.lib.version or null;
    };
  };
in
rec {
  inherit errorMarker;

  fromFlake =
    ref: flake: name:
    normalise (
      {
        inherit name;
        raw = pick "flake `${ref}`" (flakeTargets ref flake) name;
      }
      // flakeDefaults flake
    );

  fromFile =
    file: name:
    normalise (
      {
        inherit name;
        raw = pick "target file `${toString file}`" (fileTargets file) name;
      }
      // pinnedDefaults
    );

  fromPaths =
    name: paths:
    normalise (
      {
        inherit name;
        raw.modules = paths ++ map missingArgModule paths;
      }
      // pinnedDefaults
    );

  listFlake =
    ref: flake:
    map (name: describe (fromFlake ref flake name)) (builtins.attrNames (flakeTargets ref flake));

  listFile = file: map (name: describe (fromFile file name)) (builtins.attrNames (fileTargets file));

  inherit describe;

  # The evaluation of a target with `generated` appended to its modules. The
  # result is the configuration without `_module`, forced by `--strict`.
  evaluate =
    target: generated:
    let
      result = target.lib.evalModules (
        {
          modules = target.modules ++ generated;
          inherit (target) specialArgs;
        }
        // (if target.class == null then { } else { inherit (target) class; })
      );
    in
    builtins.removeAttrs result.config [ "_module" ];
}
