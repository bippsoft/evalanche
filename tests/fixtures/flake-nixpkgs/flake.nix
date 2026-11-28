{
  inputs.nixpkgs.url = "path:./nixpkgs";

  outputs =
    { self, nixpkgs }:
    {
      evalancheTargets.service.modules = [ ];
    };
}
