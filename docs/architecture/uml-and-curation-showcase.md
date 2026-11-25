# Program-First CLI Curation (3-Stage SVG Triage) & UML 2.5 Diagram Showcase

> **Core Guardrail Demonstrated on This Page:** Every diagram below begins as an **untouched program extraction**—either AST static analysis (`PythonAstStructuralIngestor` over `src/archigram`) or live Python 3.12 `sys.monitoring` execution tracing (`self_trace.py`). An LLM agent is **never allowed to hand-edit diagram source or YAML**; it may only invoke deterministic `archigram curate` CLI commands to delete unnecessary branches (`scope-khop`, `prune-leaves`), splice pass-through barrels (`bypass`), or fold cohesive subsystems (`collapse`, `trace-config`), verified at every step by the **5 ArchiGram Linter Gates**.

- **Navigation:** [Architecture Index](README.md) · [L0: System & Rings](L0-system-and-rings.md) · [L1: Feature Slices](L1-feature-slices.md) · [L2: Subsystem Guides](L2-subsystem-guides.md) · [L3: Runtime Traces](L3-runtime-traces.md) · [Styling Policy & Gallery](diagram-styling-and-gallery.md)
- **CLI-Authored Specs:** [`showcase-structural-views.yaml`](showcase-structural-views.yaml) · [`showcase-trace-views.yaml`](showcase-trace-views.yaml)

---

## 1. Program-First, CLI-Only Curation & 3-Stage Visual Comparison

When a deterministic extractor inspects a real repository or live runtime trace, the raw output routinely exceeds Gitea's **`5,000`-character Mermaid client limit** (`MERMAID_GITEA_MAX_SOURCE_CHARS = 5000` in [`src/archigram/domain/style.py`](../../src/archigram/domain/style.py)):

| Showcase Dataset | Program Extractor | Raw Program Output | Raw Mermaid Size | Curated Output | Curated Mermaid Size |
| --- | --- | --- | --- | --- | --- |
| **Structural AST (`showcase_cli_triage`)** | `PythonAstStructuralIngestor` (`src/archigram`) | `334` symbols, `484` edges | **`50,186` chars** (`10.0×` over Gitea cap) | `17` nodes, `12` edges | **`1,887` chars** (`96.2%` reduction) |
| **Runtime Trace (`showcase_trace_triage`)** | `self_trace.py` (`sys.monitoring`) | `15` participants, `183` spans | **`9,460` chars** (`1.9×` over Gitea cap) | `15` participants, `48` spans | **`3,866` chars** (`59.1%` reduction) |

Because raw Stage 1 (`50,186` and `9,460` chars) and annotated Stage 2 exceed `5,000` characters, `archigram triage` renders **dual-mode transparent D2 SVGs** (`1_original.d2.svg` -> `2_triage.d2.svg` -> `3_curated.d2.svg`) so reviewers can visually inspect **1. Original**, **2. Original + What Will Be Removed**, and **3. Final Curated Result** side by side—and then emits the `< 5,000`-character native Gitea Mermaid block at Stage 3.

### Reproducible CLI-Only Transcript (Zero Hand-Editing)

```bash
# 1. Initialize view and delete out-of-neighborhood slices + degree-0 namespace stubs
uv run archigram curate docs/architecture/showcase-structural-views.yaml \
  -v showcase_cli_triage -t "Showcase: CLI-Curated Compilation Pipeline (d2)" \
  -r d2 --variant architecture
uv run archigram curate scope-khop docs/architecture/showcase-structural-views.yaml \
  -v showcase_cli_triage -f archigram.entrypoints.cli.pipeline --upstream 1 --downstream 1
uv run archigram curate prune-leaves docs/architecture/showcase-structural-views.yaml \
  -v showcase_cli_triage --id prune_ns_stubs \
  -g archigram -g archigram.entrypoints -g archigram.entrypoints.cli -g archigram.features

# 2. Splice pass-through re-export barrels & fold cohesive internal partitions
uv run archigram curate bypass docs/architecture/showcase-structural-views.yaml \
  -v showcase_cli_triage --id splice_reexport_barrels \
  -g archigram.ports -g archigram.ports.contracts -g archigram.domain \
  -g archigram.features.emit_mermaid -g archigram.features.emit_d2 \
  -g archigram.features.emit_plantuml -g archigram.features.emit_likec4 --max-fanout 250
uv run archigram curate collapse docs/architecture/showcase-structural-views.yaml \
  -v showcase_cli_triage --id fold_domain_core --target domain_core \
  --label "Domain Core (Canonical IR, Spec, Diagnostics)" \
  -g "archigram.domain.ir*" -g "archigram.domain.spec*" -g "archigram.domain.diagnostics*"
uv run archigram curate budget docs/architecture/showcase-structural-views.yaml \
  -v showcase_cli_triage --min-nodes 5 --max-nodes 18 --max-edges 40

# 3. Compile 3-stage comparison SVGs (1_original -> 2_triage -> 3_curated) + 5-Gate verification
uv run archigram triage src/archigram \
  --spec docs/architecture/showcase-structural-views.yaml \
  --view showcase_cli_triage \
  --out-dir docs/architecture/diagrams
```

