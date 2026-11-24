#!/usr/bin/env bash
# ArchiGram Mandatory Verification Gate (invoked by Git pre-commit/pre-push & CI).
# Enforces:
#   1. Guardrail immutability in worker worktrees
#   2. Ruff formatting + cyclomatic/structural complexity + Bandit static security analysis
#   3. Strict Pyright static type checking
#   4. Hexagonal + Feature-Sliced Architecture import-linter contracts
#   5. Zero-Unit-Tests AST check (Hypothesis fuzz + pytest-benchmark perf ONLY)
#   6. Property-Based Fuzzing suite (tests/fuzz)
#   7. Performance Benchmark suite (tests/perf)
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

UV_RUN=(uv run --frozen)
if [[ ! -f "$REPO_ROOT/uv.lock" ]]; then
  UV_RUN=(uv run)
fi

echo "==> [1/7] Guardrail integrity check..."
"${UV_RUN[@]}" python scripts/check-guardrails.py

echo "==> [2/7] Ruff formatting, complexity (C901<=10, PLR) & static security (Bandit S)..."
"${UV_RUN[@]}" ruff format --check src tests scripts
"${UV_RUN[@]}" ruff check src tests scripts

echo "==> [3/7] Pyright strict static type checking..."
"${UV_RUN[@]}" pyright

echo "==> [4/7] Hexagonal & Feature-Sliced import-linter contracts..."
"${UV_RUN[@]}" lint-imports

echo "==> [5/7] Anti-unit-test discipline (Fuzz & Perf only)..."
"${UV_RUN[@]}" python scripts/check-test-discipline.py

echo "==> [6/7] Hypothesis property-based fuzzing suite..."
"${UV_RUN[@]}" pytest tests/fuzz --hypothesis-seed=0

echo "==> [7/7] Performance benchmark suite..."
"${UV_RUN[@]}" pytest tests/perf --benchmark-only --benchmark-min-rounds=3 --benchmark-warmup=off

echo "All 7 ArchiGram quality gates PASSED."
