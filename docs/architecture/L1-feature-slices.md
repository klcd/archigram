# L1: Feature-Slice Map & Independence Guarantee

**Best for:** Contributors adding a new ingestor, diagram emitter, or lint gate who want to know where every slice lives and verify what they are allowed to import.

- **Zoom Out / In:** [L0: System & Rings](L0-system-and-rings.md) · [L2: Subsystem Guides](L2-subsystem-guides.md) · [L3: Runtime Traces](L3-runtime-traces.md)
- **Companion Renders:** [Mermaid](diagrams/l1_feature_slices_mermaid.mmd) · [D2 (`.d2`)](diagrams/l1_feature_slices_d2.d2)

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
| | `emit_d2` | Deterministic D2 architecture & class diagrams | `DiagramEmitterPort` |
| | `emit_plantuml` | Deterministic PlantUML component, class & sequence diagrams | `DiagramEmitterPort` |
| | `emit_likec4` | Deterministic LikeC4 `.c4` architecture models & views | `DiagramEmitterPort` |
| **Digest (1)** | `digest` | Token-efficient Louvain/PageRank/hotspot digests for MCP & `propose` | `DigestGeneratorPort` |

---

## Curated View (`l1_feature_slices_mermaid`)

All 16 slices + 4 structural rings collapsed into 1 supernode each (within the `<= 25` cognitive node budget, `forbid_new_cycles: true`):

