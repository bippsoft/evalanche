# An option type whose `functor` cannot be read, like the type of
# `services.autorandr` at the pinned nixpkgs.
{ lib, ... }:
{
  options.broken = lib.mkOption {
    type = lib.mkOptionType {
      name = "broken";
      functor = throw "broken functor";
    };
  };
}
