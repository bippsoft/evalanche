{
  service = {
    modules = [
      ./modules/port.nix
      ./modules/name.nix
    ];
  };

  greeting = {
    modules = [ ./modules/needs-pkgs.nix ];
    specialArgs.pkgs.greeting = "hello";
  };

  broken.modules = [ ./modules/broken.nix ];
}
