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
#   8. Self-hosted ArchiGram 5-Gate & zero-drift documentation check
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

UV_RUN=(uv run --frozen)
if [[ ! -f "$REPO_ROOT/uv.lock" ]]; then
  UV_RUN=(uv run)
fi

echo "==> [1/8] Guardrail integrity check..."
"${UV_RUN[@]}" python scripts/check-guardrails.py

echo "==> [2/8] Ruff formatting, complexity (C901<=10, PLR) & static security (Bandit S)..."
"${UV_RUN[@]}" ruff format --check src tests scripts
"${UV_RUN[@]}" ruff check src tests scripts

echo "==> [3/8] Pyright strict static type checking..."
"${UV_RUN[@]}" pyright

echo "==> [4/8] Hexagonal & Feature-Sliced import-linter contracts..."
"${UV_RUN[@]}" lint-imports

echo "==> [5/8] Anti-unit-test discipline (Fuzz & Perf only)..."
"${UV_RUN[@]}" python scripts/check-test-discipline.py

echo "==> [6/8] Hypothesis property-based fuzzing suite..."
"${UV_RUN[@]}" pytest tests/fuzz --hypothesis-seed=0

echo "==> [7/8] Performance benchmark suite..."
"${UV_RUN[@]}" pytest tests/perf --benchmark-only --benchmark-min-rounds=3 --benchmark-warmup=off

echo "==> [8/8] Self-hosted ArchiGram 5-Gate & zero-drift documentation check..."
"${UV_RUN[@]}" archigram apply src/archigram \
  --spec docs/architecture/diagram-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture \
  --check
"${UV_RUN[@]}" archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
  --spec docs/architecture/trace-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture \
  --check

echo "All 8 ArchiGram quality gates PASSED."
