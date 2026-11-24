# L2: Focused Subsystem Guides (`k_hop_neighborhood` Zooms)

**Best for:** Contributors modifying a specific subsystem who want a deep, accurate dependency map of **only that neighborhood** without pulling in the rest of the repository.

Jump directly to your subsystem:
- [L2a: Domain Core & Hexagonal Ports](#l2a-domain-core--hexagonal-ports)
- [L2b: Reduction & Concurrency Compaction Engine](#l2b-reduction--concurrency-compaction-engine)
- [L2c: 5-Gate Semantic, Topological, Budget & Syntax Linter](#l2c-5-gate-semantic-topological-budget--syntax-linter)
- [L2d: CLI Data Plane & MCP Control Plane Adapters](#l2d-cli-data-plane--mcp-control-plane-adapters)
- [Canonical Styling Policy & 12-Mode Multi-Target Gallery](diagram-styling-and-gallery.md)

---

## L2a: Domain Core & Hexagonal Ports

- **Key Files:**
  - `src/archigram/domain/__init__.py` (`ProvenanceGrade`, `ProvenanceStamp`)
  - `src/archigram/domain/ir.py` (`StructuralGraphIR`, `IRNode`, `IREdge`, `InteractionTraceIR`, `TraceEvent`, `TraceParticipant`)
  - `src/archigram/domain/spec.py` (`CurationSpecV1`, `DiagramViewSpec`, `DiagramVariant`, reduction rules, invariants, YAML parser/dumper)
  - `src/archigram/domain/style.py` (`CANONICAL_EDGE_STYLES`, `EdgeVisualStyle`, `resolve_edge_visual_style`, dual-mode transparent target preambles)
  - `src/archigram/domain/diagnostics.py` (`GateId`, `DiagnosticSeverity`, `LintDiagnostic`, `LintReport`)
  - `src/archigram/domain/concurrency.py` (`PosetCotreeNode`, `decompose_series_parallel_poset`, `cluster_overlapping_intervals`)
  - `src/archigram/ports/contracts.py` (8 runtime-checkable `Protocol` interfaces)
- **Core Invariants:**
  - Zero I/O, zero network, zero framework or feature-slice imports inside `domain/` and `ports/`.
  - Every `IRNode`, `IREdge`, `TraceParticipant`, and `TraceEvent` carries an immutable `ProvenanceStamp` (`OBSERVED`, `DERIVED`, or `SYNTHETIC`).
  - `LintReport.passed` is strictly synchronized with `error_count == 0`.
- **What You Do NOT Need to Read:** Any emitter (`emit_*`), any ingestor (`ingest_*`), or CLI/MCP adapters.
- **Companion Renders:** [Mermaid](diagrams/l2a_domain_and_ports_mermaid.mmd) · [PlantUML (`.puml`)](diagrams/l2a_domain_and_ports_puml.puml)

<!-- archigram:start l2a_domain_and_ports_mermaid -->
```mermaid
---
config:
  layout: elk
  look: classic
  elk:
    mergeEdges: false
    nodePlacementStrategy: BRANDES_KOEPF
  flowchart:
    curve: linear
  themeVariables:
    primaryColor: transparent
    secondaryColor: transparent
    tertiaryColor: transparent
    mainBkg: transparent
    nodeBorder: '#64748b'
    clusterBkg: transparent
    clusterBorder: '#64748b'
    lineColor: '#64748b'
---
flowchart TD
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  subgraph n_0_archigram_983b03["archigram"]
    subgraph n_1_archigram_domain_6f6ac3["domain"]
      subgraph n_2_archigram_domain_ae7fc5["ir"]
        n_5_domain_ir_graph_6792fb["StructuralGraphIR, IRNode, IREdge"]
        n_6_domain_ir_trace_8295b5["InteractionTraceIR, TraceEvent, TraceParticipant"]
      end
      n_3_domain_concurren_e2754c["domain.concurrency (PosetCotreeNode, SP-Poset Decomposition)"]
      n_4_domain_diagnosti_fc27fb["domain.diagnostics (LintReport, LintDiagnostic, GateId)"]
      n_7_domain_spec_d7d1fd["domain.spec (CurationSpecV1, DiagramViewSpec, Rules & Invariants)"]
    end
    n_8_ports_protocols_b2b1c2["ports.contracts (Ingestor, Reducer, Compactor, LintGate, Emitter, Digest)"]
  end
  style n_0_archigram_983b03 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_1_archigram_domain_6f6ac3 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_2_archigram_domain_ae7fc5 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n_8_ports_protocols_b2b1c2 -.->|"imports"| n_4_domain_diagnosti_fc27fb
  n_8_ports_protocols_b2b1c2 -.->|"imports"| n_2_archigram_domain_ae7fc5
  n_8_ports_protocols_b2b1c2 -.->|"imports"| n_7_domain_spec_d7d1fd
  n_2_archigram_domain_ae7fc5 -.->|"imports"| n_1_archigram_domain_6f6ac3
  linkStyle 0 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 1 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 2 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 3 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end l2a_domain_and_ports_mermaid -->

---

## L2b: Reduction & Concurrency Compaction Engine

- **Key Files:**
  - `src/archigram/features/reduce_structural/reducer.py` (`StructuralTopologicalReducer`, `k_hop_neighborhood`, `prune_leaves`, `bypass_vertices`, `collapse_partition`, `condense_sccs`, `transitive_reduction`)
  - `src/archigram/features/reduce_trace/compactor.py` (`TraceCompactionEngine`, `TraceFragment`, `build_trace_fragment_tree`, `R1–R6` reduction pipeline)
  - `src/archigram/domain/concurrency.py` (interval overlap clustering & modular Series-Parallel cotree decomposition)
- **Core Invariants:**
  - **Execution Order (`StructuralTopologicalReducer.reduce`):** `global_abstractions` -> `k_hop_neighborhood` (with `retain_ancestors=True`) -> `view.rules` -> `condense_sccs` -> `transitive_reduction`.
  - **Determinism:** Output node/edge/event ordering is canonical and invariant to input dict/list permutations.
  - **Anti-Cycle Safety:** When `forbid_new_cycles=True`, `collapse_partition` raises `CycleInducedError` rather than introducing synthetic cycles between previously acyclic components.
  - **Poset Soundness (`reduce_trace`):** Compaction (`R1` self-call/noise drop, `R2` depth cap, `R3` span collapsing, `R4` contiguous loop folding, `R5` async/sync preservation, `R6` Series-Parallel `par`/`seq` lowering) preserves every causal happens-before relation.
- **What You Do NOT Need to Read:** Ingestors (`ingest_*`), syntax validators (`lint_syntax`), or diagram emitters (`emit_*`).
- **Companion Renders:** [Mermaid](diagrams/l2b_reduction_engine_mermaid.mmd) · [D2 (`.d2`)](diagrams/l2b_reduction_engine_d2.d2) · [D2 SVG](diagrams/l2b_reduction_engine_d2.d2.svg)

<!-- archigram:start l2b_reduction_engine_mermaid -->
```mermaid
---
config:
  layout: elk
  look: classic
  elk:
    mergeEdges: false
    nodePlacementStrategy: BRANDES_KOEPF
  flowchart:
    curve: linear
  themeVariables:
    primaryColor: transparent
    secondaryColor: transparent
    tertiaryColor: transparent
    mainBkg: transparent
    nodeBorder: '#64748b'
    clusterBkg: transparent
    clusterBorder: '#64748b'
    lineColor: '#64748b'
---
flowchart TD
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  subgraph n_0_archigram_983b03["archigram"]
    subgraph n_1_archigram_domain_6f6ac3["domain"]
      n_9_domain_concurren_454309["domain.concurrency (SP-Poset & Overlap Clustering)"]
      n_10_domain_ir_and_sp_b5f20e["domain.ir & domain.spec (Canonical IR & Spec)"]
    end
    subgraph n_2_archigram_entryp_6a8ce0["entrypoints"]
      n_11_driving_entrypoi_ee541f["Driving Adapters (cli.pipeline & mcp.propose_loop)"]
    end
    subgraph n_3_archigram_featur_e6f01e["features"]
      subgraph n_4_archigram_featur_f89487["reduce_structural"]
        subgraph n_5_archigram_featur_df5fa8["reducer"]
          n_12_reduce_structura_7d424e["StructuralTopologicalReducer"]
          n_13_reduce_structura_e46926["Structural Operators (k_hop, prune, bypass, collapse, SCC, TR)"]
        end
      end
      subgraph n_6_archigram_featur_e135d3["reduce_trace"]
        subgraph n_7_archigram_featur_3ee542["compactor"]
          n_14_reduce_trace_eng_1fb2ed["TraceCompactionEngine (R1-R6 + SP-Poset)"]
          n_15_trace_fragment_t_0a27fc["TraceFragment Tree Builder"]
        end
      end
      n_8_concurrency_cons_e749ae["Other Concurrency Consumers (lint_budget, digest)"]
    end
  end
  style n_0_archigram_983b03 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_1_archigram_domain_6f6ac3 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_2_archigram_entryp_6a8ce0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_3_archigram_featur_e6f01e fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_4_archigram_featur_f89487 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_5_archigram_featur_df5fa8 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_6_archigram_featur_e135d3 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_7_archigram_featur_3ee542 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n_11_driving_entrypoi_ee541f -.->|"imports"| n_4_archigram_featur_f89487
  n_11_driving_entrypoi_ee541f -.->|"imports"| n_6_archigram_featur_e135d3
  n_7_archigram_featur_3ee542 -.->|"imports"| n_9_domain_concurren_454309
  n_14_reduce_trace_eng_1fb2ed e_3@-->|"calls"| n_10_domain_ir_and_sp_b5f20e
  e_3@{ animate: true }
  n_5_archigram_featur_df5fa8 -.->|"imports"| n_10_domain_ir_and_sp_b5f20e
  n_7_archigram_featur_3ee542 -.->|"imports"| n_10_domain_ir_and_sp_b5f20e
  n_10_domain_ir_and_sp_b5f20e -.->|"imports"| n_1_archigram_domain_6f6ac3
  n_8_concurrency_cons_e749ae -.->|"imports"| n_9_domain_concurren_454309
  n_8_concurrency_cons_e749ae -.->|"imports"| n_10_domain_ir_and_sp_b5f20e
  n_11_driving_entrypoi_ee541f -.->|"imports"| n_8_concurrency_cons_e749ae
  n_11_driving_entrypoi_ee541f e_10@-->|"calls"| n_12_reduce_structura_7d424e
  e_10@{ animate: true }
  n_12_reduce_structura_7d424e e_11@-->|"calls"| n_13_reduce_structura_e46926
  e_11@{ animate: true }
  n_11_driving_entrypoi_ee541f e_12@-->|"calls"| n_14_reduce_trace_eng_1fb2ed
  e_12@{ animate: true }
  n_14_reduce_trace_eng_1fb2ed e_13@-->|"calls"| n_15_trace_fragment_t_0a27fc
  e_13@{ animate: true }
  n_4_archigram_featur_f89487 -.->|"imports"| n_5_archigram_featur_df5fa8
  n_6_archigram_featur_e135d3 -.->|"imports"| n_7_archigram_featur_3ee542
  linkStyle 0 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 1 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 2 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 3 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 4 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 5 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 6 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 7 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 8 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 9 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 10 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 11 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 12 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 13 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 14 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 15 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end l2b_reduction_engine_mermaid -->

### Rendered D2 View (`l2b_reduction_engine_d2`)

![L2b Reduction & Concurrency Compaction Engine in D2](diagrams/l2b_reduction_engine_d2.d2.svg)

---

## L2c: 5-Gate Semantic, Topological, Budget & Syntax Linter

- **Key Files:**
  - `src/archigram/features/lint_grounding/gates.py` — **Gate 1** (`UNKNOWN_SYMBOL_ID`, `ZERO_MATCH_GLOB`, `INVALID_EDGE_RELATION`) & **Gate 2** (`PRUNE_INTERNAL_BRIDGE`, `PROTECTED_NODE_DROPPED`)
  - `src/archigram/features/lint_topology/gate.py` — **Gate 3** (`REACHABILITY_BROKEN`, `CYCLE_INDUCED`, `DISCONNECTED_PARTITION_MERGE`, `POSET_ORDER_VIOLATION`)
  - `src/archigram/features/lint_budget/gate.py` — **Gate 4** (`NODE_BUDGET_EXCEEDED`, `EDGE_BUDGET_EXCEEDED`, `PARTICIPANT_BUDGET_EXCEEDED`, `EVENT_BUDGET_EXCEEDED`, `ORPHAN_NODE`)
  - `src/archigram/features/lint_syntax/validators.py` — **Gate 5** (`SYNTAX_VALIDATION_FAILED`, headless validation across Mermaid, D2, PlantUML, LikeC4, Graphviz DOT)
- **Core Invariants:**
  - Every `LintDiagnostic` emits an actionable `repair_hint` and `offending_symbols` (plus shortest witness paths in Gate 3) so both humans and the MCP `propose` loop can fix failures mechanically.
  - Gate 2 simulates survival in the exact same order as `StructuralTopologicalReducer.reduce` (`global_rules -> k_hop_neighborhood(retain_ancestors=True) -> view.rules`).
  - All 4 linter slices are mutually independent and depend only on `archigram.domain` and `archigram.ports`.
- **What You Do NOT Need to Read:** Ingestors (`ingest_*`) or diagram emitters (`emit_*`)—only the rendered text crosses into Gate 5.
- **Companion Renders:** [Mermaid Flowchart](diagrams/l2c_five_gate_linter_mermaid.mmd) · [Mermaid OOP Class](diagrams/l2c_oop_syntax_validators_mermaid.mmd) · [D2 OOP Class (`.d2`)](diagrams/l2c_oop_syntax_validators_d2.d2) · [D2 OOP Class SVG](diagrams/l2c_oop_syntax_validators_d2.d2.svg) · [PlantUML OOP Class (`.puml`)](diagrams/l2c_oop_syntax_validators_puml.puml)

<!-- archigram:start l2c_five_gate_linter_mermaid -->
```mermaid
---
config:
  layout: elk
  look: classic
  elk:
    mergeEdges: false
    nodePlacementStrategy: BRANDES_KOEPF
  flowchart:
    curve: linear
  themeVariables:
    primaryColor: transparent
    secondaryColor: transparent
    tertiaryColor: transparent
    mainBkg: transparent
    nodeBorder: '#64748b'
    clusterBkg: transparent
    clusterBorder: '#64748b'
    lineColor: '#64748b'
---
flowchart TD
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  subgraph n_0_archigram_983b03["archigram"]
    subgraph n_1_archigram_domain_6f6ac3["domain"]
      n_5_domain_diagnosti_fc27fb["domain.diagnostics (LintReport, LintDiagnostic, GateId)"]
      n_6_domain_ir_spec_p_0383ff["domain.ir, domain.spec & domain.concurrency"]
    end
    subgraph n_2_archigram_entryp_6a8ce0["entrypoints"]
      n_4_cli_pipeline_run_774c88["entrypoints.cli (pipeline & diff_report)"]
      n_11_mcp_repair_loop_f2e883["entrypoints.mcp (propose_loop & server)"]
    end
    subgraph n_3_archigram_featur_e6f01e["features"]
      n_7_gate_1_2_groundi_256693["Gates 1-2: lint_grounding (Referential & Leaf Safety)"]
      n_8_gate_3_topology_b45c48["Gate 3: lint_topology (Reachability, Cycles, Poset)"]
      n_9_gate_4_budget_6e89c3["Gate 4: lint_budget (Node/Edge Budgets & Orphans)"]
      n_10_gate_5_syntax_76244e["Gate 5: lint_syntax (Headless Mermaid/D2/PUML/C4/DOT)"]
    end
    n_12_ports_contracts_d0accd["ports.contracts (LintGatePort, SyntaxValidatorPort)"]
  end
  style n_0_archigram_983b03 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_1_archigram_domain_6f6ac3 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_2_archigram_entryp_6a8ce0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_3_archigram_featur_e6f01e fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n_9_gate_4_budget_6e89c3 -.->|"imports"| n_5_domain_diagnosti_fc27fb
  n_8_gate_3_topology_b45c48 -.->|"imports"| n_5_domain_diagnosti_fc27fb
  n_7_gate_1_2_groundi_256693 -.->|"imports"| n_5_domain_diagnosti_fc27fb
  n_6_domain_ir_spec_p_0383ff -.->|"imports"| n_1_archigram_domain_6f6ac3
  n_8_gate_3_topology_b45c48 -.->|"imports"| n_6_domain_ir_spec_p_0383ff
  n_7_gate_1_2_groundi_256693 -.->|"imports"| n_6_domain_ir_spec_p_0383ff
  n_12_ports_contracts_d0accd -.->|"imports"| n_6_domain_ir_spec_p_0383ff
  n_9_gate_4_budget_6e89c3 -.->|"imports"| n_6_domain_ir_spec_p_0383ff
  n_11_mcp_repair_loop_f2e883 -.->|"imports"| n_7_gate_1_2_groundi_256693
  n_11_mcp_repair_loop_f2e883 -.->|"imports"| n_8_gate_3_topology_b45c48
  n_11_mcp_repair_loop_f2e883 -.->|"imports"| n_9_gate_4_budget_6e89c3
  n_11_mcp_repair_loop_f2e883 -.->|"imports"| n_10_gate_5_syntax_76244e
  n_4_cli_pipeline_run_774c88 -.->|"imports"| n_11_mcp_repair_loop_f2e883
  n_12_ports_contracts_d0accd -.->|"imports"| n_5_domain_diagnosti_fc27fb
  n_10_gate_5_syntax_76244e -.->|"imports"| n_12_ports_contracts_d0accd
  linkStyle 0 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 1 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 2 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 3 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 4 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 5 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 6 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 7 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 8 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 9 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 10 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 11 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 12 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 13 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 14 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end l2c_five_gate_linter_mermaid -->

### Gate 5 OOP Protocol & Concrete Validators (`diagram_variant: class`)

Zooming into `SyntaxValidatorPort`, `Gate5SyntaxHarness`, and the 5 concrete validators in **OOP Class mode** showcases UML `<<interface>>` realization (`<|..`), filled-diamond composition (`*--`), and method compartments in both Mermaid `classDiagram` and rendered D2 (`shape: class`):

<!-- archigram:start l2c_oop_syntax_validators_mermaid -->
```mermaid
---
config:
  layout: elk
  look: classic
  elk:
    mergeEdges: false
    nodePlacementStrategy: BRANDES_KOEPF
  flowchart:
    curve: linear
  themeVariables:
    primaryColor: transparent
    secondaryColor: transparent
    tertiaryColor: transparent
    mainBkg: transparent
    nodeBorder: '#64748b'
    clusterBkg: transparent
    clusterBorder: '#64748b'
    lineColor: '#64748b'
---
classDiagram
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  class c_0_archigram_featur_ba3738["lint_syntax"]
  class c_1_archigram_featur_d6e8be["validators"]
  class c_2_archigram_featur_bb531f["D2SyntaxValidator"] {
    +init(use_cli: bool, cli_timeout: float) None
    +target_format() str property
    +validate(diagram_source: str) SyntaxValidationResult
  }
  class c_3_archigram_featur_86114b["DotSyntaxValidator"] {
    +init(use_cli: bool, cli_timeout: float) None
    +target_format() str property
    +validate(diagram_source: str) SyntaxValidationResult
  }
  class c_4_archigram_featur_4106c5["Gate5SyntaxHarness"] {
    +str slice_name
    +init(validators: Mapping#91;str, SyntaxValidatorPort#93; | None) None
    +supported_formats() tuple_91_str_93 property
    +get_validator(target_format: str) SyntaxValidatorPort_None
    +validate(target_format: str, diagram_source: str) SyntaxValidationResult
  }
  class c_5_archigram_featur_c2d69d["LikeC4SyntaxValidator"] {
    +target_format() str property
    +validate(diagram_source: str) SyntaxValidationResult
  }
  class c_6_archigram_featur_086112["MermaidSyntaxValidator"] {
    +target_format() str property
    +validate(diagram_source: str) SyntaxValidationResult
  }
  class c_7_archigram_featur_46a958["PlantUmlSyntaxValidator"] {
    +init(use_cli: bool, cli_timeout: float) None
    +target_format() str property
    +validate(diagram_source: str) SyntaxValidationResult
  }
  class c_8_archigram_ports__7ed230["contracts"]
  class c_9_archigram_ports__58ed33["SyntaxValidationResult"] {
    +bool valid
    +str target_format
    +tuple#91;str, ...#93; errors
  }
  class c_10_archigram_ports__993eea["SyntaxValidatorPort"] {
    <<interface>>
    +target_format() str property
    +validate(diagram_source: str) SyntaxValidationResult
  }
  c_0_archigram_featur_ba3738 ..> c_1_archigram_featur_d6e8be : imports
  c_1_archigram_featur_d6e8be ..> c_8_archigram_ports__7ed230 : imports
  c_2_archigram_featur_bb531f --> c_9_archigram_ports__58ed33 : calls
  c_10_archigram_ports__993eea <|.. c_2_archigram_featur_bb531f : implements
  c_3_archigram_featur_86114b --> c_9_archigram_ports__58ed33 : calls
  c_10_archigram_ports__993eea <|.. c_3_archigram_featur_86114b : implements
  c_4_archigram_featur_4106c5 --> c_9_archigram_ports__58ed33 : calls
  c_10_archigram_ports__993eea *-- c_4_archigram_featur_4106c5 : composes
  c_5_archigram_featur_c2d69d --> c_9_archigram_ports__58ed33 : calls
  c_10_archigram_ports__993eea <|.. c_5_archigram_featur_c2d69d : implements
  c_6_archigram_featur_086112 --> c_9_archigram_ports__58ed33 : calls
  c_10_archigram_ports__993eea <|.. c_6_archigram_featur_086112 : implements
  c_7_archigram_featur_46a958 --> c_9_archigram_ports__58ed33 : calls
  c_10_archigram_ports__993eea <|.. c_7_archigram_featur_46a958 : implements
```
<!-- archigram:end l2c_oop_syntax_validators_mermaid -->

![L2c OOP Gate 5 Validators in D2](diagrams/l2c_oop_syntax_validators_d2.d2.svg)

---

## L2d: CLI Data Plane & MCP Control Plane Adapters

- **Key Files:**
  - `src/archigram/entrypoints/cli/app.py` — Typer CLI (`archigram apply`, `archigram lint`, `archigram diff`, `archigram propose`)
  - `src/archigram/entrypoints/cli/pipeline.py` — Zero-LLM orchestrator (`compile_and_lint_pipeline`, auto-detecting ingestors, reducers, Gates 1–5, and emitters)
  - `src/archigram/entrypoints/cli/markdown_sync.py` — Marker-based Markdown sync (`<!-- archigram:start <view_id> --> ... <!-- archigram:end <view_id> -->`) and `--check` drift enforcement
  - `src/archigram/entrypoints/cli/diff_report.py` — Structural/architectural PR regression diff reporter
  - `src/archigram/entrypoints/cli/self_trace.py` — Deterministic live `sys.monitoring` self-hosted trace snapshotter
  - `src/archigram/entrypoints/mcp/server.py` — FastMCP tools (`get_graph_digest`, `lint_curation_spec`, `preview_curated_view`, `propose_curation_spec`)
  - `src/archigram/entrypoints/mcp/propose_loop.py` — Bounded 3-turn self-healing proposal -> 5-Gate lint -> repair loop
  - `src/archigram/features/digest/generator.py` — Token-efficient Louvain/PageRank/hotspot Markdown digests for LLM context
- **Core Invariants:**
  - **Strict Plane Separation:** `cli.pipeline` executes deterministically with zero LLM calls; `mcp.propose_loop` is strictly bounded (`max_turns=3`) and can never emit unverified diagrams because every candidate spec is re-checked by all 5 Gates.
  - **Composition Root:** `entrypoints/` is the sole layer permitted to import and wire feature slices together.
- **What You Do NOT Need to Read:** Internal graph math inside `reduce_structural`, `reduce_trace`, or `domain.concurrency`.
- **Companion Renders:** [Mermaid](diagrams/l2d_cli_and_mcp_adapters_mermaid.mmd)

<!-- archigram:start l2d_cli_and_mcp_adapters_mermaid -->
```mermaid
---
config:
  layout: elk
  look: classic
  elk:
    mergeEdges: false
    nodePlacementStrategy: BRANDES_KOEPF
  flowchart:
    curve: linear
  themeVariables:
    primaryColor: transparent
    secondaryColor: transparent
    tertiaryColor: transparent
    mainBkg: transparent
    nodeBorder: '#64748b'
    clusterBkg: transparent
    clusterBorder: '#64748b'
    lineColor: '#64748b'
---
flowchart TD
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  subgraph n_0_archigram_983b03["archigram"]
    subgraph n_1_archigram_entryp_6a8ce0["entrypoints"]
      subgraph n_2_archigram_entryp_f9dc23["cli"]
        n_5_cli_app_af790b["cli.app (Typer apply/lint/diff/propose)"]
        n_6_cli_diff_report_7a51b7["cli.diff_report (PR Regression Diff)"]
        n_7_cli_markdown_syn_cf9552["cli.markdown_sync (--sync-md Inline Blocks)"]
        n_8_cli_pipeline_6371a4["cli.pipeline (Zero-LLM Compile & Lint Orchestrator)"]
        n_9_cli_self_trace_5e0e93["cli.self_trace (Live Trace Snapshotter)"]
      end
      subgraph n_3_archigram_entryp_d739e6["mcp"]
        n_15_mcp_propose_loop_e477f8["mcp.propose_loop (Bounded 3-Turn Repair Loop)"]
        n_16_mcp_server_f6e365["mcp.server (FastMCP Tools: digest, lint, preview)"]
      end
    end
    subgraph n_4_archigram_featur_e6f01e["features"]
      n_10_feature_digest_52876d["features.digest (GraphDigestGenerator)"]
      n_11_feature_emitters_75a4c0["Emitter Slices (Mermaid/D2/PUML/LikeC4)"]
      n_12_feature_ingestor_73234e["Ingestor Slices (DOT/SCIP/OTLP/Class/Python)"]
      n_13_feature_linters_57c819["5-Gate Linter Slices"]
      n_14_feature_reducers_8a2858["Reducer Slices (Structural & Trace)"]
    end
    n_17_ports_and_domain_38e7a5["Ports & Domain Core"]
  end
  style n_0_archigram_983b03 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_1_archigram_entryp_6a8ce0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_2_archigram_entryp_f9dc23 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_3_archigram_entryp_d739e6 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n_4_archigram_featur_e6f01e fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n_2_archigram_entryp_f9dc23 -.->|"imports"| n_5_cli_app_af790b
  n_6_cli_diff_report_7a51b7 -.->|"imports"| n_8_cli_pipeline_6371a4
  n_5_cli_app_af790b -.->|"imports"| n_6_cli_diff_report_7a51b7
  n_7_cli_markdown_syn_cf9552 -.->|"imports"| n_8_cli_pipeline_6371a4
  n_2_archigram_entryp_f9dc23 -.->|"imports"| n_7_cli_markdown_syn_cf9552
  n_9_cli_self_trace_5e0e93 -.->|"imports"| n_8_cli_pipeline_6371a4
  n_15_mcp_propose_loop_e477f8 -.->|"imports"| n_10_feature_digest_52876d
  n_15_mcp_propose_loop_e477f8 -.->|"imports"| n_11_feature_emitters_75a4c0
  n_8_cli_pipeline_6371a4 -.->|"imports"| n_11_feature_emitters_75a4c0
  n_8_cli_pipeline_6371a4 -.->|"imports"| n_12_feature_ingestor_73234e
  n_15_mcp_propose_loop_e477f8 -.->|"imports"| n_13_feature_linters_57c819
  n_8_cli_pipeline_6371a4 -.->|"imports"| n_13_feature_linters_57c819
  n_9_cli_self_trace_5e0e93 -.->|"imports"| n_15_mcp_propose_loop_e477f8
  n_16_mcp_server_f6e365 -.->|"imports"| n_15_mcp_propose_loop_e477f8
  n_3_archigram_entryp_d739e6 -.->|"imports"| n_16_mcp_server_f6e365
  n_10_feature_digest_52876d -.->|"imports"| n_17_ports_and_domain_38e7a5
  n_8_cli_pipeline_6371a4 -.->|"imports"| n_17_ports_and_domain_38e7a5
  n_15_mcp_propose_loop_e477f8 -.->|"imports"| n_14_feature_reducers_8a2858
  n_8_cli_pipeline_6371a4 -.->|"imports"| n_14_feature_reducers_8a2858
  linkStyle 0 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 1 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 2 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 3 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 4 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 5 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 6 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 7 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 8 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 9 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 10 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 11 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 12 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 13 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 14 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 15 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 16 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 17 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 18 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end l2d_cli_and_mcp_adapters_mermaid -->
