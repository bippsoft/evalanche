# Leaf types, including the constrained ones that carry their rule only in
# `description` or in `check`.
{ lib, ... }:
let
  t = lib.types;
  opt = type: lib.mkOption { inherit type; };
in
{
  options = {
    bool = opt t.bool;
    boolByOr = opt t.boolByOr;
    int = opt t.int;
    float = opt t.float;
    number = opt t.number;
    str = opt t.str;
    nonEmptyStr = opt t.nonEmptyStr;
    singleLineStr = opt t.singleLineStr;
    passwdEntry = opt (t.passwdEntry t.str);
    strMatching = opt (t.strMatching "[a-z]+");
    separatedString = opt (t.separatedString ":");
    lines = opt t.lines;
    commas = opt t.commas;
    envVar = opt t.envVar;
    enum = opt (
      t.enum [
        "a"
        1
        true
        null
      ]
    );
    enumNonJson = opt (
      t.enum [
        "a"
        [ 1 ]
        { }
      ]
    );
    between = opt (t.ints.between 1 10);
    u8 = opt t.ints.u8;
    s32 = opt t.ints.s32;
    unsigned = opt t.ints.unsigned;
    positive = opt t.ints.positive;
    port = opt t.port;
    numBetween = opt (t.numbers.between 0 1.5);
    numNonnegative = opt t.numbers.nonnegative;
    numPositive = opt t.numbers.positive;
    path = opt t.path;
    pathInStore = opt t.pathInStore;
    externalPath = opt t.externalPath;
    pathWith = opt (t.pathWith { absolute = false; });
    package = opt t.package;
    shellPackage = opt t.shellPackage;
    pkgs = opt t.pkgs;
    fileset = opt t.fileset;
    anything = opt t.anything;
    attrs = opt t.attrs;
    raw = opt t.raw;
    unspecified = opt t.unspecified;
    json = opt t.json;
    toml = opt t.toml;
    luaInline = opt t.luaInline;
    optionType = opt t.optionType;
    addCheck = opt (t.addCheck t.str (lib.hasPrefix "a"));
    custom = opt (
      lib.mkOptionType {
        name = "even";
        check = x: builtins.isInt x && lib.mod x 2 == 0;
      }
    );
    untyped = lib.mkOption { };
    withDefault = lib.mkOption {
      type = t.int;
      default = 1;
    };
    readOnly = lib.mkOption {
      type = t.int;
      default = 1;
      readOnly = true;
    };
    internal = lib.mkOption {
      type = t.int;
      default = 1;
      internal = true;
      visible = false;
    };
    nested.deeper.leaf = opt t.str;
  };
}
