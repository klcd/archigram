# L0: System Overview & Hexagonal Rings

**Best for:** New contributors, CLI/MCP integrators, and reviewers who want the full system mental model in under two minutes without wading into individual slices.

- **Zoom In:** [L1: Feature-Slice Map](L1-feature-slices.md) · [L2: Subsystem Guides](L2-subsystem-guides.md) · [L3: Runtime Traces](L3-runtime-traces.md) · [Styling & Full Multi-Target Gallery](diagram-styling-and-gallery.md)
- **Companion Renders:** [Mermaid](diagrams/l0_hexagonal_rings_mermaid.mmd) · [D2 (`.d2`)](diagrams/l0_hexagonal_rings_d2.d2) · [D2 SVG](diagrams/l0_hexagonal_rings_d2.d2.svg) · [PlantUML (`.puml`)](diagrams/l0_hexagonal_rings_puml.puml) · [LikeC4 (`.c4`)](diagrams/l0_hexagonal_rings_likec4.c4) · [DOT (`.dot`)](diagrams/l0_hexagonal_rings_dot.dot) · [DOT SVG](diagrams/l0_hexagonal_rings_dot.dot.svg)

---

## Hexagonal Dependency Rule

ArchiGram enforces a strict **inward-only** Hexagonal Architecture (`lint-imports` runs on every commit):

```text
archigram.entrypoints  ──▶  archigram.features  ──▶  archigram.ports  ──▶  archigram.domain
   (Driving Adapters)        (16 Vertical Slices)     (Protocol Contracts)   (Zero-I/O Core)
```

1. **`domain_core` (`src/archigram/domain/`)**: Pure Pydantic v2 models (`StructuralGraphIR`, `InteractionTraceIR`, `CurationSpecV1`, `LintReport`, `GraphDigest`), canonical dual-mode visual styling (`style.py`), and Series-Parallel poset math. Zero I/O, zero CLI/MCP/emitter imports.
2. **`ports_contracts` (`src/archigram/ports/`)**: Runtime-checkable Python `Protocol` contracts (`StructuralIngestorPort`, `TraceIngestorPort`, `StructuralReducerPort`, `TraceCompactorPort`, `LintGatePort`, `SyntaxValidatorPort`, `DiagramEmitterPort`, `DigestGeneratorPort`).
3. **Capability Groups (`src/archigram/features/*`)**: Five horizontal capability families implemented as **16 strictly independent vertical slices** (Ingestors, Reducers, 5-Gate Linters, Emitters, Digest). Slices never import each other.
4. **Driving Adapters (`src/archigram/entrypoints/`)**:
   - **Data Plane (`cli_dataplane`)**: Zero-LLM deterministic compiler (`apply`, `lint`, `diff`, `--sync-md`, `self_trace`) safe for CI pipelines and pre-commit hooks.
   - **Control Plane (`mcp_controlplane`)**: FastMCP server (`digest`, `lint`, `preview`) and bounded 3-turn self-healing `propose` loop for LLM-assisted spec authoring.

---

## Curated View — Mermaid Orthogonal Flowchart (`l0_hexagonal_rings_mermaid`)

Proven by Gate 3 invariants in [`diagram-views.yaml`](diagram-views.yaml):
- `forbid_new_cycles: true` (strict DAG across all Hexagonal rings)
- `preserve_reachability`: `entrypoints.cli.pipeline -> ports.contracts -> domain.ir`
- Styled with transparent node/subgraph backgrounds and semantic edge-only `linkStyle` strokes (`#3b82f6` calls, `#64748b` dashed imports, `#a855f7` dashed implements, `#14b8a6` composes):

<!-- archigram:start l0_hexagonal_rings_mermaid -->
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
      n3["CLI Data Plane (entrypoints.cli)"]
      n10["MCP Control Plane (entrypoints.mcp)"]
    end
    subgraph n2["features"]
      n5["Digest Slice (Graph & Trace Digest)"]
      n6["Emitter Slices (Mermaid, D2, PlantUML, LikeC4)"]
      n7["Ingestor Slices (DOT, SCIP, OTLP, Class, Python)"]
      n8["5-Gate Linter Slices (Grounding, Topology, Budget, Syntax)"]
      n9["Reducer Slices (Structural + Trace Compactor)"]
    end
    n4["Domain Core (Canonical IR, Spec, Poset, Diagnostics)"]
    n11["Ports & Contracts (archigram.ports)"]
  end
  style n0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n1 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n2 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n3 -.->|"imports"| n0
  n7 -->|"composes"| n4
  n8 -->|"composes"| n4
  n6 -->|"calls"| n4
  n7 -->|"calls"| n4
  n5 -.->|"imports"| n4
  n5 -->|"composes"| n4
  n9 -.->|"imports"| n4
  n9 -->|"calls"| n4
  n9 -->|"composes"| n4
  n7 -.->|"imports"| n4
  n8 -.->|"imports"| n4
  n8 -->|"calls"| n4
  n6 -.->|"imports"| n4
  n6 -->|"composes"| n4
  n10 -->|"calls"| n5
  n10 -.->|"imports"| n5
  n10 -->|"calls"| n6
  n10 -.->|"imports"| n6
  n3 -->|"calls"| n7
  n3 -.->|"imports"| n7
  n10 -.->|"imports"| n8
  n10 -->|"calls"| n8
  n10 -.->|"imports"| n9
  n10 -->|"calls"| n9
  n3 -->|"calls"| n10
  n3 -.->|"imports"| n10
  n8 -.->|"implements"| n11
  n8 -.->|"imports"| n11
  n7 -.->|"implements"| n11
  n7 -.->|"imports"| n11
  n8 -->|"composes"| n11
  n8 -->|"calls"| n11
  linkStyle 0,5,7,10,11,13,16,18,20,21,23,26,28,30 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
  linkStyle 1,2,6,9,14,31 stroke:#14b8a6,stroke-width:1.75px
  linkStyle 3,4,8,12,15,17,19,22,24,25,32 stroke:#3b82f6,stroke-width:1.75px
  linkStyle 27,29 stroke:#a855f7,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end l0_hexagonal_rings_mermaid -->

---

## Curated View — Rendered D2 Architecture (`l0_hexagonal_rings_d2`)

Compiled from the identical 9-supernode reduction into D2 (`layout-engine: elk`, `theme-id: 0`, `dark-theme-id: 200`, `style.fill: transparent`):

![L0 Hexagonal Rings in D2](diagrams/l0_hexagonal_rings_d2.d2.svg)

---

## What Is Intentionally Abstracted Away at L0

- Individual ingestors, reducers, lint gates, and emitters are collapsed into their 5 capability groups (`feature_ingestors`, `feature_reducers`, `feature_linters`, `feature_emitters`, `feature_digest`).
- To see all **16 independent vertical slices** and their exact wiring to entrypoints, ports, and domain, step down to **[L1: Feature-Slice Map](L1-feature-slices.md)**.
- To inspect classes, functions, and runtime calls inside a single capability group, jump straight to **[L2: Subsystem Guides](L2-subsystem-guides.md)**, **[L3: Runtime Traces](L3-runtime-traces.md)**, or **[Styling & Full Multi-Target Gallery](diagram-styling-and-gallery.md)**.
