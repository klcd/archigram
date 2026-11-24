# ArchiGram

Deterministic graph projection, runtime concurrency trace compaction (`R1–R6` + Series-Parallel poset lowering), and 5-gate semantic/topological linting CLI + MCP server.

## Architecture (Hexagonal + Feature-Sliced)

Enforced on every commit by `lint-imports` (`import-linter`), `ruff` (McCabe `C901 <= 10`, structural limits, Bandit security), and strict `pyright`:

- `src/archigram/domain/` — Pure Canonical IR v1, Curation Spec v1, Diagnostics, and Digest value objects (zero framework/I/O dependencies).
- `src/archigram/ports/` — Protocol interfaces (`IngestorPort`, `StructuralReducerPort`, `TraceCompactorPort`, `LintGatePort`, `DiagramEmitterPort`, `DigestGeneratorPort`).
- `src/archigram/features/*` — 15 strictly mutually independent vertical feature slices (zero cross-slice imports).
- `src/archigram/entrypoints/{cli,mcp}/` — Driving adapters (`archigram` CLI Data Plane & MCP Control Plane).

## Quality Gates & Testing Policy

Static example-based unit tests are **forbidden** (`scripts/check-test-discipline.py`). Every slice is verified exclusively via:
- `tests/fuzz/fuzz_*.py` — Property-based & stateful fuzz tests using `hypothesis`.
- `tests/perf/perf_*.py` — Latency & throughput benchmarks using `pytest-benchmark`.

Run all 7 gates locally:
```bash
./scripts/verify.sh
```
