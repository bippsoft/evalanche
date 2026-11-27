# A small lib.evalModules target. The prototype adds one generated module
# per evaluation and forces every option value except _module.
{ lib, config, ... }:
let
  inherit (lib) mkOption types;
in
{
  options = {
    name = mkOption {
      type = types.str;
      default = "svc";
    };
    port = mkOption {
      type = types.port;
      default = 80;
    };
    tags = mkOption {
      type = types.listOf types.str;
      default = [ ];
    };
    users = mkOption {
      default = { };
      type = types.attrsOf (
        types.submodule {
          options = {
            uid = mkOption { type = types.ints.unsigned; };
            shell = mkOption {
              type = types.nullOr types.str;
              default = null;
            };
          };
        }
      );
    };
    source = mkOption {
      type = types.nullOr types.path;
      default = null;
    };
    summary = mkOption {
      type = types.str;
      readOnly = true;
    };
  };

  config.summary = lib.concatStringsSep ":" [
    config.name
    (toString config.port)
    (lib.concatStringsSep "," config.tags)
    (toString (lib.length (lib.attrNames config.users)))
  ];
}
