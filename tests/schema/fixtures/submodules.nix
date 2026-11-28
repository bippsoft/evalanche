# Submodules, freeform types, deferred modules and recursion.
{ lib, ... }:
let
  t = lib.types;
  opt = type: lib.mkOption { inherit type; };

  # Directly recursive.
  tree = t.submodule {
    options.value = opt t.int;
    options.children = lib.mkOption {
      type = t.listOf tree;
      default = [ ];
    };
  };

  # Mutually recursive.
  even = t.submodule { options.next = opt (t.nullOr odd); };
  odd = t.submodule { options.next = opt (t.nullOr even); };

  # Recursive through `attrTag` and a submodule. A recursive `attrTag` with no
  # submodule in the cycle fails with infinite recursion in `lib` itself.
  expr = t.attrTag {
    lit = opt t.int;
    add = opt (t.listOf (t.submodule { options.operand = opt expr; }));
  };

  # Same shape at the first level, different below.
  wrapA = t.submodule { options.inner = opt (t.submodule { options.x = opt t.int; }); };
  wrapB = t.submodule { options.inner = opt (t.submodule { options.x = opt t.str; }); };
in
{
  freeformType = t.attrsOf t.str;

  options = {
    sub = opt (
      t.submodule {
        options.a = lib.mkOption {
          type = t.int;
          default = 1;
        };
        options.inner = opt (t.submodule { options.deep = opt t.bool; });
      }
    );
    named = opt (
      t.attrsOf (
        t.submodule (
          { name, ... }: {
            options.n = lib.mkOption {
              type = t.str;
              default = name;
            };
          }
        )
      )
    );
    listed = opt (t.listOf (t.submodule { options.v = opt t.int; }));
    free = opt (
      t.submodule {
        freeformType = t.attrsOf t.str;
        options.known = opt t.int;
      }
    );
    freeSub = opt (
      t.submodule {
        freeformType = t.attrsOf (t.submodule { options.z = opt t.int; });
      }
    );
    deferred = opt t.deferredModule;
    deferredStatic = opt (
      t.deferredModuleWith {
        staticModules = [ { options.s = opt t.str; } ];
      }
    );
    eitherSub = opt (t.either t.str (t.submodule { options.e = opt t.int; }));
    tree = opt tree;
    even = opt even;
    expr = opt expr;
    wrapA = opt wrapA;
    wrapB = opt wrapB;
  };
}
