{ lib, ... }:
{
  options.port = lib.mkOption {
    type = lib.types.port;
    default = "not a port";
  };
}
