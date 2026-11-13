{
  description = "Microwatershed DSS - terrain assessment development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
        pythonEnv = python.withPackages (ps: with ps; [
          numpy
          rasterio
          matplotlib
          pytest
        ]);
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.whitebox-tools # native WhiteboxTools binary (no pip download needed)
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            echo "Microwatershed DSS dev shell. Try:"
            echo "  python -m microwatershed.main --lat 17.68 --lon 74.02 --radius 2"
          '';
        };
      });
}