> [!NOTE]
> **How the 5-Gate Linter Policed the LLM During CLI Curation:** When constructing these views via `archigram curate`, **Gate 1 (`ZERO_MATCH_GLOB`)** immediately rejected a guessed `archigram.entrypoints.cli.diff_engine` glob (the real module is `archigram.entrypoints.cli.diff`), **Gate 3 (`DISCONNECTED_PARTITION_MERGE`)** warned when `domain.style` was grouped with disconnected `domain.ir` symbols, and **Gate 4 (`NODE_BUDGET_EXCEEDED`)** guided sub-symbol collapsing from `24` down to `17` nodes (`0` errors, `0` warnings).

---

### Case Study A — Structural AST Curation (`50,186` Raw Mermaid Chars -> `1,887` Chars)

#### Stage 1: Untouched Program Output (`334` Symbols, `484` Dependency Edges)
Extracted directly from `src/archigram` by `PythonAstStructuralIngestor` (`50,186` Mermaid characters—unrenderable in Gitea Mermaid, rendered via SVG):

![Stage 1 — Original Untouched AST Graph (334 nodes, 484 edges)](diagrams/showcase_cli_triage.1_original.d2.svg)

#### Stage 2: Original + Triage Disposition Overlay (`What Will Be Removed / Spliced / Collapsed`)
Every raw symbol and edge in the untouched `334`-node AST is classified by `archigram triage` with dual-mode transparent borders:
- **Emerald Solid (`#10b981`, `[KEEP]`):** `11` focal symbols retained directly in the curated view.
- **Crimson Dashed (`#ef4444`, `[REMOVE]`):** `302` out-of-neighborhood symbols and degree-0 namespace stubs deleted by `scope-khop` and `prune-leaves`.
- **Amber Dashed (`#f59e0b`, `[BYPASS]`):** `1` pass-through contract barrel spliced out while preserving transitive reachability.
- **Violet Dashed (`#8b5cf6`, `[COLLAPSE -> ...]`):** `20` slice internals merged into cohesive supernodes.

![Stage 2 — Triage Overlay Showing Keep, Remove, Bypass, and Collapse](diagrams/showcase_cli_triage.2_triage.d2.svg)

#### Stage 3: Final 5-Gate-Verified Result (`17` Nodes, `12` Edges, `1,887` Mermaid Chars)
Companion D2 SVG ([`showcase_cli_triage.3_curated.d2`](diagrams/showcase_cli_triage.3_curated.d2)):

![Stage 3 — Final Curated Pipeline Architecture SVG](diagrams/showcase_cli_triage.3_curated.d2.svg)

Native Gitea-ready Mermaid (`1,887` chars < `5,000` limit, synced via `archigram apply --sync-md`):

<!-- archigram:start showcase_cli_triage_mermaid -->
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
    subgraph n1["domain"]
      n12["Domain Core (Canonical IR, Spec, Diagnostics)"]
      n13["Canonical Dual-Mode Style Policy (domain.style)"]
    end
    subgraph n2["entrypoints"]
      subgraph n3["cli"]
        n4["pipeline"]
        n11["CLI Commands (app, triage, diff, sync)"]
      end
    end
    subgraph n5["features"]
      n6["emit_d2"]
      n7["emit_likec4"]
      n8["emit_mermaid"]
      n9["emit_plantuml"]
      n14["Ingestor Slices (5 Extractors)"]
      n15["5-Gate Linter Slices"]
      n16["Reducer Slices (Graph & Trace)"]
    end
    n10["ports"]
  end
  style n0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n1 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n2 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n3 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n5 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n3 -.->|"imports"| n11
  n11 -.->|"imports"| n4
  n12 -.->|"imports"| n1
  n4 -.->|"imports"| n13
  n13 -.->|"imports"| n12
  n4 -.->|"imports"| n14
  n4 -.->|"imports"| n15
  n4 -.->|"imports"| n16
  n4 -.->|"imports"| n6
  n4 -.->|"imports"| n7
  n4 -.->|"imports"| n8
  n4 -.->|"imports"| n9
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end showcase_cli_triage_mermaid -->

---

### Case Study B — Live `sys.monitoring` Trace Curation (`9,460` Raw Mermaid Chars -> `3,866` Chars)

#### Stage 1: Untouched Runtime Call Topology (`15` Participants, `183` Raw Spans)
Captured by running ArchiGram's own compiler and 3-turn repair loop under Python 3.12 `sys.monitoring` (`self_trace.py`). Raw sequence emission produces `9,460` Mermaid chars (`1.9×` over Gitea's `5,000`-char cap):

![Stage 1 — Original Untouched Runtime Trace Call Topology](diagrams/showcase_trace_triage.1_original.d2.svg)

