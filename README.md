# ArchiGram

Deterministic graph projection, runtime concurrency trace compaction (`R1–R6` + Series-Parallel poset lowering), and 5-gate semantic/topological linting CLI + MCP server.

## Architecture Documentation (4 Zoom Levels)

ArchiGram dogfoods itself to generate and lint its own architecture docs (`0` hand-drawn drift, verified by `--sync-md --check` across all 5 Gates). Start at **[docs/architecture/README.md](docs/architecture/README.md)** or jump straight to the abstraction level you need:

| Level | Guide | Focus | Cognitive Scope |
| --- | --- | --- | --- |
| **L0** | [System & Hexagonal Rings](docs/architecture/L0-system-and-rings.md) | Hexagonal rings, Data Plane (CLI) vs. Control Plane (MCP), dependency direction | 9 capability groups |
| **L1** | [Feature-Slice Map](docs/architecture/L1-feature-slices.md) | All 16 strictly independent vertical slices + checklist to add a 17th slice | 16 slices + 4 rings |
| **L2** | [Subsystem Guides](docs/architecture/L2-subsystem-guides.md) | Focused `k_hop_neighborhood` zooms: Domain & Ports (L2a), Reducers (L2b), 5-Gate Linter (L2c), CLI & MCP (L2d) | 9–18 nodes per view |
| **L3** | [Runtime Execution & Self-Healing Traces](docs/architecture/L3-runtime-traces.md) | Live `sys.monitoring` trace of `compile_and_lint_pipeline` & 3-turn `run_propose_loop` compacted via `R1–R6` | 14 participants, 48 events |

## CLI Quickstart

```bash
# Lint a curation spec against a codebase or trace IR through all 5 Gates
uv run archigram lint src/archigram --spec docs/architecture/diagram-views.yaml

# Compile curated views to Mermaid/D2/PlantUML/LikeC4 and sync inline Markdown blocks
uv run archigram apply src/archigram \
  --spec docs/architecture/diagram-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture

# CI drift check (fails non-zero if any Gate fails or any Markdown diagram block drifted)
uv run archigram apply src/archigram \
  --spec docs/architecture/diagram-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture \
  --check

# Compare two architectural snapshots for structural regressions (cycles, lost paths, budget drift)
uv run archigram diff base.ir.json head.ir.json --spec docs/architecture/diagram-views.yaml

# Run the bounded 3-turn self-healing proposal -> lint -> repair loop
uv run archigram propose src/archigram --goal "High-level hexagonal overview" --out curated.yaml
```

## Architecture (Hexagonal + Feature-Sliced)

Enforced on every commit by `lint-imports` (`import-linter`), `ruff` (McCabe `C901 <= 10`, structural limits, Bandit security), and strict `pyright`:

- `src/archigram/domain/` — Pure Canonical IR v1, Curation Spec v1, Diagnostics, Concurrency Poset, and Digest value objects (zero framework/I/O dependencies).
- `src/archigram/ports/` — Protocol interfaces (`StructuralIngestorPort`, `TraceIngestorPort`, `StructuralReducerPort`, `TraceCompactorPort`, `LintGatePort`, `SyntaxValidatorPort`, `DiagramEmitterPort`, `DigestGeneratorPort`).
- `src/archigram/features/*` — 16 strictly mutually independent vertical feature slices (zero cross-slice imports).
- `src/archigram/entrypoints/{cli,mcp}/` — Driving adapters (`archigram` CLI Data Plane & MCP Control Plane).

## Quality Gates & Testing Policy

Static example-based unit tests are **forbidden** (`scripts/check-test-discipline.py`). Every slice is verified exclusively via:
- `tests/fuzz/fuzz_*.py` — Property-based & stateful fuzz tests using `hypothesis`.
- `tests/perf/perf_*.py` — Latency & throughput benchmarks using `pytest-benchmark`.

Run all 7 gates locally:
```bash
./scripts/verify.sh
```