<!-- archigram:start l1_feature_slices_mermaid -->
```mermaid
---
config:
  layout: elk
---
flowchart TD
  subgraph n_0_archigram_983b03["archigram"]
    subgraph n_1_archigram_entryp_6a8ce0["entrypoints"]
      n_9_entrypoints_cli_4bc091["entrypoints.cli"]
      n_10_entrypoints_mcp_55388b["entrypoints.mcp"]
    end
    subgraph n_2_archigram_featur_e6f01e["features"]
      n_3_digest_0bf474["digest"]
      n_5_emit_d2_620ecc["emit_d2"]
      n_6_emit_likec4_1b4b94["emit_likec4"]
      n_7_emit_mermaid_1088af["emit_mermaid"]
      n_8_emit_plantuml_e13ade["emit_plantuml"]
      n_11_ingest_class_b85511["ingest_class"]
      n_12_ingest_dot_b06b1d["ingest_dot"]
      n_13_ingest_otel_03f3a3["ingest_otel"]
      n_14_ingest_python_d587ef["ingest_python"]
      n_15_ingest_scip_e323c8["ingest_scip"]
      n_16_lint_budget_5ef046["lint_budget"]
      n_17_lint_grounding_60eca0["lint_grounding"]
      n_18_lint_syntax_703d04["lint_syntax"]
      n_19_lint_topology_df2e1d["lint_topology"]
      n_21_reduce_structura_382821["reduce_structural"]
      n_22_reduce_trace_a80f36["reduce_trace"]
    end
    n_4_domain_f2ff83["domain"]
    n_20_ports_87afb3["ports"]
  end
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_3_digest_0bf474
  n_19_lint_topology_df2e1d -.->|"imports"| n_4_domain_f2ff83
  n_7_emit_mermaid_1088af -.->|"imports"| n_4_domain_f2ff83
  n_3_digest_0bf474 -.->|"imports"| n_4_domain_f2ff83
  n_17_lint_grounding_60eca0 -->|"calls"| n_4_domain_f2ff83
  n_5_emit_d2_620ecc -.->|"imports"| n_4_domain_f2ff83
  n_3_digest_0bf474 -->|"composes"| n_4_domain_f2ff83
  n_16_lint_budget_5ef046 -->|"calls"| n_4_domain_f2ff83
  n_22_reduce_trace_a80f36 -->|"calls"| n_4_domain_f2ff83
  n_19_lint_topology_df2e1d -->|"composes"| n_4_domain_f2ff83
  n_19_lint_topology_df2e1d -->|"calls"| n_4_domain_f2ff83
  n_22_reduce_trace_a80f36 -.->|"imports"| n_4_domain_f2ff83
  n_17_lint_grounding_60eca0 -.->|"imports"| n_4_domain_f2ff83
  n_8_emit_plantuml_e13ade -->|"composes"| n_4_domain_f2ff83
  n_20_ports_87afb3 -.->|"imports"| n_4_domain_f2ff83
  n_21_reduce_structura_382821 -.->|"imports"| n_4_domain_f2ff83
  n_16_lint_budget_5ef046 -.->|"imports"| n_4_domain_f2ff83
  n_8_emit_plantuml_e13ade -.->|"imports"| n_4_domain_f2ff83
  n_7_emit_mermaid_1088af -->|"composes"| n_4_domain_f2ff83
  n_6_emit_likec4_1b4b94 -.->|"imports"| n_4_domain_f2ff83
  n_21_reduce_structura_382821 -->|"calls"| n_4_domain_f2ff83
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_5_emit_d2_620ecc
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_6_emit_likec4_1b4b94
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_7_emit_mermaid_1088af
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_8_emit_plantuml_e13ade
  n_9_entrypoints_cli_4bc091 -.->|"imports"| n_11_ingest_class_b85511
  n_9_entrypoints_cli_4bc091 -.->|"imports"| n_12_ingest_dot_b06b1d
  n_9_entrypoints_cli_4bc091 -.->|"imports"| n_13_ingest_otel_03f3a3
  n_9_entrypoints_cli_4bc091 -->|"calls"| n_14_ingest_python_d587ef
  n_9_entrypoints_cli_4bc091 -.->|"imports"| n_14_ingest_python_d587ef
  n_9_entrypoints_cli_4bc091 -.->|"imports"| n_15_ingest_scip_e323c8
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_16_lint_budget_5ef046
  n_10_entrypoints_mcp_55388b -->|"calls"| n_17_lint_grounding_60eca0
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_17_lint_grounding_60eca0
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_18_lint_syntax_703d04
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_19_lint_topology_df2e1d
  n_9_entrypoints_cli_4bc091 -.->|"imports"| n_10_entrypoints_mcp_55388b
  n_9_entrypoints_cli_4bc091 -->|"calls"| n_10_entrypoints_mcp_55388b
  n_18_lint_syntax_703d04 -.->|"imports"| n_20_ports_87afb3
  n_14_ingest_python_d587ef -->|"implements"| n_20_ports_87afb3
  n_14_ingest_python_d587ef -.->|"imports"| n_20_ports_87afb3
  n_13_ingest_otel_03f3a3 -.->|"imports"| n_20_ports_87afb3
  n_13_ingest_otel_03f3a3 -->|"implements"| n_20_ports_87afb3
  n_18_lint_syntax_703d04 -->|"implements"| n_20_ports_87afb3
  n_15_ingest_scip_e323c8 -->|"implements"| n_20_ports_87afb3
  n_15_ingest_scip_e323c8 -.->|"imports"| n_20_ports_87afb3
  n_12_ingest_dot_b06b1d -.->|"imports"| n_20_ports_87afb3
  n_11_ingest_class_b85511 -.->|"imports"| n_20_ports_87afb3
  n_11_ingest_class_b85511 -->|"implements"| n_20_ports_87afb3
  n_18_lint_syntax_703d04 -->|"calls"| n_20_ports_87afb3
  n_12_ingest_dot_b06b1d -->|"implements"| n_20_ports_87afb3
  n_18_lint_syntax_703d04 -->|"composes"| n_20_ports_87afb3
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_21_reduce_structura_382821
  n_10_entrypoints_mcp_55388b -.->|"imports"| n_22_reduce_trace_a80f36
```
<!-- archigram:end l1_feature_slices_mermaid -->

---

## Checklist: Adding a 17th Vertical Slice Without Reading Sibling Slices

Because slices are mutually independent, adding a new emitter, ingestor, or lint gate requires touching **zero** sibling slices:

1. **Read only the contract & models:** Inspect [`src/archigram/ports/contracts.py`](../../src/archigram/ports/contracts.py) and [`src/archigram/domain/ir.py`](../../src/archigram/domain/ir.py) (or jump to [L2a](L2-subsystem-guides.md#l2a-domain-core--hexagonal-ports)).
2. **Create `src/archigram/features/<new_slice>/`:** Implement the corresponding `Protocol` using pure `archigram.domain` types.
3. **Wire it at the edge:** Register the new slice in `src/archigram/entrypoints/cli/pipeline.py` (and `src/archigram/entrypoints/mcp/` if exposed as an MCP tool).
4. **Add Hypothesis fuzz + `pytest-benchmark` tests:** Create `tests/fuzz/fuzz_<new_slice>.py` (`@given` / `RuleBasedStateMachine`) and `tests/perf/perf_<new_slice>.py` (`benchmark` fixture)—no static unit tests.
5. **Verify all 7 gates:** Run `./scripts/verify.sh` (`ruff`, strict `pyright`, `lint-imports`, `check-test-discipline.py`, fuzz, perf).