#### Stage 2: Original + Trace Compaction Triage (`[KEEP]` vs `[FOLD 183 -> 48 Spans]`)
Applying `archigram curate trace-config --max-depth 2 --fold-loops --fold-parallel` compacts `135` repetitive loop/deep spans (`[FOLD]`, violet dashed `#8b5cf6`) while preserving `48` causal root/repair spans (`[KEEP]`, emerald solid `#10b981`) and happens-before partial order (Gate 3):

![Stage 2 — Runtime Trace Triage Overlay](diagrams/showcase_trace_triage.2_triage.d2.svg)

#### Stage 3: Final Curated Sequence Diagram (`15` Participants, `48` Spans, `3,866` Mermaid Chars)
Companion D2 Sequence SVG with flowing CSS call edges ([`showcase_trace_triage.3_curated.d2`](diagrams/showcase_trace_triage.3_curated.d2)):

![Stage 3 — Curated Runtime Sequence SVG](diagrams/showcase_trace_triage.3_curated.d2.svg)

Native Gitea-ready Mermaid `sequenceDiagram` (`3,866` chars < `5,000` limit):

<!-- archigram:start showcase_trace_triage_mermaid -->
```mermaid
---
config:
  sequence:
    mirrorActors: false
    messageAlign: center
---
sequenceDiagram
  participant p0 as "diagnostics"
  participant p1 as "ir"
  participant p2 as "spec"
  participant p3 as "style"
  participant p4 as "pipeline"
  participant p5 as "propose_loop"
  participant p6 as "generator"
  participant p7 as "emit_d2.emitter"
  participant p8 as "emit_mermaid.emitter"
  participant p9 as "emit_plantuml.emitter"
  participant p10 as "lint_budget.gate"
  participant p11 as "gates"
  participant p12 as "validators"
  participant p13 as "lint_topology.gate"
  participant p14 as "reducer"
  p4->>+p4: compile_and_lint_pipeline
  p4->>+p14: StructuralTopologicalReducer.reduce
  p14-->>-p4: return
  p4->>+p8: MermaidEmitter.__init__
  p8-->>-p4: return
  p4->>+p8: MermaidEmitter.emit
  p8-->>-p4: return
  p4->>+p14: StructuralTopologicalReducer.reduce
  p14-->>-p4: return
  p4->>+p7: D2Emitter.emit
  p7-->>-p4: return
  p4->>+p14: StructuralTopologicalReducer.reduce
  p14-->>-p4: return
  p4->>+p9: PlantUmlEmitter.emit
  p9-->>-p4: return
  p4->>+p11: GroundingAndBoundaryGate.__init__
  p11-->>-p4: return
  p4->>+p11: GroundingAndBoundaryGate.check
  p11-->>-p4: return
  p4->>+p13: TopologicalInvariantGate.__init__
  p13-->>-p4: return
  p4->>+p13: TopologicalInvariantGate.check
  p13-->>-p4: return
  p4->>+p10: CognitiveBudgetAndOrphanGate.check
  p10-->>-p4: return
  p4->>+p12: build_default_validators
  p12-->>-p4: return
  p4->>+p12: Gate5SyntaxHarness.__init__
  p12-->>-p4: return
  p4->>+p12: Gate5SyntaxHarness.validate #91;loop x3#93;
  p12-->>-p4: return
  p4->>+p0: LintReport.from_diagnostics
  p0-->>-p4: return
  p4-->>-p4: return
  p5->>+p5: run_propose_loop
  p5->>+p6: GraphDigestGenerator.to_compact_markdown
  p6-->>-p5: return
  p5->>+p2: parse_curation_spec_yaml
  p2-->>-p5: return
  p5->>+p11: GroundingAndBoundaryGate.check #91;loop x2#93;
  p11-->>-p5: return
  p5->>+p13: TopologicalInvariantGate.check
  p13-->>-p5: return
  p5->>+p0: LintReport.from_diagnostics
  p0-->>-p5: return
  p5->>+p0: LintReport._validate_passed_invariant
  p0-->>-p5: return
  p5->>+p2: parse_curation_spec_yaml
  p2-->>-p5: return
  p5->>+p11: collect_observed_symbols
  p11-->>-p5: return
  p5->>+p2: DiagramViewSpec._validate_unique_view_rules
  p2-->>-p5: return
  p5->>+p2: CurationSpecV1._validate_spec_uniqueness
  p2-->>-p5: return
  p5->>+p11: GroundingAndBoundaryGate.check
  p11-->>-p5: return
  p5->>+p14: StructuralTopologicalReducer.reduce
  p14-->>-p5: return
  p5->>+p11: GroundingAndBoundaryGate.check
  p11-->>-p5: return
  p5->>+p13: TopologicalInvariantGate.check
  p13-->>-p5: return
  p5->>+p10: CognitiveBudgetAndOrphanGate.check
  p10-->>-p5: return
  p5->>+p8: MermaidEmitter.emit
  p8-->>-p5: return
  p5->>+p12: Gate5SyntaxHarness.validate
  p12-->>-p5: return
  p5->>+p0: LintReport.from_diagnostics
  p0-->>-p5: return
  p5->>+p0: LintReport._validate_passed_invariant
  p0-->>-p5: return
  p5->>+p2: dump_curation_spec_yaml
  p2-->>-p5: return
  p5->>+p2: parse_curation_spec_yaml
  p2-->>-p5: return
  p5->>+p11: GroundingAndBoundaryGate.check
  p11-->>-p5: return
  p5->>+p14: StructuralTopologicalReducer.reduce
  p14-->>-p5: return
  p5->>+p11: GroundingAndBoundaryGate.check
  p11-->>-p5: return
  p5->>+p13: TopologicalInvariantGate.check
  p13-->>-p5: return
  p5->>+p10: CognitiveBudgetAndOrphanGate.check
  p10-->>-p5: return
  p5->>+p8: MermaidEmitter.emit
  p8-->>-p5: return
  p5->>+p12: Gate5SyntaxHarness.validate
  p12-->>-p5: return
  p5->>+p0: LintReport.from_diagnostics
  p0-->>-p5: return
  p5->>+p0: LintReport._validate_passed_invariant
  p0-->>-p5: return
  p5->>+p2: dump_curation_spec_yaml
  p2-->>-p5: return
  p5-->>-p5: return
```
<!-- archigram:end showcase_trace_triage_mermaid -->

