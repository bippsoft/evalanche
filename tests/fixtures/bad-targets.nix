{
  prefixed = {
    modules = [ ./modules/port.nix ];
    prefix = [ "services" ];
  };

  configuration = {
    kind = "configuration";
    modules = [ ./modules/port.nix ];
  };

  noModules.specialArgs = { };
}
