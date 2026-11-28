{ lib, ... }:
{
  options.port = lib.mkOption {
    type = lib.types.port;
    default = 8080;
  };
}
