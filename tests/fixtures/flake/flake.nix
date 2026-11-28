{
  outputs =
    { self }:
    {
      evalancheTargets = {
        service.modules = [ ./port.nix ];
        broken.modules = [ ./broken.nix ];
      };
    };
}
