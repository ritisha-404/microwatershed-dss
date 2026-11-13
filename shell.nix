# Non-flake entry point: `nix-shell` in the project root.
{ pkgs ? import <nixpkgs> { } }:

let
  pythonEnv = pkgs.python313.withPackages (ps: with ps; [
    numpy
    rasterio
    matplotlib
    pytest
  ]);
in
pkgs.mkShell {
  packages = [
    pythonEnv
    pkgs.whitebox-tools
  ];
  shellHook = ''
    export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
  '';
}
