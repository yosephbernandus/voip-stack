{
  description = "voip-stack: a VoIP stack built from scratch in Python";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        pythonPackages = pkgs.python312Packages;
      in
      {
        devShells.default = pkgs.mkShell {
          name = "voip-stack";

          buildInputs = [
            pkgs.python312
            pythonPackages.pydantic
            pythonPackages.websockets
            pythonPackages.pytest
            pythonPackages.pip
          ];

          shellHook = ''
            echo "voip-stack dev shell"
            echo "  Python: $(python --version)"
            echo ""
            echo "Commands:"
            echo "  pytest              run the test suite"
            echo "  python -m voip      start the signaling server on :8080"

            # Make the voip package importable from the project root.
            export PYTHONPATH="$PWD:$PYTHONPATH"
          '';
        };
      }
    );
}
