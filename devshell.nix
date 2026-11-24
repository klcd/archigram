{ pkgs }:

pkgs.mkShell {
  packages = with pkgs; [
    python312
    uv
    ruff
    pyright
    graphviz
    d2
    nodejs_22
    jq
    just
  ];

  shellHook = ''
    export ARCHIGRAM_ROOT="$PWD"
    export UV_PYTHON="${pkgs.python312}/bin/python3"
    if [ -d "$PWD/.venv/bin" ]; then
      export PATH="$PWD/.venv/bin:$PATH"
    fi
  '';
}
