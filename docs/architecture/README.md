# ArchiGram Architecture Documentation

> **100% Self-Hosted, Zero Hand-Drawn Drift:** Every diagram in this folder is compiled directly from live `src/archigram` AST symbols and a reproducible `sys.monitoring` runtime trace snapshot using ArchiGram's own `CurationSpecV1` specs, verified by all **5 ArchiGram Linter Gates**, compacted below Gitea's `5,000`-character Mermaid limit (`1,413–3,866` chars), and synced into Markdown via `archigram apply --sync-md docs/architecture`.

## Why ArchiGram Exists

Architecture diagrams traditionally fail in one of three ways: **hand-drawn boxes silently drift** as code evolves, **raw AST/trace dumpers emit 500-node hairballs** that exceed human working memory and platform limits (such as Gitea's `5,000`-character Mermaid cap), and **freehand LLM prompts hallucinate symbols**, drop critical paths, or emit broken syntax.

ArchiGram solves this by treating diagrams as **compiled, formally verified projections**:
1. **Deterministic Data Plane (`ingest_*` -> `reduce_*` -> `lint_*` -> `emit_*`):** Factual code and runtime traces (`OBSERVED` provenance) are reduced via declarative YAML rules (`CurationSpecV1`) and checked by **5 mathematical gates** (Grounding, Boundary Survival, Reachability & Concurrency Poset Preservation, Cognitive Budget, and Headless Syntax) before a single byte is written.
2. **Bounded AI Control Plane (`entrypoints/mcp` & `archigram propose`):** LLMs never emit raw diagram syntax—they only propose declarative `CurationSpecV1` rules inside a bounded 3-turn self-healing loop guided by formal counterexamples, leaving CI (`just docs-check`) 100% deterministic and zero-cost.

## Choose Your Abstraction Level

Pick the exact zoom level you need for your current task—each guide isolates its scope so you do not have to read unrelated slices.

| Your Goal / Contributor Task | Start Here | Cognitive Scope |
| --- | --- | --- |
| Understand the system rings, Data Plane vs. Control Plane, or integrate the CLI / MCP server | **[L0: System & Hexagonal Rings](L0-system-and-rings.md)** | 9 capability groups (`~12` nodes) |
| Add a new ingestor, emitter, or lint gate (17th vertical slice) without touching sibling slices | **[L1: Feature-Slice Map](L1-feature-slices.md)** | 16 independent slices + 4 rings (`~23` nodes) |
| Evolve Canonical IR, `CurationSpecV1`, `LintReport`, or Hexagonal `Protocol` contracts | **[L2a: Domain Core & Ports](L2-subsystem-guides.md#l2a-domain-core--hexagonal-ports)** | Focused `k_hop` zoom (`9` nodes) |
| Modify topological graph reduction, SCC/TR simplification, or `R1–R6` + SP-Poset trace compaction | **[L2b: Reduction & Concurrency Engine](L2-subsystem-guides.md#l2b-reduction--concurrency-compaction-engine)** | Focused `k_hop` zoom (`16` nodes) |
| Add or tune a validation rule, witness path, or repair hint across Gates 1–5 | **[L2c: 5-Gate Linter](L2-subsystem-guides.md#l2c-5-gate-semantic-topological-budget--syntax-linter)** | Focused `k_hop` zoom (`13` nodes) |
| Work on Typer CLI commands, PR diffing, Markdown sync, FastMCP tools, or the 3-turn repair loop | **[L2d: CLI & MCP Driving Adapters](L2-subsystem-guides.md#l2d-cli-data-plane--mcp-control-plane-adapters)** | Focused `k_hop` zoom (`18` nodes) |
| Trace how `compile_and_lint_pipeline` and `run_propose_loop` execute end-to-end at runtime | **[L3: Runtime Execution & Self-Healing Traces](L3-runtime-traces.md)** | Compacted live sequence (`15` participants, `48` events) |
| Inspect default dual-mode light/dark coloring (`style.py`), UML arrows, orthogonal routing & all 12 target/variant diagrams | **[Styling Policy & Full Multi-Target Gallery](diagram-styling-and-gallery.md)** | All 5 renderers × structural, OOP class & sequence modes |

---

## Declarative Specs, Trace Snapshot & Companion Multi-Target Diagrams

All views are defined declaratively and rendered across **all five supported target languages** (Mermaid, D2 + `.d2.svg`, PlantUML, LikeC4, Graphviz DOT + `.dot.svg`):

- **Canonical Styling Policy:** [`src/archigram/domain/style.py`](../../src/archigram/domain/style.py) (transparent element fills, `#64748b` borders, edge-only semantic strokes, 90-degree orthogonal routing, UML arrowheads)
- **Structural Curation Spec:** [`diagram-views.yaml`](diagram-views.yaml) (compiled against `src/archigram`)
- **Runtime Trace Curation Spec:** [`trace-views.yaml`](trace-views.yaml) (compiled against [`traces/pipeline-and-repair.ir.json`](traces/pipeline-and-repair.ir.json))
- **Live Trace Snapshot:** [`traces/pipeline-and-repair.ir.json`](traces/pipeline-and-repair.ir.json) (captured via `src/archigram/entrypoints/cli/self_trace.py`)

| Level | View ID | Renderer & Mode | Companion Artifacts |
| --- | --- | --- | --- |
| L0 | `l0_hexagonal_rings_mermaid` | Mermaid (`flowchart TD`, ELK linear) | [`diagrams/l0_hexagonal_rings_mermaid.mmd`](diagrams/l0_hexagonal_rings_mermaid.mmd) |
| L0 | `l0_hexagonal_rings_d2` | D2 (`elk` containers + SVG) | [`diagrams/l0_hexagonal_rings_d2.d2`](diagrams/l0_hexagonal_rings_d2.d2) · [`SVG`](diagrams/l0_hexagonal_rings_d2.d2.svg) |
| L0 | `l0_hexagonal_rings_puml` | PlantUML (Mode C ELK component) | [`diagrams/l0_hexagonal_rings_puml.puml`](diagrams/l0_hexagonal_rings_puml.puml) |
| L0 | `l0_hexagonal_rings_likec4` | LikeC4 (structural model & view) | [`diagrams/l0_hexagonal_rings_likec4.c4`](diagrams/l0_hexagonal_rings_likec4.c4) |
| L0 | `l0_hexagonal_rings_dot` | Graphviz DOT (`splines="ortho"` + SVG) | [`diagrams/l0_hexagonal_rings_dot.dot`](diagrams/l0_hexagonal_rings_dot.dot) · [`SVG`](diagrams/l0_hexagonal_rings_dot.dot.svg) |
| L1 | `l1_feature_slices_mermaid` | Mermaid (`flowchart TD`, ELK linear) | [`diagrams/l1_feature_slices_mermaid.mmd`](diagrams/l1_feature_slices_mermaid.mmd) |
| L1 | `l1_feature_slices_d2` | D2 (`elk` containers + SVG) | [`diagrams/l1_feature_slices_d2.d2`](diagrams/l1_feature_slices_d2.d2) · [`SVG`](diagrams/l1_feature_slices_d2.d2.svg) |
| L2a | `l2a_domain_and_ports_mermaid` | Mermaid (`flowchart TD`, ELK linear) | [`diagrams/l2a_domain_and_ports_mermaid.mmd`](diagrams/l2a_domain_and_ports_mermaid.mmd) |
| L2a | `l2a_domain_and_ports_puml` | PlantUML (Mode C ELK component) | [`diagrams/l2a_domain_and_ports_puml.puml`](diagrams/l2a_domain_and_ports_puml.puml) |
| L2b | `l2b_reduction_engine_mermaid` | Mermaid (`flowchart TD`, ELK linear) | [`diagrams/l2b_reduction_engine_mermaid.mmd`](diagrams/l2b_reduction_engine_mermaid.mmd) |
| L2b | `l2b_reduction_engine_d2` | D2 (`elk` containers + SVG) | [`diagrams/l2b_reduction_engine_d2.d2`](diagrams/l2b_reduction_engine_d2.d2) · [`SVG`](diagrams/l2b_reduction_engine_d2.d2.svg) |
| L2c | `l2c_five_gate_linter_mermaid` | Mermaid (`flowchart TD`, ELK linear) | [`diagrams/l2c_five_gate_linter_mermaid.mmd`](diagrams/l2c_five_gate_linter_mermaid.mmd) |
| L2c OOP | `l2c_oop_syntax_validators_mermaid` | Mermaid (`classDiagram`, UML) | [`diagrams/l2c_oop_syntax_validators_mermaid.mmd`](diagrams/l2c_oop_syntax_validators_mermaid.mmd) |
| L2c OOP | `l2c_oop_syntax_validators_d2` | D2 (`shape: class` + SVG) | [`diagrams/l2c_oop_syntax_validators_d2.d2`](diagrams/l2c_oop_syntax_validators_d2.d2) · [`SVG`](diagrams/l2c_oop_syntax_validators_d2.d2.svg) |
| L2c OOP | `l2c_oop_syntax_validators_puml` | PlantUML (Mode B OOP `class` / `interface`) | [`diagrams/l2c_oop_syntax_validators_puml.puml`](diagrams/l2c_oop_syntax_validators_puml.puml) |
| L2d | `l2d_cli_and_mcp_adapters_mermaid` | Mermaid (`flowchart TD`, ELK linear) | [`diagrams/l2d_cli_and_mcp_adapters_mermaid.mmd`](diagrams/l2d_cli_and_mcp_adapters_mermaid.mmd) |
| L3 | `l3_runtime_pipeline_mermaid` | Mermaid (`sequenceDiagram`) | [`diagrams/l3_runtime_pipeline_mermaid.mmd`](diagrams/l3_runtime_pipeline_mermaid.mmd) |
| L3 | `l3_runtime_pipeline_d2` | D2 (`shape: sequence_diagram` + flowing SVG) | [`diagrams/l3_runtime_pipeline_d2.d2`](diagrams/l3_runtime_pipeline_d2.d2) · [`SVG`](diagrams/l3_runtime_pipeline_d2.d2.svg) |
| L3 | `l3_runtime_pipeline_steps_d2` | D2 (`steps` animated multi-frame playback + SVG) | [`diagrams/l3_runtime_pipeline_steps_d2.d2`](diagrams/l3_runtime_pipeline_steps_d2.d2) · [`Animated SVG`](diagrams/l3_runtime_pipeline_steps_d2.d2.svg) |
| L3 | `l3_runtime_pipeline_puml` | PlantUML (Mode A `teoz` sequence) | [`diagrams/l3_runtime_pipeline_puml.puml`](diagrams/l3_runtime_pipeline_puml.puml) |
| L3 | `l3_runtime_pipeline_likec4` | LikeC4 (`dynamic view ... variant sequence`) | [`diagrams/l3_runtime_pipeline_likec4.c4`](diagrams/l3_runtime_pipeline_likec4.c4) |
| L3 | `l3_runtime_pipeline_dot` | Graphviz DOT (orthogonal trace + SVG) | [`diagrams/l3_runtime_pipeline_dot.dot`](diagrams/l3_runtime_pipeline_dot.dot) · [`SVG`](diagrams/l3_runtime_pipeline_dot.dot.svg) |

---

## Regenerating & Verifying Zero Drift (`justfile`)

Use the root [`justfile`](../../justfile) to re-capture traces, compile all companion `.svg` / animated `.d2.svg` diagrams, sync Markdown blocks, and preview live changes:

```bash
# Re-capture trace + compile all L0-L3 diagrams & animated SVGs + sync Markdown docs
just docs

# Verify 5-Gate pass + zero Markdown/SVG drift (also run in Gate 8 of `just verify`)
just docs-check

# Compile any individual .d2 file to <file>.svg (auto-detects `steps:` -> --animate-interval 1200)
just d2 docs/architecture/diagrams/l3_runtime_pipeline_steps_d2.d2

# Live hot-reload a D2 diagram in the browser while editing
just watch-d2 docs/architecture/diagrams/l0_hexagonal_rings_d2.d2
```
