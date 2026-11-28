{ lib, pkgs, ... }:
{
  options.greeting = lib.mkOption {
    type = lib.types.str;
    default = pkgs.greeting;
  };
}
