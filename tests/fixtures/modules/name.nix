{ lib, config, ... }:
{
  options.name = lib.mkOption {
    type = lib.types.str;
    default = "service-${toString config.port}";
  };
}
