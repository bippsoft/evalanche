# Type constructors over leaf types.
{ lib, ... }:
let
  t = lib.types;
  opt = type: lib.mkOption { inherit type; };

  # Recursive through `nestedTypes` alone, as in the settings types of
  # `pkgs.formats`. The fixed `description` keeps `lib` from looping.
  valueType =
    t.nullOr (
      t.oneOf [
        t.bool
        t.int
        t.str
        (t.attrsOf valueType)
        (t.listOf valueType)
      ]
    )
    // {
      description = "settings value";
    };
in
{
  options = {
    listOf = opt (t.listOf t.int);
    nonEmptyListOf = opt (t.nonEmptyListOf t.int);
    attrsOf = opt (t.attrsOf t.int);
    lazyAttrsOf = opt (t.lazyAttrsOf t.int);
    attrsWith = opt (
      t.attrsWith {
        elemType = t.str;
        placeholder = "user";
      }
    );
    nullOr = opt (t.nullOr t.int);
    either = opt (t.either t.int t.str);
    oneOf = opt (
      t.oneOf [
        t.int
        t.str
        t.bool
      ]
    );
    coercedTo = opt (t.coercedTo t.int toString t.str);
    functionTo = opt (t.functionTo t.int);
    unique = opt (t.unique { message = "only once"; } t.int);
    uniq = opt (t.uniq t.int);
    nestedLists = opt (t.listOf (t.listOf (t.nullOr t.str)));
    sharedA = opt (t.listOf t.int);
    sharedB = opt (t.listOf t.int);
    settings = opt (t.attrsOf valueType);
    attrTag = opt (
      t.attrTag {
        a = opt t.int;
        b = opt t.str;
      }
    );
  };
}
