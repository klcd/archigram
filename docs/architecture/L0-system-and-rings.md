# L0: System Overview & Hexagonal Rings

**Best for:** New contributors, CLI/MCP integrators, and reviewers who want the full system mental model in under two minutes without wading into individual slices.

- **Zoom In:** [L1: Feature-Slice Map](L1-feature-slices.md) · [L2: Subsystem Guides](L2-subsystem-guides.md) · [L3: Runtime Traces](L3-runtime-traces.md)
- **Companion Renders:** [Mermaid](diagrams/l0_hexagonal_rings_mermaid.mmd) · [LikeC4 (`.c4`)](diagrams/l0_hexagonal_rings_likec4.c4) · [D2 (`.d2`)](diagrams/l0_hexagonal_rings_d2.d2)

---

## Hexagonal Dependency Rule

ArchiGram enforces a strict **inward-only** Hexagonal Architecture (`lint-imports` runs on every commit):

```text
archigram.entrypoints  ──▶  archigram.features  ──▶  archigram.ports  ──▶  archigram.domain
   (Driving Adapters)        (16 Vertical Slices)     (Protocol Contracts)   (Zero-I/O Core)
```

1. **`domain_core` (`src/archigram/domain/`)**: Pure Pydantic v2 models (`StructuralGraphIR`, `InteractionTraceIR`, `CurationSpecV1`, `LintReport`, `GraphDigest`) and Series-Parallel poset math. Zero I/O, zero CLI/MCP/emitter imports.
2. **`ports_contracts` (`src/archigram/ports/`)**: Runtime-checkable Python `Protocol` contracts (`StructuralIngestorPort`, `TraceIngestorPort`, `StructuralReducerPort`, `TraceCompactorPort`, `LintGatePort`, `SyntaxValidatorPort`, `DiagramEmitterPort`, `DigestGeneratorPort`).
3. **Capability Groups (`src/archigram/features/*`)**: Five horizontal capability families implemented as **16 strictly independent vertical slices** (Ingestors, Reducers, 5-Gate Linters, Emitters, Digest). Slices never import each other.
4. **Driving Adapters (`src/archigram/entrypoints/`)**:
   - **Data Plane (`cli_dataplane`)**: Zero-LLM deterministic compiler (`apply`, `lint`, `diff`, `--sync-md`, `self_trace`) safe for CI pipelines and pre-commit hooks.
   - **Control Plane (`mcp_controlplane`)**: FastMCP server (`digest`, `lint`, `preview`) and bounded 3-turn self-healing `propose` loop for LLM-assisted spec authoring.

---

## Curated View (`l0_hexagonal_rings_mermaid`)

Proven by Gate 3 invariants in [`diagram-views.yaml`](diagram-views.yaml):
- `forbid_new_cycles: true` (strict DAG across all Hexagonal rings)
- `preserve_reachability`: `entrypoints.cli.pipeline -> ports.contracts -> domain.ir`

<!-- archigram:start l0_hexagonal_rings_mermaid -->
```mermaid
---
config:
  layout: elk
---
flowchart TD
  subgraph n_0_archigram_983b03["archigram"]
    subgraph n_1_archigram_entryp_6a8ce0["entrypoints"]
      n_3_cli_dataplane_493a5b["CLI Data Plane (entrypoints.cli)"]
      n_10_mcp_controlplane_ff4d3b["MCP Control Plane (entrypoints.mcp)"]
    end
    subgraph n_2_archigram_featur_e6f01e["features"]
      n_5_feature_digest_52876d["Digest Slice (Graph & Trace Digest)"]
      n_6_feature_emitters_75a4c0["Emitter Slices (Mermaid, D2, PlantUML, LikeC4)"]
      n_7_feature_ingestor_73234e["Ingestor Slices (DOT, SCIP, OTLP, Class, Python)"]
      n_8_feature_linters_57c819["5-Gate Linter Slices (Grounding, Topology, Budget, Syntax)"]
      n_9_feature_reducers_8a2858["Reducer Slices (Structural + Trace Compactor)"]
    end
    n_4_domain_core_f195b2["Domain Core (Canonical IR, Spec, Poset, Diagnostics)"]
    n_11_ports_contracts_d0accd["Ports & Contracts (archigram.ports)"]
  end
  n_5_feature_digest_52876d -.->|"imports"| n_4_domain_core_f195b2
  n_5_feature_digest_52876d -->|"composes"| n_4_domain_core_f195b2
  n_9_feature_reducers_8a2858 -.->|"imports"| n_4_domain_core_f195b2
  n_9_feature_reducers_8a2858 -->|"calls"| n_4_domain_core_f195b2
  n_11_ports_contracts_d0accd -.->|"imports"| n_4_domain_core_f195b2
  n_6_feature_emitters_75a4c0 -.->|"imports"| n_4_domain_core_f195b2
  n_6_feature_emitters_75a4c0 -->|"composes"| n_4_domain_core_f195b2
  n_10_mcp_controlplane_ff4d3b -.->|"imports"| n_5_feature_digest_52876d
  n_10_mcp_controlplane_ff4d3b -.->|"imports"| n_6_feature_emitters_75a4c0
  n_3_cli_dataplane_493a5b -->|"calls"| n_7_feature_ingestor_73234e
  n_3_cli_dataplane_493a5b -.->|"imports"| n_7_feature_ingestor_73234e
  n_10_mcp_controlplane_ff4d3b -.->|"imports"| n_8_feature_linters_57c819
  n_10_mcp_controlplane_ff4d3b -->|"calls"| n_8_feature_linters_57c819
  n_10_mcp_controlplane_ff4d3b -.->|"imports"| n_9_feature_reducers_8a2858
  n_3_cli_dataplane_493a5b -->|"calls"| n_10_mcp_controlplane_ff4d3b
  n_3_cli_dataplane_493a5b -.->|"imports"| n_10_mcp_controlplane_ff4d3b
  n_8_feature_linters_57c819 -->|"implements"| n_11_ports_contracts_d0accd
  n_8_feature_linters_57c819 -.->|"imports"| n_11_ports_contracts_d0accd
  n_7_feature_ingestor_73234e -->|"implements"| n_11_ports_contracts_d0accd
  n_7_feature_ingestor_73234e -.->|"imports"| n_11_ports_contracts_d0accd
  n_8_feature_linters_57c819 -->|"composes"| n_11_ports_contracts_d0accd
  n_8_feature_linters_57c819 -->|"calls"| n_11_ports_contracts_d0accd
```
<!-- archigram:end l0_hexagonal_rings_mermaid -->

---

## What Is Intentionally Abstracted Away at L0

- Individual ingestors, reducers, lint gates, and emitters are collapsed into their 5 capability groups (`feature_ingestors`, `feature_reducers`, `feature_linters`, `feature_emitters`, `feature_digest`).
- To see all **16 independent vertical slices** and their exact wiring to entrypoints, ports, and domain, step down to **[L1: Feature-Slice Map](L1-feature-slices.md)**.
- To inspect classes, functions, and runtime calls inside a single capability group, jump straight to **[L2: Subsystem Guides](L2-subsystem-guides.md)** or **[L3: Runtime Traces](L3-runtime-traces.md)**.
