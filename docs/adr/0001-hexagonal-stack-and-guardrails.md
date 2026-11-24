# ADR-001: Hexagonal Feature-Sliced Stack, Nix/Direnv DevShell, and Fuzz/Perf Git Hooks

- **Status:** Accepted
- **Beads Issue:** `ArchiGram-v2u.1`

## Context

ArchiGram is developed concurrently by specialized subagents working in isolated Orca Git worktrees across multiple Gemini model tiers (`gemini-4-carbon`, `gemini-4-neon`, `gemini-4-argon`, `gemini-4-hydrogen`, `gemini-3.8-flash`). Without hard architectural and testing guardrails, parallel AI branches suffer from cross-module coupling, cyclomatic bloat, and brittle example-based unit tests.

## Decisions

1. **Core Implementation Stack:**
   - **Language & Runtime:** Python 3.12 managed hermetically via Nix (`flake.nix` + `devshell.nix` + `.envrc` via `direnv`) and `uv` (`pyproject.toml` + `uv.lock`).
   - **Graph Engine:** `Qiskit/rustworkx` (`PyDiGraph`) for Rust-backed topological operations (`contract_nodes(check_cycle=True)` raising `DAGWouldCycle`, `remove_node_retain_edges_by_key`, SCC condensation, transitive reduction).
   - **Schemas & CLI/MCP:** `Pydantic` v2, `PyYAML`, `Typer`, `Rich`, and `mcp`.

2. **Hexagonal + Feature-Sliced Architecture (`import-linter`):**
   - **Inward Dependency Layers:** `entrypoints` -> `features` -> `ports` -> `domain`.
   - **Mutual Feature Independence:** All 15 slices under `src/archigram/features/*` satisfy an `import-linter` `independence` contract—zero sibling imports allowed; slices share only `archigram.domain` and `archigram.ports`.
   - **Domain Purity:** `domain` and `ports` are forbidden from importing CLI, MCP, or subprocess modules.

3. **Zero-Unit-Tests Verification Discipline (Git Hooks in `.beads/hooks`):**
   - Static unit tests are rejected by `scripts/check-test-discipline.py`.
   - Every feature slice must ship **Hypothesis property-based fuzz tests** (`tests/fuzz/fuzz_<slice>.py`) and **`pytest-benchmark` performance tests** (`tests/perf/perf_<slice>.py`).
   - Every commit in every worktree executes `scripts/verify.sh` (guardrail check, `ruff` format + `C901<=10` complexity + Bandit static security, strict `pyright`, `lint-imports`, anti-unit-test AST gate, fuzz suite, and perf benchmark suite).
