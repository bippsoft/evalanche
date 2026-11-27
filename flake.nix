{
  description = "Differential fuzzer for the CppNix and Lix evaluators";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      project = (lib.importTOML ./pyproject.toml).project;

      # The evaluators under test. Both come from the pinned nixpkgs so that
      # every run uses the same builds, whatever `nix` is on PATH.
      evaluators = pkgs: {
        cppnix = pkgs.nixVersions.latest;
        lix = pkgs.lixPackageSets.latest.lix;
      };

      evaluatorEnv = pkgs: {
        EVALANCHE_CPPNIX_BIN = "${(evaluators pkgs).cppnix}/bin";
        EVALANCHE_LIX_BIN = "${(evaluators pkgs).lix}/bin";
      };

      dependencies = ps: [ ps.hypothesis ];
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          python3Packages = pkgs.python3Packages;
        in
        {
          evalanche = python3Packages.buildPythonApplication {
            pname = project.name;
            inherit (project) version;
            pyproject = true;

            src = lib.fileset.toSource {
              root = ./.;
              fileset = lib.fileset.unions [
                ./pyproject.toml
                ./README.md
                ./src
              ];
            };

            build-system = [ python3Packages.setuptools ];
            dependencies = dependencies python3Packages;

            makeWrapperArgs = lib.flatten (
              lib.mapAttrsToList (name: value: [
                "--set"
                name
                value
              ]) (evaluatorEnv pkgs)
            );

            pythonImportsCheck = [ "evalanche" ];

            passthru = evaluators pkgs;

            meta = {
              inherit (project) description;
              mainProgram = "evalanche";
            };
          };

          default = self.packages.${pkgs.stdenv.hostPlatform.system}.evalanche;

          inherit (evaluators pkgs) cppnix lix;
        }
      );

      devShells = forAllSystems (
        pkgs:
        let
          editable = pkgs.python3Packages.mkPythonEditablePackage {
            pname = project.name;
            inherit (project) version scripts;
            root = "$EVALANCHE_SRC";
            dependencies = dependencies pkgs.python3Packages;
          };
        in
        {
          default = pkgs.mkShell {
            packages = [ (pkgs.python3.withPackages (_: [ editable ])) ];
            env = evaluatorEnv pkgs;
            shellHook = ''
              export EVALANCHE_SRC="$PWD/src"
            '';
          };
        }
      );

      checks = forAllSystems (
        pkgs:
        let
          evalanche = self.packages.${pkgs.stdenv.hostPlatform.system}.evalanche;
        in
        {
          inherit evalanche;

          version = pkgs.runCommand "evalanche-version" { } ''
            ${lib.getExe evalanche} > $out
            grep -Fx 'evalanche ${project.version}' $out
            grep -Fx 'cppnix: nix (Nix) ${(evaluators pkgs).cppnix.version}' $out
            grep -Fx 'lix: nix (Lix, like Nix) ${(evaluators pkgs).lix.version}' $out
          '';
        }
      );

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
