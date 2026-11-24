# ArchiGram

Deterministic graph projection, runtime concurrency trace compaction (`R1–R6` + Series-Parallel poset lowering), and 5-gate semantic/topological linting CLI + MCP server.

## Why We Created ArchiGram

Software teams face three recurring failure modes whenever they try to keep architecture diagrams accurate and readable:

1. **Hand-drawn diagrams silently rot:** Diagrams sketched in Excalidraw, Lucidchart, or hand-edited Markdown have zero connection to the codebase. Within days of refactoring, boxes point to deleted modules, omit newly introduced dependencies, and turn architecture docs into misleading folklore.
2. **Naive auto-generators dump unreadable "hairballs":** Raw AST, dependency, and OpenTelemetry trace visualizers swing to the opposite extreme—dumping 500 classes or 1,000 runtime spans onto a canvas. The result overwhelms human working memory *and* breaks platform rendering limits (such as Gitea's `5,000`-character Mermaid cap).
3. **Unbounded LLM diagramming hallucinates topology and syntax:** Asking an LLM to write raw Mermaid, D2, or PlantUML directly invents non-existent symbols, silently drops critical security or data-flow paths when summarizing, trips over parser version skew, and produces noisy non-deterministic diffs on every run.

**ArchiGram's thesis is that architecture diagrams should be compiled and formally verified, not hand-drawn or freehand-prompted.** ArchiGram separates deterministic compilation (the **Data Plane**) from declarative abstraction intent (the **Control Plane**):

- **Ground-Truth Ingestion (`OBSERVED` Provenance):** Deterministic extractors ingest real symbols, types, edges, and happens-before spans from Python ASTs, SCIP indexes, Graphviz DOT, OOP class hierarchies, OpenTelemetry traces, or live `sys.monitoring` execution—stamping every fact with cryptographic provenance.
- **Declarative Curation (`CurationSpecV1`) Instead of Pixel Pushing:** Humans and AI agents never edit raw diagram syntax. Instead, they author small, version-controlled YAML views (`collapse_partition`, `k_hop_neighborhood`, `bypass_vertices`, `prune_leaves`, and `R1–R6` + Series-Parallel poset trace compaction) that distill large systems into focused `10–25` node views across zoom levels (`L0`–`L3`).
- **5-Gate Mathematical Verification Before Emission:** Every view must pass **Gate 1** (100% symbol grounding—zero hallucinations), **Gate 2** (protected boundary survival), **Gate 3** (transitive reachability, cycle discipline & concurrency happens-before preservation with concrete witness counterexamples), **Gate 4** (cognitive node/edge budgets), and **Gate 5** (headless target syntax validation).
- **Bounded Self-Healing AI Loop + Zero-Cost Deterministic CI:** When an LLM curates views via `archigram propose` or the MCP server, it only proposes `CurationSpecV1` rules inside a bounded 3-turn lint-and-repair loop guided by formal counterexamples. Once committed, `archigram apply --check` enforces **zero documentation drift** in CI and Git hooks in milliseconds with zero LLM calls and byte-identical output across Mermaid (`< 5,000` chars for Gitea/GitHub), D2 (+ animated `.d2.svg`), PlantUML, LikeC4, and Graphviz DOT.

## Architecture Documentation (4 Zoom Levels)

ArchiGram dogfoods itself to generate and lint its own architecture docs (`0` hand-drawn drift, verified by `--sync-md --check` across all 5 Gates). Start at **[docs/architecture/README.md](docs/architecture/README.md)** or jump straight to the abstraction level you need:

| Level | Guide | Focus | Cognitive Scope |
| --- | --- | --- | --- |
| **L0** | [System & Hexagonal Rings](docs/architecture/L0-system-and-rings.md) | Hexagonal rings, Data Plane (CLI) vs. Control Plane (MCP), dependency direction | 9 capability groups |
| **L1** | [Feature-Slice Map](docs/architecture/L1-feature-slices.md) | All 16 strictly independent vertical slices + checklist to add a 17th slice | 16 slices + 4 rings |
| **L2** | [Subsystem Guides](docs/architecture/L2-subsystem-guides.md) | Focused `k_hop_neighborhood` zooms: Domain & Ports (L2a), Reducers (L2b), 5-Gate Linter (L2c), CLI & MCP (L2d) | 9–18 nodes per view |
| **L3** | [Runtime Execution & Self-Healing Traces](docs/architecture/L3-runtime-traces.md) | Live `sys.monitoring` trace of `compile_and_lint_pipeline` & 3-turn `run_propose_loop` compacted via `R1–R6` | 15 participants, 48 events |
| **Gallery** | [Styling Policy & Multi-Target Gallery](docs/architecture/diagram-styling-and-gallery.md) | Dual-mode transparent styling (`domain/style.py`), edge-only semantic colors, flowing D2 SVGs, animated `steps` playback, compact `< 5,000`-char Gitea-ready Mermaid & all modes | All 5 renderers × all modes |
| **UML & Triage** | [UML 2.5 & 3-Stage CLI Curation Showcase](docs/architecture/uml-and-curation-showcase.md) | Program-first AST/trace extraction (`50k` chars) -> CLI-only `curate` + `triage` 3-stage SVGs (`original` -> `triage` -> `curated`) & full 14-type UML 2.5 taxonomy (`package`, `deployment`, `use_case`, `communication`, `activity`, `state`) | 3-stage SVGs + 14 UML types |

## Developer & CLI Quickstart (`justfile`)

Run `just` to list all grouped quality and diagram recipes:

```bash
just              # List all recipes (verify, lint, fmt, fuzz, perf, docs, docs-check, d2, watch-d2, trace, propose)
just docs         # Re-capture live trace + compile L0-L3, 3-stage triage SVGs & UML 2.5 showcase + sync Markdown
just docs-check   # Verify 5-Gate pass + zero Markdown/SVG/triage drift
just d2 docs/architecture/diagrams/l3_runtime_pipeline_steps_d2.d2   # Compile single .d2 -> .d2.svg
just watch-d2     # Live hot-reload D2 diagram preview in browser
just verify       # Run all 8 mandatory ArchiGram quality gates
```

Direct CLI usage (`archigram`):

```bash
# Author or refine CurationSpecV1 views strictly via CLI (zero hand-edited YAML/diagram syntax)
uv run archigram curate init curated.yaml -v core_pkg -t "Core Packages" -r mermaid --variant package
uv run archigram curate prune-leaves curated.yaml -v core_pkg --id prune_stubs -g archigram

# Render 3-stage before/after SVG triage (1_original -> 2_triage -> 3_curated) when raw graphs > 5k chars
uv run archigram triage src/archigram --spec docs/architecture/showcase-structural-views.yaml \
  --view showcase_cli_triage --out-dir docs/architecture/diagrams

# Lint a curation spec against a codebase or trace IR through all 5 Gates
uv run archigram lint src/archigram --spec docs/architecture/diagram-views.yaml

# Compile curated views to Mermaid/D2/PlantUML/LikeC4/DOT (+ companion SVGs) and sync Markdown blocks
uv run archigram apply src/archigram \
  --spec docs/architecture/diagram-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture

# Compare two architectural snapshots for structural regressions (cycles, lost paths, budget drift)
uv run archigram diff base.ir.json head.ir.json --spec docs/architecture/diagram-views.yaml

# Run the bounded 3-turn self-healing proposal -> lint -> repair loop
uv run archigram propose src/archigram --goal "High-level hexagonal overview" --out curated.yaml
```

## Architecture (Hexagonal + Feature-Sliced)

Enforced on every commit by `lint-imports` (`import-linter`), `ruff` (McCabe `C901 <= 10`, structural limits, Bandit security), and strict `pyright`:

- `src/archigram/domain/` — Pure Canonical IR v1, Curation Spec v1, Canonical Dual-Mode Styling (`style.py`), Diagnostics, Concurrency Poset, and Digest value objects (zero framework/I/O dependencies).
- `src/archigram/ports/` — Protocol interfaces (`StructuralIngestorPort`, `TraceIngestorPort`, `StructuralReducerPort`, `TraceCompactorPort`, `LintGatePort`, `SyntaxValidatorPort`, `DiagramEmitterPort`, `DigestGeneratorPort`).
- `src/archigram/features/*` — 16 strictly mutually independent vertical feature slices (zero cross-slice imports).
- `src/archigram/entrypoints/{cli,mcp}/` — Driving adapters (`archigram` CLI Data Plane & MCP Control Plane).

## Quality Gates & Testing Policy

Static example-based unit tests are **forbidden** (`scripts/check-test-discipline.py`). Every slice is verified exclusively via:
- `tests/fuzz/fuzz_*.py` — Property-based & stateful fuzz tests using `hypothesis`.
- `tests/perf/perf_*.py` — Latency & throughput benchmarks using `pytest-benchmark`.

Run all 8 gates locally:
```bash
just verify   # or: ./scripts/verify.sh
```