---

## 2. UML 2.5 Diagram Taxonomy Coverage (14-Type Reference Mapping)

Inspired by the standard [14 UML 2.5 Diagram Types taxonomy](https://creately.com/blog/diagrams/uml-diagram-types-examples/), ArchiGram compiles both **Structural** and **Behavioral** UML projections deterministically from its two Canonical IR profiles (`StructuralGraphIR` and `InteractionTraceIR`)—without adding speculative IR noise or hand-written layout code:

| Category | UML 2.5 Diagram Type | ArchiGram Support & `diagram_variant` | Canonical IR Source | Live Self-Hosted Example |
| --- | --- | --- | --- | --- |
| **Structural** | **1. Class Diagram** | Native (`auto` OOP / `class`) across Mermaid, D2, PlantUML | `StructuralGraphIR` (`CLASS`, `INTERFACE`, `ENUM`) | [L2c OOP Validator Class Diagrams](diagram-styling-and-gallery.md#part-b--oop-uml-class-diagrams-all-3-oop-renderers-on-l2c-validators) |
| **Structural** | **2. Component Diagram** | Native (`architecture`) across Mermaid, D2, PlantUML, LikeC4, DOT | `StructuralGraphIR` (`SERVICE`, `MODULE`, `COMPONENT`) | [L0 Hexagonal Component Views](L0-system-and-rings.md) |
| **Structural** | **3. Package Diagram** | **`diagram_variant: package`** (`<<package>>` + dashed `<<import>>`) | `StructuralGraphIR` | [Section 3.1 below](#31-uml-package-diagram-diagram_variant-package) |
| **Structural** | **4. Deployment Diagram** | **`diagram_variant: deployment`** (`<<executionEnv>>` 3D nodes + `<<artifact>>`) | `StructuralGraphIR` | [Section 3.2 below](#32-uml-deployment-diagram-diagram_variant-deployment) |
| **Structural** | **5. Composite Structure** | Hierarchical nested ELK port/container boundaries (`architecture`) | `StructuralGraphIR` (`parent_id` containment forest) | [L1 Feature-Slice Containment Map](L1-feature-slices.md) |
| **Structural** | **6. Object Diagram** | Concrete runtime participant instance topology (`Stage 1` snapshot / `communication`) | `InteractionTraceIR` / `StructuralGraphIR` | [Stage 1 Instance Topology above](#stage-1-untouched-runtime-call-topology-15-participants-183-raw-spans) |
| **Structural** | **7. Profile Diagram** | Stereotype mapping via `<<interface>>`, `<<package>>`, `<<executionEnv>>`, `<<actor>>` | `IRNode.kind` & `attributes` | Applied across Sections 3.1–3.3 |
| **Behavioral** | **8. Use Case Diagram** | **`diagram_variant: use_case`** (`<<actor>>` outside `System Subject Boundary` + ovals) | `StructuralGraphIR` (driving roots -> system capabilities) | [Section 3.3 below](#33-uml-use-case-diagram-diagram_variant-use_case) |
| **Behavioral** | **9. Sequence Diagram** | Native (`auto` on `interaction_trace`) across all 5 renderers | `InteractionTraceIR` (`R1–R6` + SP-Poset) | [L3 Runtime Sequence Views](L3-runtime-traces.md) |
| **Behavioral** | **10. Communication Diagram** | **`diagram_variant: communication`** (numbered `1`, `1.1`, `2 [xN]` channels) | `InteractionTraceIR` | [Section 3.4 below](#34-uml-communication-diagram-diagram_variant-communication) |
| **Behavioral** | **11. Activity Diagram** | **`diagram_variant: activity`** (start/stop bullseyes, loop diamonds, fork/join bars) | `InteractionTraceIR` / `StructuralGraphIR` | [Section 3.5 below](#35-uml-activity-diagram-diagram_variant-activity) |
| **Behavioral** | **12. State Machine Diagram** | **`diagram_variant: state`** (`[*] -> State` lifecycle transitions & self-healing loops) | `InteractionTraceIR` / `StructuralGraphIR` | [Section 3.6 below](#36-uml-state-machine-diagram-diagram_variant-state) |
| **Behavioral** | **13. Interaction Overview** | Multi-wave causal step frames (`diagram_variant: steps` animated D2 multiboard) | `InteractionTraceIR` | [L3 Animated Steps SVG](L3-runtime-traces.md) |
| **Behavioral** | **14. Timing Diagram** | Nanosecond `start_ns` / `end_ns` / `clock_domain_id` span annotations in compacted traces | `InteractionTraceIR` (`clock_domain_id`) | [L3 Trace Poset Diagnostics](L3-runtime-traces.md) |

---

## 3. Live Self-Hosted UML 2.5 Showcase (Compiled from ArchiGram IR)

Every diagram below is compiled from live `src/archigram` AST or `traces/pipeline-and-repair.ir.json` with **transparent dual-mode fills**, **orthogonal routing**, and **`< 5,000`-char Gitea-verified Mermaid**. Companion PlantUML sources are also emitted in [`docs/architecture/diagrams/`](diagrams/).

---

### 3.1 UML Package Diagram (`diagram_variant: package`)

Projects ArchiGram's 9 hexagonal capability groups as explicit `<<package>>` folders with dashed `<<import>>` / `<<access>>` dependency arrows ([`showcase_uml_package_d2.d2`](diagrams/showcase_uml_package_d2.d2) · [`showcase_uml_package_plantuml.puml`](diagrams/showcase_uml_package_plantuml.puml)):

![UML 2.5 Package Diagram (D2 SVG)](diagrams/showcase_uml_package_d2.d2.svg)

<!-- archigram:start showcase_uml_package_mermaid -->
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
  subgraph n0["<<package>> archigram"]
    subgraph n1["<<package>> entrypoints"]
      n3["<<package>> CLI Data Plane (entrypoints.cli)"]
      n10["<<package>> MCP Control Plane (entrypoints.mcp)"]
    end
    subgraph n2["<<package>> features"]
      n5["<<package>> Digest Slice (Graph & Trace)"]
      n6["<<package>> Emitter Slices (Mermaid, D2, PlantUML, LikeC4)"]
      n7["<<package>> Ingestor Slices (5 Extractors)"]
      n8["<<package>> 5-Gate Linter Slices"]
      n9["<<package>> Reducer Slices (Graph & Trace)"]
    end
    n4["<<package>> Domain Core (Canonical IR, Spec, Style)"]
    n11["<<package>> Ports & Contracts (archigram.ports)"]
  end
  style n0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n1 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n2 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n10 -.->|"<<import>>"| n5
  n5 -.->|"<<import>>"| n4
  n9 -.->|"<<import>>"| n4
  n9 -.->|"<<import>>"| n4
  n5 -.->|"<<import>>"| n4
  n11 -.->|"<<import>>"| n4
  n9 -.->|"<<import>>"| n4
  n6 -.->|"<<import>>"| n4
  n6 -.->|"<<import>>"| n4
  n10 -.->|"<<import>>"| n6
  n3 -.->|"<<import>>"| n7
  n3 -.->|"<<import>>"| n7
  n10 -.->|"<<import>>"| n8
  n10 -.->|"<<import>>"| n8
  n3 -.->|"<<import>>"| n10
  n3 -.->|"<<import>>"| n10
  n8 -.->|"<<import>>"| n11
  n7 -.->|"<<import>>"| n11
  n8 -.->|"<<import>>"| n11
  n8 -.->|"<<import>>"| n11
  n7 -.->|"<<import>>"| n11
  n8 -.->|"<<import>>"| n11
  n10 -.->|"<<import>>"| n9
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22 stroke:#64748b,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end showcase_uml_package_mermaid -->

---

### 3.2 UML Deployment Diagram (`diagram_variant: deployment`)

Maps driving runtime environments (`CLI Data Plane`, `MCP Control Plane`) as 3D `<<executionEnv>>` nodes hosting deployed `<<artifact>>` slices ([`showcase_uml_deployment_d2.d2`](diagrams/showcase_uml_deployment_d2.d2) · [`showcase_uml_deployment_plantuml.puml`](diagrams/showcase_uml_deployment_plantuml.puml)):

![UML 2.5 Deployment Diagram (D2 SVG)](diagrams/showcase_uml_deployment_d2.d2.svg)

<!-- archigram:start showcase_uml_deployment_mermaid -->
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
flowchart LR
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  subgraph n0["<<executionEnv>> archigram"]
    subgraph n1["<<executionEnv>> entrypoints"]
      n3["<<artifact>> CLI Data Plane (entrypoints.cli)"]
      n10["<<artifact>> MCP Control Plane (entrypoints.mcp)"]
    end
    subgraph n2["<<executionEnv>> features"]
      n5["<<artifact>> Digest Slice (Graph & Trace)"]
      n6["<<artifact>> Emitter Slices (Mermaid, D2, PlantUML, LikeC4)"]
      n7["<<artifact>> Ingestor Slices (5 Extractors)"]
      n8["<<artifact>> 5-Gate Linter Slices"]
      n9["<<artifact>> Reducer Slices (Graph & Trace)"]
    end
    n4["<<artifact>> Domain Core (Canonical IR, Spec, Style)"]
    n11["<<artifact>> Ports & Contracts (archigram.ports)"]
  end
  style n0 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n1 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  style n2 fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  n10 -.->|"<<deploy>>"| n5
  n5 -.->|"<<deploy>>"| n4
  n9 -.->|"<<deploy>>"| n4
  n5 -.->|"<<deploy>>"| n4
  n6 -.->|"<<deploy>>"| n4
  n9 -.->|"<<deploy>>"| n4
  n6 -.->|"<<deploy>>"| n4
  n9 -.->|"<<deploy>>"| n4
  n11 -.->|"<<deploy>>"| n4
  n10 -.->|"<<deploy>>"| n6
  n3 -.->|"<<deploy>>"| n7
  n3 -.->|"<<deploy>>"| n7
  n10 -.->|"<<deploy>>"| n8
  n10 -.->|"<<deploy>>"| n8
  n3 -.->|"<<deploy>>"| n10
  n3 -.->|"<<deploy>>"| n10
  n8 -.->|"<<deploy>>"| n11
  n8 -.->|"<<deploy>>"| n11
  n7 -.->|"<<deploy>>"| n11
  n8 -.->|"<<deploy>>"| n11
  n7 -.->|"<<deploy>>"| n11
  n8 -.->|"<<deploy>>"| n11
  n10 -.->|"<<deploy>>"| n9
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22 stroke:#3b82f6,stroke-width:1.75px,stroke-dasharray:5 5
```
<!-- archigram:end showcase_uml_deployment_mermaid -->

---

### 3.3 UML Use Case Diagram (`diagram_variant: use_case`)

Separates zero-indegree driving actors (`CLI Developer / CI Hook`, `LLM Agent Client (MCP)`) outside the **`System Subject Boundary`** from stadium-oval system use cases inside the boundary ([`showcase_uml_use_case_d2.d2`](diagrams/showcase_uml_use_case_d2.d2) · [`showcase_uml_use_case_plantuml.puml`](diagrams/showcase_uml_use_case_plantuml.puml)):

![UML 2.5 Use Case Diagram (D2 SVG)](diagrams/showcase_uml_use_case_d2.d2.svg)

<!-- archigram:start showcase_uml_use_case_mermaid -->
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
flowchart LR
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  u0["<<actor>> CLI Developer / CI Hook"]
  u1["<<actor>> LLM Agent Client (MCP)"]
  u3["<<actor>> entrypoints"]
  u4["<<actor>> cli"]
  u5["<<actor>> mcp"]
  subgraph sys_boundary["System Subject Boundary"]
    u2(["archigram"])
    u6(["features"])
    u7(["Compile Views, Diff & Sync Markdown"])
    u8(["Curate Specs & Render 3-Stage Triage"])
    u9(["Validate Canonical IR, Spec & Port Contracts"])
    u10(["Emit Dual-Mode UML & Architecture Views"])
    u11(["Ingest Factual AST / SCIP / OTLP IR"])
    u12(["Propose & Self-Heal Spec (3-Turn Loop)"])
    u13(["Reduce Topology & Compact R1-R6 Traces"])
    u14(["Verify 5 Mathematical Linter Gates"])
  end
  style sys_boundary fill:transparent,stroke:#64748b,stroke-width:1px,stroke-dasharray:4 4
  u4 -->|"invokes"| u0
  u5 -->|"invokes"| u1
  u8 -.->|"<<include>>"| u7
  u8 -.->|"<<include>>"| u7
  u8 -.->|"<<include>>"| u7
  u0 -->|"invokes"| u8
  u0 -->|"invokes"| u8
  u14 -.->|"<<include>>"| u9
  u10 -.->|"<<include>>"| u9
  u13 -.->|"<<include>>"| u9
  u14 -.->|"<<include>>"| u9
  u14 -.->|"<<include>>"| u9
  u14 -.->|"<<include>>"| u9
  u11 -.->|"<<include>>"| u9
  u11 -.->|"<<include>>"| u9
  u11 -.->|"<<include>>"| u9
  u11 -.->|"<<include>>"| u9
  u13 -.->|"<<include>>"| u9
  u10 -.->|"<<include>>"| u9
  u13 -.->|"<<include>>"| u9
  u7 -.->|"<<include>>"| u10
  u7 -.->|"<<include>>"| u10
  u12 -.->|"<<include>>"| u10
  u7 -.->|"<<include>>"| u11
  u12 -.->|"<<include>>"| u14
  u7 -.->|"<<include>>"| u14
  u7 -.->|"<<include>>"| u14
  u12 -.->|"<<include>>"| u14
  u1 -->|"invokes"| u12
  u1 -->|"invokes"| u12
  u0 -->|"invokes"| u12
  u0 -->|"invokes"| u12
  u7 -.->|"<<include>>"| u13
  u7 -.->|"<<include>>"| u13
  u12 -.->|"<<include>>"| u13
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34 stroke:#3b82f6,stroke-width:1.75px
```
<!-- archigram:end showcase_uml_use_case_mermaid -->

---

### 3.4 UML Communication Diagram (`diagram_variant: communication`)

Collapses timeline lifelines from `pipeline-and-repair.ir.json` into an orthogonal structural network annotated with hierarchical UML sequence numbers (`1`, `1.1`, `2: validate()`) ([`showcase_uml_communication_d2.d2`](diagrams/showcase_uml_communication_d2.d2) · [`showcase_uml_communication_plantuml.puml`](diagrams/showcase_uml_communication_plantuml.puml)):

![UML 2.5 Communication Diagram (D2 SVG)](diagrams/showcase_uml_communication_d2.d2.svg)

<!-- archigram:start showcase_uml_communication_mermaid -->
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
flowchart LR
  classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
  o0[":diagnostics"]
  o1[":ir"]
  o2[":spec"]
  o3[":style"]
  o4[":pipeline"]
  o5[":propose_loop"]
  o6[":generator"]
  o7[":emit_d2.emitter"]
  o8[":emit_mermaid.emitter"]
  o9[":emit_plantuml.emitter"]
  o10[":lint_budget.gate"]
  o11[":gates"]
  o12[":validators"]
  o13[":lint_topology.gate"]
  o14[":reducer"]
  o4 -->|"1: compile_and_lint_pipeline"| o4
  o4 -->|"1.1: StructuralTopologicalReducer.reduce, 1.4: StructuralTopologicalReducer.reduce, 1.6: StructuralTopologicalReducer.reduce"| o14
  o4 -->|"1.2: MermaidEmitter.__init__, 1.3: MermaidEmitter.emit"| o8
  o4 -->|"1.5: D2Emitter.emit"| o7
  o4 -->|"1.7: PlantUmlEmitter.emit"| o9
  o4 -->|"1.8: GroundingAndBoundaryGate.__init__, 1.9: GroundingAndBoundaryGate.check"| o11
  o4 -->|"1.10: TopologicalInvariantGate.__init__, 1.11: TopologicalInvariantGate.check"| o13
  o4 -->|"1.12: CognitiveBudgetAndOrphanGate.check"| o10
  o4 -->|"1.13: build_default_validators, 1.14: Gate5SyntaxHarness.__init__, 1.15: Gate5SyntaxHarness.validate (loop x3"| o12
  o4 -->|"1.16: LintReport.from_diagnostics"| o0
  o5 -->|"2: run_propose_loop"| o5
  o5 -->|"2.1: GraphDigestGenerator.to_compact_mark"| o6
  o5 -->|"2.2: parse_curation_spec_yaml, 2.7: parse_curation_spec_yaml, 2.9: DiagramViewSpec._validate_unique_vie (+4)"| o2
  o5 -->|"2.3: GroundingAndBoundaryGate.check (loop, 2.8: collect_observed_symbols, 2.11: GroundingAndBoundaryGate.check (+3)"| o11
  o5 -->|"2.4: TopologicalInvariantGate.check, 2.14: TopologicalInvariantGate.check, 2.25: TopologicalInvariantGate.check"| o13
  o5 -->|"2.5: LintReport.from_diagnostics, 2.6: LintReport._validate_passed_invarian, 2.18: LintReport.from_diagnostics (+3)"| o0
  o5 -->|"2.12: StructuralTopologicalReducer.reduce, 2.23: StructuralTopologicalReducer.reduce"| o14
  o5 -->|"2.15: CognitiveBudgetAndOrphanGate.check, 2.26: CognitiveBudgetAndOrphanGate.check"| o10
  o5 -->|"2.16: MermaidEmitter.emit, 2.27: MermaidEmitter.emit"| o8
  o5 -->|"2.17: Gate5SyntaxHarness.validate, 2.28: Gate5SyntaxHarness.validate"| o12
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19 stroke:#3b82f6,stroke-width:1.75px
```
<!-- archigram:end showcase_uml_communication_mermaid -->

---

### 3.5 UML Activity Diagram (`diagram_variant: activity`)

Lowers compacted runtime trace events into an action-flow graph with initial (`●`) and final (`◉`) nodes, diamond loop guards (`loop xN`), and parallel fork/join synchronization bars ([`showcase_uml_activity_d2.d2`](diagrams/showcase_uml_activity_d2.d2) · [`showcase_uml_activity_plantuml.puml`](diagrams/showcase_uml_activity_plantuml.puml)):

![UML 2.5 Activity Diagram (D2 SVG)](diagrams/showcase_uml_activity_d2.d2.svg)

<!-- archigram:start showcase_uml_activity_mermaid -->
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
  act_start((" "))
  a0("pipeline: compile_and_lint_pipeline")
  a1("reducer: StructuralTopologicalReducer.reduce")
  a2("emit_mermaid.emitter: MermaidEmitter.__init__")
  a3("emit_mermaid.emitter: MermaidEmitter.emit")
  a4("reducer: StructuralTopologicalReducer.reduce")
  a5("emit_d2.emitter: D2Emitter.emit")
  a6("reducer: StructuralTopologicalReducer.reduce")
  a7("emit_plantuml.emitter: PlantUmlEmitter.emit")
  a8("gates: GroundingAndBoundaryGate.__init__")
  a9("gates: GroundingAndBoundaryGate.check")
  a10("lint_topology.gate: TopologicalInvariantGate.__init__")
  a11("lint_topology.gate: TopologicalInvariantGate.check")
  a12("lint_budget.gate: CognitiveBudgetAndOrphanGate.check")
  a13("validators: build_default_validators")
  a14("validators: Gate5SyntaxHarness.__init__")
  g15{"loop x3"}
  a15("validators: Gate5SyntaxHarness.validate (loop x3")
  a16("diagnostics: LintReport.from_diagnostics")
  a17("propose_loop: run_propose_loop")
  a_more("+31 compacted actions")
  act_stop(((" ")))
  act_start --> a0
  a0 --> a1
  a1 --> a2
  a2 --> a3
  a3 --> a4
  a4 --> a5
  a5 --> a6
  a6 --> a7
  a7 --> a8
  a8 --> a9
  a9 --> a10
  a10 --> a11
  a11 --> a12
  a12 --> a13
  a13 --> a14
  a14 --> g15
  g15 --> a15
  a15 --> a16
  a16 --> a17
  a17 --> a_more
  a_more --> act_stop
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20 stroke:#3b82f6,stroke-width:1.75px
```
<!-- archigram:end showcase_uml_activity_mermaid -->

---

### 3.6 UML State Machine Diagram (`diagram_variant: state`)

Projects active control-flow transfers across participants as a deterministic `stateDiagram-v2` / rounded state machine (`[*] --> ActiveComponent` transitions triggered by runtime calls and self-healing repair turns) ([`showcase_uml_state_d2.d2`](diagrams/showcase_uml_state_d2.d2) · [`showcase_uml_state_plantuml.puml`](diagrams/showcase_uml_state_plantuml.puml)):

![UML 2.5 State Machine Diagram (D2 SVG)](diagrams/showcase_uml_state_d2.d2.svg)

<!-- archigram:start showcase_uml_state_mermaid -->
```mermaid
---
config:
  layout: elk
---
stateDiagram-v2
  direction TB
  state "diagnostics" as s0
  state "ir" as s1
  state "spec" as s2
  state "style" as s3
  state "pipeline" as s4
  state "propose_loop" as s5
  state "generator" as s6
  state "emit_d2.emitter" as s7
  state "emit_mermaid.emitter" as s8
  state "emit_plantuml.emitter" as s9
  state "lint_budget.gate" as s10
  state "gates" as s11
  state "validators" as s12
  state "lint_topology.gate" as s13
  state "reducer" as s14
  [*] --> s4
  s4 --> s4 : compile_and_lint_pipeline
  s4 --> s14 : StructuralTopologicalReducer.reduce
  s4 --> s8 : MermaidEmitter.__init__
  s4 --> s7 : D2Emitter.emit
  s4 --> s9 : PlantUmlEmitter.emit
  s4 --> s11 : GroundingAndBoundaryGate.__init__
  s4 --> s13 : TopologicalInvariantGate.__init__
  s4 --> s10 : CognitiveBudgetAndOrphanGate.check
  s4 --> s12 : build_default_validators
  s4 --> s0 : LintReport.from_diagnostics
  s5 --> s5 : run_propose_loop
  s5 --> s6 : GraphDigestGenerator.to_compact_mark
  s5 --> s2 : parse_curation_spec_yaml
  s5 --> s11 : GroundingAndBoundaryGate.check loop
  s5 --> s13 : TopologicalInvariantGate.check
  s5 --> s0 : LintReport.from_diagnostics
  s5 --> s14 : StructuralTopologicalReducer.reduce
  s5 --> s10 : CognitiveBudgetAndOrphanGate.check
  s5 --> s8 : MermaidEmitter.emit
  s5 --> s12 : Gate5SyntaxHarness.validate
  s2 --> [*]
```
<!-- archigram:end showcase_uml_state_mermaid -->
