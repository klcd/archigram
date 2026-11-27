# L1: Feature-Slice Map & Independence Guarantee

**Best for:** Contributors adding a new ingestor, diagram emitter, or lint gate who want to know where every slice lives and verify what they are allowed to import.

- **Zoom Out / In:** [L0: System & Rings](L0-system-and-rings.md) · [L2: Subsystem Guides](L2-subsystem-guides.md) · [L3: Runtime Traces](L3-runtime-traces.md) · [Styling & Full Multi-Target Gallery](diagram-styling-and-gallery.md)
- **Companion Renders:** [Mermaid](diagrams/l1_feature_slices_mermaid.mmd) · [D2 (`.d2`)](diagrams/l1_feature_slices_d2.d2) · [D2 SVG](diagrams/l1_feature_slices_d2.d2.svg)

---

## Zero Cross-Slice Coupling (`import-linter` Contract)

Every directory under `src/archigram/features/<slice>/` is a **vertically isolated slice**. An `import-linter` `independence` contract enforces on every commit that:

- A feature slice may import **only** from `archigram.domain` and `archigram.ports` (plus standard library / declared third-party packages).
- A feature slice may **never** import from another feature slice or from `archigram.entrypoints`.
- Composition happens strictly in the outer driving adapters (`entrypoints.cli.pipeline` and `entrypoints.mcp`).

---

## The 16 Independent Vertical Slices

| Family | Slice | Primary Responsibility | Port Contract |
| --- | --- | --- | --- |
| **Ingestors (5)** | `ingest_dot` | Parse Graphviz `.dot` graphs into `StructuralGraphIR` | `StructuralIngestorPort` |
| | `ingest_scip` | Parse SCIP code-intelligence indexes into `StructuralGraphIR` | `StructuralIngestorPort` |
| | `ingest_otel` | Parse OpenTelemetry OTLP JSON spans into `InteractionTraceIR` | `TraceIngestorPort` |
| | `ingest_class` | Parse Python/TypeScript/Java/Go/Rust OOP class schemas | `StructuralIngestorPort` |
| | `ingest_python` | Extract Python AST graphs & live `sys.monitoring` traces | `StructuralIngestorPort` / `TraceIngestorPort` |
| **Reducers (2)** | `reduce_structural` | `k_hop`, `prune_leaves`, `bypass_vertices`, `collapse_partition`, SCC, TR | `StructuralReducerPort` |
| | `reduce_trace` | `R1–R6` trace compaction + Series-Parallel poset lowering | `TraceCompactorPort` |
| **Linters (4)** | `lint_grounding` | Gate 1 (closed-world symbol/glob grounding) & Gate 2 (leaf & protected survival) | `LintGatePort` |
| | `lint_topology` | Gate 3 (reachability witnesses, anti-cycle, cohesion, poset preservation) | `LintGatePort` |
| | `lint_budget` | Gate 4 (cognitive node/edge/participant/event budgets & orphan detection) | `LintGatePort` |
| | `lint_syntax` | Gate 5 (headless syntax verification for Mermaid, D2, PlantUML, LikeC4, DOT) | `SyntaxValidatorPort` |
| **Emitters (4)** | `emit_mermaid` | Deterministic Mermaid `flowchart` / `classDiagram` / `sequenceDiagram` | `DiagramEmitterPort` |
| | `emit_d2` | Deterministic D2 architecture, OOP `shape: class` & sequence diagrams | `DiagramEmitterPort` |
| | `emit_plantuml` | Deterministic PlantUML component, class & `teoz` sequence diagrams | `DiagramEmitterPort` |
| | `emit_likec4` | Deterministic LikeC4 `.c4` architecture & dynamic sequence views | `DiagramEmitterPort` |
| **Digest (1)** | `digest` | Token-efficient Louvain/PageRank/hotspot digests for MCP & `propose` | `DigestGeneratorPort` |

---

## Curated View — Mermaid Orthogonal Flowchart (`l1_feature_slices_mermaid`)

All 16 slices + 4 structural rings collapsed into 1 supernode each (within the `<= 25` cognitive node budget, `forbid_new_cycles: true`):

