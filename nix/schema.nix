# Export the option tree of a `modules` target as JSON-safe data.
#
# Every distinct type node is emitted once in `types` under an id and is
# referenced by that id. The id hashes the node unrolled until it repeats an
# ancestor, so a recursive type gets a finite id. A repeat is found with a key
# that looks `fingerprintDepth` levels deep. The walk does not expand a node
# whose id equals the id of one of its ancestors. Two different nodes that
# would get the same id make the export fail.
#
# Usage:
#   nix-instantiate --eval --strict --json nix/schema.nix --arg target 'import ./target.nix'
#
# docs/schema.md describes the output, the ids and the fields left out.
{
  target,
  fingerprintDepth ? 3,
  # Option-path prefixes to export, such as `[ [ "services" "nginx" ] ]`.
  # `null` exports every option. Only the selected subtrees are forced.
  select ? null,
}:
let
  inherit (target) lib;
  inherit (builtins)
    attrNames
    attrValues
    concatMap
    filter
    foldl'
    hashString
    head
    isAttrs
    isList
    mapAttrs
    substring
    toJSON
    typeOf
    ;

  checkTarget =
    let
      known = [
        "kind"
        "modules"
        "lib"
        "specialArgs"
        "class"
      ];
      unknown = filter (n: !(lib.elem n known)) (attrNames target);
    in
    if unknown != [ ] then
      throw "target has unknown attribute `${head unknown}'"
    else if (target.kind or "modules") != "modules" then
      throw "target kind `${target.kind}' is not supported"
    else
      true;

  ev = lib.evalModules {
    inherit (target) modules;
    specialArgs = target.specialArgs or { };
    class = target.class or null;
  };

  isScalar =
    v:
    lib.elem (typeOf v) [
      "null"
      "bool"
      "int"
      "float"
      "string"
    ];

  # A payload value is kept when it is a scalar or a list. A list element that
  # is not a scalar becomes a marker naming its Nix type. Attrsets, functions
  # and types are dropped. Types reach the schema through `nested` instead.
  payloadValue =
    v:
    if isScalar v then
      v
    else
      map (
        x:
        if isScalar x then
          x
        else
          {
            unserialisable = typeOf x;
          }
      ) v;

  payloadOf =
    t:
    let
      p = t.functor.payload or null;
    in
    if !isAttrs p then
      { }
    else
      mapAttrs (_: payloadValue) (
        lib.filterAttrs (
          n: v:
          !(lib.elem n [
            "elemType"
            "modules"
            "staticModules"
          ])
          && (isScalar v || isList v)
        ) p
      );

  # Types whose sub-options are part of the node. For every other type
  # `getSubOptions` only forwards to a nested type.
  hasOptions =
    t:
    lib.elem t.functor.name [
      "submodule"
      "deferredModuleWith"
      "attrTag"
    ];

  # `attrTag` keeps its tags as option declarations in `nestedTypes`, and
  # they are walked as options.
  nestedOf =
    t: if t.functor.name == "attrTag" then { } else lib.filterAttrs (_: lib.isOptionType) t.nestedTypes;

  # Options below a nested attrset of declarations, as a list sorted by path.
  # `_module` and `_freeformOptions` are dropped. The freeform type is
  # reached through `nestedTypes.freeformType`.
  collectOptions =
    path: set:
    concatMap (
      n:
      let
        v = set.${n};
      in
      if
        lib.elem n [
          "_module"
          "_freeformOptions"
        ]
      then
        [ ]
      else if lib.isOption v then
        [
          {
            path = path ++ [ n ];
            option = v;
          }
        ]
      else if isAttrs v then
        collectOptions (path ++ [ n ]) v
      else
        [ ]
    ) (attrNames set);

  subOptionsOf = t: if hasOptions t then collectOptions [ ] (t.getSubOptions [ ]) else [ ];

  # A type can contain itself through `nestedTypes` alone. Its `description`
  # then recurses forever unless some node on the cycle sets one, and an
  # infinite recursion cannot be caught. A node that reaches such a cycle
  # gets `description = null` and `reachesTypeCycle = true`. Cycles are found
  # with a key that leaves `description` out and looks `fingerprintDepth`
  # edges deep.
  structuralKey =
    depth: t:
    hashString "sha256" (toJSON {
      inherit (t) name;
      functor = t.functor.name;
      payload = payloadOf t;
      nested = if depth == 0 then { } else mapAttrs (_: structuralKey (depth - 1)) (nestedOf t);
    });

  reachesTypeCycleFrom =
    ancestors: t:
    let
      key = structuralKey fingerprintDepth t;
    in
    lib.elem key ancestors
    || lib.any (reachesTypeCycleFrom (ancestors ++ [ key ])) (attrValues (nestedOf t));

  reachesTypeCycle = reachesTypeCycleFrom [ ];

  shallow =
    t:
    let
      cyclic = reachesTypeCycle t;
    in
    {
      inherit (t) name;
      description = if cyclic then null else t.description;
      reachesTypeCycle = cyclic;
      descriptionClass = t.descriptionClass or null;
      deprecationMessage = t.deprecationMessage or null;
      functor = t.functor.name;
      payload = payloadOf t;
      hasEmptyValue = t.emptyValue ? value;
    };

  idPrefix = t: head (lib.splitString " " t.name);

  # A bounded key used to spot a node that repeats one of its ancestors. It
  # covers the node's fields, nested types and sub-options down to
  # `fingerprintDepth` levels.
  repeatKey =
    depth: t:
    hashString "sha256" (
      toJSON (
        shallow t
        // {
          nested = if depth == 0 then { } else mapAttrs (_: repeatKey (depth - 1)) (nestedOf t);
          options = map (
            o: if depth == 0 then o.path else optionEntry o (repeatKey (depth - 1) o.option.type)
          ) (subOptionsOf t);
        }
      )
    );

  # The node unrolled until a repeat key, with each repeat written as the
  # number of levels up to the node it repeats.
  unroll =
    stack: t:
    let
      key = repeatKey fingerprintDepth t;
      up = lib.lists.findFirstIndex (k: k == key) null (lib.reverseList stack);
      inner = stack ++ [ key ];
    in
    if up != null then
      { repeats = up + 1; }
    else
      shallow t
      // {
        nested = mapAttrs (_: unroll inner) (nestedOf t);
        options = map (o: optionEntry o (unroll inner o.option.type)) (subOptionsOf t);
      };

  fingerprint = t: "${idPrefix t}-${substring 0 12 (hashString "sha256" (toJSON (unroll [ ] t)))}";

  mergeTables = foldl' (
    acc: t:
    let
      clash = filter (k: acc.${k} != t.${k}) (attrNames (builtins.intersectAttrs acc t));
    in
    if clash != [ ] then
      throw "two different type nodes share the id `${head clash}'. Raise fingerprintDepth. ${toJSON acc.${head clash}} ${toJSON t.${head clash}}"
    else
      acc // t
  ) { };

  optionEntry = o: typeId: {
    inherit (o) path;
    type = typeId;
    hasDefault = o.option ? default;
    readOnly = o.option.readOnly or false;
    internal = o.option.internal or false;
    visible = o.option.visible or true;
  };

  walkOptions =
    ancestors: opts:
    let
      walked = map (o: o // { result = walk ancestors o.option.type; }) opts;
    in
    {
      entries = map (o: optionEntry o o.result.id) walked;
      table = mergeTables (map (o: o.result.table) walked);
    };

  walk =
    ancestors: t:
    let
      id = fingerprint t;
      inner = ancestors ++ [ id ];
      nested = mapAttrs (_: walk inner) (nestedOf t);
      options = walkOptions inner (subOptionsOf t);
      node =
        shallow t
        // {
          nested = mapAttrs (_: r: r.id) nested;
        }
        // lib.optionalAttrs (hasOptions t) { options = options.entries; };
    in
    if lib.elem id ancestors then
      {
        inherit id;
        table = { };
      }
    else
      {
        inherit id;
        table = mergeTables (
          [ { ${id} = node; } ] ++ map (r: r.table) (attrValues nested) ++ [ options.table ]
        );
      };

  # The root option attrset restricted to the selected prefixes. A prefix may
  # stop at an option or at an attrset of options, and it does not cross into
  # a type.
  selectPrefix =
    set: prefix:
    if prefix == [ ] || lib.isOption set then
      set
    else
      let
        n = head prefix;
      in
      if set ? ${n} then { ${n} = selectPrefix set.${n} (lib.tail prefix); } else { };

  selected =
    if select == null then
      ev.options
    else
      lib.foldl' lib.recursiveUpdate { } (map (selectPrefix ev.options) select);

  root = walkOptions [ ] (collectOptions [ ] selected);
  rootFreeform = ev._module.freeformType or null;
  freeform = if rootFreeform == null then null else walk [ ] rootFreeform;
in
assert checkTarget;
{
  schemaVersion = 1;
  inherit fingerprintDepth;
  options = root.entries;
  freeformType = if freeform == null then null else freeform.id;
  types = mergeTables [
    root.table
    (if freeform == null then { } else freeform.table)
  ];
}
