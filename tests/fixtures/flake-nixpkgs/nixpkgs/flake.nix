# A stand-in for nixpkgs whose `lib` is told apart by its version.
{
  outputs =
    { self }:
    {
      lib = {
        version = "fixture-input";
        evalModules = { modules, ... }: {
          config.modules = builtins.length modules;
        };
      };
    };
}