<!-- archigram:start l1_feature_slices_mermaid -->
```mermaid
---
config:
  layout: elk
  elk:
    mergeEdges: false
    nodePlacementStrategy: BRANDES_KOEPF
  flowchart:
    defaultRenderer: elk
    curve: linear
---
flowchart TD
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  subgraph n0["archigram"]
    subgraph n1["entrypoints"]
      n8["entrypoints.cli"]
      n9["entrypoints.mcp"]
    end
    subgraph n2["features"]
      n3["digest"]
      n5["emit_d2"]
      n6["emit_likec4 + emit_plantuml"]
      n7["emit_mermaid"]
      n10["ingest_class"]
      n11["ingest_dot"]
      n12["ingest_otel"]
      n13["ingest_python"]
      n14["ingest_scip"]
      n15["lint_budget"]
      n16["lint_grounding"]
      n17["lint_syntax"]
      n18["lint_topology"]
      n20["reduce_structural"]
      n21["reduce_trace"]
    end
    n4["domain"]
    n19["ports"]
  end
  style n0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n1 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n2 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n8 -.->|"imports"| n0
  n9 -->|"calls"| n3
  n9 -.->|"imports"| n3
  n12 -.->|"imports"| n4
  n18 -.->|"imports"| n4
  n7 -.->|"imports"| n4
  n11 -->|"calls"| n4
  n21 -->|"composes"| n4
  n12 -->|"calls"| n4
  n3 -.->|"imports"| n4
  n6 -->|"composes"| n4
  n13 -->|"composes"| n4
  n14 -->|"calls"| n4
  n5 -->|"calls"| n4
  n16 -->|"calls"| n4
  n6 -.->|"imports"| n4
  n5 -.->|"imports"| n4
  n13 -.->|"imports"| n4
  n3 -->|"composes"| n4
  n15 -->|"calls"| n4
  n21 -->|"calls"| n4
  n10 -->|"composes"| n4
  n18 -->|"composes"| n4
  n18 -->|"calls"| n4
  n14 -.->|"imports"| n4
  n21 -.->|"imports"| n4
  n16 -.->|"imports"| n4
  n6 -->|"calls"| n4
  n5 -->|"composes"| n4
  n13 -->|"calls"| n4
  n10 -->|"calls"| n4
  n10 -.->|"imports"| n4
  n11 -->|"composes"| n4
  n20 -.->|"imports"| n4
  n15 -.->|"imports"| n4
  n7 -->|"composes"| n4
  n14 -->|"composes"| n4
  n11 -.->|"imports"| n4
  n12 -->|"composes"| n4
  n20 -->|"calls"| n4
  n9 -.->|"imports"| n5
  n9 -->|"calls"| n5
  n9 -.->|"imports"| n6
  n9 -->|"calls"| n6
  n9 -->|"calls"| n7
  n9 -.->|"imports"| n7
  n8 -.->|"imports"| n10
  n8 -.->|"imports"| n11
  n8 -.->|"imports"| n12
  n8 -->|"calls"| n13
  n8 -.->|"imports"| n13
  n8 -.->|"imports"| n14
  n9 -->|"calls"| n15
  n9 -.->|"imports"| n15
  n9 -->|"calls"| n16
  n9 -.->|"imports"| n16
  n9 -.->|"imports"| n17
  n9 -->|"calls"| n17
  n9 -->|"calls"| n18
  n9 -.->|"imports"| n18
  n8 -.->|"imports"| n9
  n8 -->|"calls"| n9
  n17 -.->|"imports"| n19
  n13 -.->|"implements"| n19
  n13 -.->|"imports"| n19
  n12 -.->|"imports"| n19
  n12 -.->|"implements"| n19
  n17 -.->|"implements"| n19
  n14 -.->|"implements"| n19
  n14 -.->|"imports"| n19
  n11 -.->|"imports"| n19
  n10 -.->|"imports"| n19
  n10 -.->|"implements"| n19
  n17 -->|"calls"| n19
  n11 -.->|"implements"| n19
  n17 -->|"composes"| n19
  n9 -->|"calls"| n20
  n9 -.->|"imports"| n20
  n9 -.->|"imports"| n21
  n9 -->|"calls"| n21
  linkStyle 0,2,3,4,5,9,15,16,17,24,25,26,31,33,34,37,40,42,45,46,47,48,50,51,53,55,56,59,60,62,64,65,69,70,71,77,78 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 1,6,8,12,13,14,19,20,23,27,29,30,39,41,43,44,49,52,54,57,58,61,73,76,79 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 7,10,11,18,21,22,28,32,35,36,38,75 stroke:#14b8a6,stroke-width:1.75px
  linkStyle 63,66,67,68,72,74 stroke:#a855f7,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end l1_feature_slices_mermaid -->

---

## Curated View — Rendered D2 Feature Slices (`l1_feature_slices_d2`)

![L1 Independent Feature Slices in D2](diagrams/l1_feature_slices_d2.d2.svg)

---

## Checklist: Adding a 17th Vertical Slice Without Reading Sibling Slices

Because slices are mutually independent, adding a new emitter, ingestor, or lint gate requires touching **zero** sibling slices:

1. **Read only the contract & models:** Inspect [`src/archigram/ports/contracts.py`](../../src/archigram/ports/contracts.py) and [`src/archigram/domain/ir.py`](../../src/archigram/domain/ir.py) (or jump to [L2a](L2-subsystem-guides.md#l2a-domain-core--hexagonal-ports)).
2. **Create `src/archigram/features/<new_slice>/`:** Implement the corresponding `Protocol` using pure `archigram.domain` types.
3. **Wire it at the edge:** Register the new slice in `src/archigram/entrypoints/cli/pipeline.py` (and `src/archigram/entrypoints/mcp/` if exposed as an MCP tool).
4. **Add Hypothesis fuzz + `pytest-benchmark` tests:** Create `tests/fuzz/fuzz_<new_slice>.py` (`@given` / `RuleBasedStateMachine`) and `tests/perf/perf_<new_slice>.py` (`benchmark` fixture)—no static unit tests.
5. **Verify all 7 gates:** Run `./scripts/verify.sh` (`ruff`, strict `pyright`, `lint-imports`, `check-test-discipline.py`, fuzz, perf).
