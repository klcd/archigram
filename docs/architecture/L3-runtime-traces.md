# L3: Runtime Execution & Self-Healing Traces

**Best for:** Contributors debugging pipeline orchestration, tracing Gate 1–5 evaluation order, or understanding how the bounded 3-turn MCP `propose` repair loop turns lint diagnostics into passing specs.

- **Zoom Out:** [L0: System & Rings](L0-system-and-rings.md) · [L1: Feature Slices](L1-feature-slices.md) · [L2: Subsystem Guides](L2-subsystem-guides.md) · [Styling & Full Multi-Target Gallery](diagram-styling-and-gallery.md)
- **Source Spec & Snapshot:** [`trace-views.yaml`](trace-views.yaml) · [`traces/pipeline-and-repair.ir.json`](traces/pipeline-and-repair.ir.json)
- **Companion Renders:** [Mermaid](diagrams/l3_runtime_pipeline_mermaid.mmd) · [D2 Sequence (`.d2`)](diagrams/l3_runtime_pipeline_d2.d2) · [D2 Sequence SVG](diagrams/l3_runtime_pipeline_d2.d2.svg) · [D2 Animated Steps (`.d2`)](diagrams/l3_runtime_pipeline_steps_d2.d2) · [D2 Animated Steps SVG](diagrams/l3_runtime_pipeline_steps_d2.d2.svg) · [PlantUML (`.puml`)](diagrams/l3_runtime_pipeline_puml.puml) · [LikeC4 (`.c4`)](diagrams/l3_runtime_pipeline_likec4.c4) · [DOT (`.dot`)](diagrams/l3_runtime_pipeline_dot.dot) · [DOT SVG](diagrams/l3_runtime_pipeline_dot.dot.svg)

---

## How the Live Runtime Trace Is Captured

Rather than hand-writing a sequence diagram that drifts out of date, ArchiGram executes itself under Python 3.12+ `sys.monitoring` via [`src/archigram/entrypoints/cli/self_trace.py`](../../src/archigram/entrypoints/cli/self_trace.py):

```bash
just trace   # or: uv run python -m archigram.entrypoints.cli.self_trace
```

Inside a single `capture_python_trace(...)` session targeting `archigram.*`, the workload executes two real end-to-end flows:

1. **Multi-View Deterministic Pipeline (`compile_and_lint_pipeline`)**:
   - Reduces a real architectural graph across 3 views (`MermaidEmitter`, `D2Emitter`, `PlantUmlEmitter`).
   - Runs all 5 Gates in order: `GroundingAndBoundaryGate` (Gates 1–2) -> `TopologicalInvariantGate` (Gate 3) -> `CognitiveBudgetAndOrphanGate` (Gate 4) -> `Gate5SyntaxHarness` (Gate 5, folded `[loop x3]` across the 3 emitted views) -> `LintReport.from_diagnostics`.
2. **Bounded 3-Turn Self-Healing Repair Loop (`run_propose_loop`)**:
   - Generates a compact token-efficient digest via `GraphDigestGenerator.to_compact_markdown`.
   - **Turn 1 (Rejected):** Submits a flawed YAML candidate with a hallucinated symbol ID; Gates 1–3 fail and return structured diagnostics + repair hints.
   - **Turn 2 (Repaired -> Budget Warn/Refined):** Fixes grounding using observed symbols and exercises reduction + `MermaidEmitter.emit` + `Gate5SyntaxHarness.validate`, then dumps repaired YAML.
   - **Turn 3 (Passed):** Re-verifies the repaired `CurationSpecV1` across all 5 Gates with `0` errors and exits cleanly.

Wall-clock jitter is replaced with a deterministic monotonic 5 ms virtual clock grid while preserving exact live call topology, parent-child nesting, and repo-relative `OBSERVED` provenance stamps (`src/archigram/...`).

---

## Compaction Pipeline (`R1–R6` + Series-Parallel Poset Lowering)

Raw execution captures **180 observed events** across **15 participants**. Applying [`trace-views.yaml`](trace-views.yaml) (`max_call_depth: 2`, `fold_loops: true`, `decompose_series_parallel: true`, `event_budget_max: 60`) compacts that raw trace into **48 legible interactions** (`0` lint errors, `0` warnings across all 5 Gates):

- **Depth Capping (`R2`)**: Clips internal helper frames below depth 2 so module-level architectural handoffs stay front and center.
- **Contiguous Loop Folding (`R4`)**: Collapses repeated calls such as the 3 consecutive `Gate5SyntaxHarness.validate` invocations into a single annotated `[loop x3]` interaction.
- **Series-Parallel Poset Lowering (`R6`)**: Preserves causal happens-before order and lifts concurrent spans into structured `par` / `seq` blocks without losing backing event provenance.

---

## Curated View — Mermaid Sequence Diagram (`l3_runtime_pipeline_mermaid`)

<!-- archigram:start l3_runtime_pipeline_mermaid -->
```mermaid
---
config:
  themeVariables:
    actorBkg: transparent
    actorBorder: '#64748b'
    actorLineColor: '#64748b'
    signalColor: '#3b82f6'
    activationBkgColor: transparent
    activationBorderColor: '#64748b'
    noteBkgColor: transparent
    noteBorderColor: '#64748b'
---
sequenceDiagram
  participant p_0_archigram_domain_71c3aa as "diagnostics"
  participant p_1_archigram_domain_ae7fc5 as "ir"
  participant p_2_archigram_domain_498ecc as "spec"
  participant p_3_archigram_domain_baf783 as "style"
  participant p_4_archigram_entryp_8024d0 as "pipeline"
  participant p_5_archigram_entryp_a3efe2 as "propose_loop"
  participant p_6_archigram_featur_603002 as "generator"
  participant p_7_archigram_featur_65f494 as "emit_d2.emitter"
  participant p_8_archigram_featur_4d345c as "emit_mermaid.emitter"
  participant p_9_archigram_featur_ddda4c as "emit_plantuml.emitter"
  participant p_10_archigram_featur_9a2480 as "lint_budget.gate"
  participant p_11_archigram_featur_d444ee as "gates"
  participant p_12_archigram_featur_d6e8be as "validators"
  participant p_13_archigram_featur_2a23e8 as "lint_topology.gate"
  participant p_14_archigram_featur_df5fa8 as "reducer"
  p_4_archigram_entryp_8024d0->>+p_4_archigram_entryp_8024d0: compile_and_lint_pipeline
  p_4_archigram_entryp_8024d0->>+p_14_archigram_featur_df5fa8: StructuralTopologicalReducer.reduce
  p_14_archigram_featur_df5fa8-->>-p_4_archigram_entryp_8024d0: return StructuralTopologicalReducer.reduce
  p_4_archigram_entryp_8024d0->>+p_8_archigram_featur_4d345c: MermaidEmitter.emit
  p_8_archigram_featur_4d345c-->>-p_4_archigram_entryp_8024d0: return MermaidEmitter.emit
  p_4_archigram_entryp_8024d0->>+p_14_archigram_featur_df5fa8: StructuralTopologicalReducer.reduce
  p_14_archigram_featur_df5fa8-->>-p_4_archigram_entryp_8024d0: return StructuralTopologicalReducer.reduce
  p_4_archigram_entryp_8024d0->>+p_7_archigram_featur_65f494: D2Emitter.emit
  p_7_archigram_featur_65f494-->>-p_4_archigram_entryp_8024d0: return D2Emitter.emit
  p_4_archigram_entryp_8024d0->>+p_14_archigram_featur_df5fa8: StructuralTopologicalReducer.reduce
  p_14_archigram_featur_df5fa8-->>-p_4_archigram_entryp_8024d0: return StructuralTopologicalReducer.reduce
  p_4_archigram_entryp_8024d0->>+p_9_archigram_featur_ddda4c: PlantUmlEmitter.emit
  p_9_archigram_featur_ddda4c-->>-p_4_archigram_entryp_8024d0: return PlantUmlEmitter.emit
  p_4_archigram_entryp_8024d0->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.__init__
  p_11_archigram_featur_d444ee-->>-p_4_archigram_entryp_8024d0: return GroundingAndBoundaryGate.__init__
  p_4_archigram_entryp_8024d0->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.check
  p_11_archigram_featur_d444ee-->>-p_4_archigram_entryp_8024d0: return GroundingAndBoundaryGate.check
  p_4_archigram_entryp_8024d0->>+p_13_archigram_featur_2a23e8: TopologicalInvariantGate.__init__
  p_13_archigram_featur_2a23e8-->>-p_4_archigram_entryp_8024d0: return TopologicalInvariantGate.__init__
  p_4_archigram_entryp_8024d0->>+p_13_archigram_featur_2a23e8: TopologicalInvariantGate.check
  p_13_archigram_featur_2a23e8-->>-p_4_archigram_entryp_8024d0: return TopologicalInvariantGate.check
  p_4_archigram_entryp_8024d0->>+p_10_archigram_featur_9a2480: CognitiveBudgetAndOrphanGate.check
  p_10_archigram_featur_9a2480-->>-p_4_archigram_entryp_8024d0: return CognitiveBudgetAndOrphanGate.check
  p_4_archigram_entryp_8024d0->>+p_12_archigram_featur_d6e8be: build_default_validators
  p_12_archigram_featur_d6e8be-->>-p_4_archigram_entryp_8024d0: return build_default_validators
  p_4_archigram_entryp_8024d0->>+p_12_archigram_featur_d6e8be: Gate5SyntaxHarness.__init__
  p_12_archigram_featur_d6e8be-->>-p_4_archigram_entryp_8024d0: return Gate5SyntaxHarness.__init__
  p_4_archigram_entryp_8024d0->>+p_12_archigram_featur_d6e8be: Gate5SyntaxHarness.validate #91;loop x3#93;
  p_12_archigram_featur_d6e8be-->>-p_4_archigram_entryp_8024d0: return Gate5SyntaxHarness.validate #91;loop x3#93;
  p_4_archigram_entryp_8024d0->>+p_0_archigram_domain_71c3aa: LintReport.from_diagnostics
  p_0_archigram_domain_71c3aa-->>-p_4_archigram_entryp_8024d0: return LintReport.from_diagnostics
  p_4_archigram_entryp_8024d0-->>-p_4_archigram_entryp_8024d0: return compile_and_lint_pipeline
  p_5_archigram_entryp_a3efe2->>+p_5_archigram_entryp_a3efe2: run_propose_loop
  p_5_archigram_entryp_a3efe2->>+p_6_archigram_featur_603002: GraphDigestGenerator.to_compact_markdown
  p_6_archigram_featur_603002-->>-p_5_archigram_entryp_a3efe2: return GraphDigestGenerator.to_compact_markdown
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: parse_curation_spec_yaml
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return parse_curation_spec_yaml
  p_5_archigram_entryp_a3efe2->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.check #91;loop x2#93;
  p_11_archigram_featur_d444ee-->>-p_5_archigram_entryp_a3efe2: return GroundingAndBoundaryGate.check #91;loop x2#93;
  p_5_archigram_entryp_a3efe2->>+p_13_archigram_featur_2a23e8: TopologicalInvariantGate.check
  p_13_archigram_featur_2a23e8-->>-p_5_archigram_entryp_a3efe2: return TopologicalInvariantGate.check
  p_5_archigram_entryp_a3efe2->>+p_0_archigram_domain_71c3aa: LintReport.from_diagnostics
  p_0_archigram_domain_71c3aa-->>-p_5_archigram_entryp_a3efe2: return LintReport.from_diagnostics
  p_5_archigram_entryp_a3efe2->>+p_0_archigram_domain_71c3aa: LintReport._validate_passed_invariant
  p_0_archigram_domain_71c3aa-->>-p_5_archigram_entryp_a3efe2: return LintReport._validate_passed_invariant
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: parse_curation_spec_yaml
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return parse_curation_spec_yaml
  p_5_archigram_entryp_a3efe2->>+p_11_archigram_featur_d444ee: collect_observed_symbols
  p_11_archigram_featur_d444ee-->>-p_5_archigram_entryp_a3efe2: return collect_observed_symbols
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: DiagramViewSpec._validate_unique_view_rules
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return DiagramViewSpec._validate_unique_view_rules
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: CurationSpecV1._validate_spec_uniqueness
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return CurationSpecV1._validate_spec_uniqueness
  p_5_archigram_entryp_a3efe2->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.check
  p_11_archigram_featur_d444ee-->>-p_5_archigram_entryp_a3efe2: return GroundingAndBoundaryGate.check
  p_5_archigram_entryp_a3efe2->>+p_14_archigram_featur_df5fa8: StructuralTopologicalReducer.reduce
  p_14_archigram_featur_df5fa8-->>-p_5_archigram_entryp_a3efe2: return StructuralTopologicalReducer.reduce
  p_5_archigram_entryp_a3efe2->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.check
  p_11_archigram_featur_d444ee-->>-p_5_archigram_entryp_a3efe2: return GroundingAndBoundaryGate.check
  p_5_archigram_entryp_a3efe2->>+p_13_archigram_featur_2a23e8: TopologicalInvariantGate.check
  p_13_archigram_featur_2a23e8-->>-p_5_archigram_entryp_a3efe2: return TopologicalInvariantGate.check
  p_5_archigram_entryp_a3efe2->>+p_10_archigram_featur_9a2480: CognitiveBudgetAndOrphanGate.check
  p_10_archigram_featur_9a2480-->>-p_5_archigram_entryp_a3efe2: return CognitiveBudgetAndOrphanGate.check
  p_5_archigram_entryp_a3efe2->>+p_8_archigram_featur_4d345c: MermaidEmitter.emit
  p_8_archigram_featur_4d345c-->>-p_5_archigram_entryp_a3efe2: return MermaidEmitter.emit
  p_5_archigram_entryp_a3efe2->>+p_12_archigram_featur_d6e8be: Gate5SyntaxHarness.validate
  p_12_archigram_featur_d6e8be-->>-p_5_archigram_entryp_a3efe2: return Gate5SyntaxHarness.validate
  p_5_archigram_entryp_a3efe2->>+p_0_archigram_domain_71c3aa: LintReport.from_diagnostics
  p_0_archigram_domain_71c3aa-->>-p_5_archigram_entryp_a3efe2: return LintReport.from_diagnostics
  p_5_archigram_entryp_a3efe2->>+p_0_archigram_domain_71c3aa: LintReport._validate_passed_invariant
  p_0_archigram_domain_71c3aa-->>-p_5_archigram_entryp_a3efe2: return LintReport._validate_passed_invariant
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: dump_curation_spec_yaml
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return dump_curation_spec_yaml
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: parse_curation_spec_yaml
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return parse_curation_spec_yaml
  p_5_archigram_entryp_a3efe2->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.check
  p_11_archigram_featur_d444ee-->>-p_5_archigram_entryp_a3efe2: return GroundingAndBoundaryGate.check
  p_5_archigram_entryp_a3efe2->>+p_14_archigram_featur_df5fa8: StructuralTopologicalReducer.reduce
  p_14_archigram_featur_df5fa8-->>-p_5_archigram_entryp_a3efe2: return StructuralTopologicalReducer.reduce
  p_5_archigram_entryp_a3efe2->>+p_11_archigram_featur_d444ee: GroundingAndBoundaryGate.check
  p_11_archigram_featur_d444ee-->>-p_5_archigram_entryp_a3efe2: return GroundingAndBoundaryGate.check
  p_5_archigram_entryp_a3efe2->>+p_13_archigram_featur_2a23e8: TopologicalInvariantGate.check
  p_13_archigram_featur_2a23e8-->>-p_5_archigram_entryp_a3efe2: return TopologicalInvariantGate.check
  p_5_archigram_entryp_a3efe2->>+p_10_archigram_featur_9a2480: CognitiveBudgetAndOrphanGate.check
  p_10_archigram_featur_9a2480-->>-p_5_archigram_entryp_a3efe2: return CognitiveBudgetAndOrphanGate.check
  p_5_archigram_entryp_a3efe2->>+p_8_archigram_featur_4d345c: MermaidEmitter.emit
  p_8_archigram_featur_4d345c-->>-p_5_archigram_entryp_a3efe2: return MermaidEmitter.emit
  p_5_archigram_entryp_a3efe2->>+p_12_archigram_featur_d6e8be: Gate5SyntaxHarness.validate
  p_12_archigram_featur_d6e8be-->>-p_5_archigram_entryp_a3efe2: return Gate5SyntaxHarness.validate
  p_5_archigram_entryp_a3efe2->>+p_0_archigram_domain_71c3aa: LintReport.from_diagnostics
  p_0_archigram_domain_71c3aa-->>-p_5_archigram_entryp_a3efe2: return LintReport.from_diagnostics
  p_5_archigram_entryp_a3efe2->>+p_0_archigram_domain_71c3aa: LintReport._validate_passed_invariant
  p_0_archigram_domain_71c3aa-->>-p_5_archigram_entryp_a3efe2: return LintReport._validate_passed_invariant
  p_5_archigram_entryp_a3efe2->>+p_2_archigram_domain_498ecc: dump_curation_spec_yaml
  p_2_archigram_domain_498ecc-->>-p_5_archigram_entryp_a3efe2: return dump_curation_spec_yaml
  p_5_archigram_entryp_a3efe2-->>-p_5_archigram_entryp_a3efe2: return run_propose_loop
```
<!-- archigram:end l3_runtime_pipeline_mermaid -->

---

## Curated View — Rendered D2 Sequence Diagram (`l3_runtime_pipeline_d2`)

Active runtime messages carry pure-CSS flowing strokes (`style.animated: true`):

![L3 Runtime Pipeline & Repair Loop in D2](diagrams/l3_runtime_pipeline_d2.d2.svg)

---

## Curated View — Step-by-Step Animated Architecture Playback (`l3_runtime_pipeline_steps_d2`)

Setting `diagram_variant: steps` on a D2 trace view emits a multi-board `steps: { step_1: { ... } ... }` playback compiled via `d2 --animate-interval 1200 --pad 24` into a self-playing multi-frame SVG (zero JavaScript—works natively on GitHub and Markdown viewers):

![L3 Step-by-Step Animated Playback in D2](diagrams/l3_runtime_pipeline_steps_d2.d2.svg)

<details>
<summary>View synced D2 multiboard steps source (<code>l3_runtime_pipeline_steps_d2</code>)</summary>

<!-- archigram:start l3_runtime_pipeline_steps_d2 -->
```d2
vars: {
  d2-config: {
    layout-engine: elk
    theme-id: 0
    dark-theme-id: 200
  }
}
style.fill: transparent

direction: right

"archigram.domain.diagnostics": "diagnostics" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.domain.ir": "ir" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.domain.spec": "spec" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.domain.style": "style" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.entrypoints.cli.pipeline": "pipeline" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.entrypoints.mcp.propose_loop": "propose_loop" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.digest.generator": "generator" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.emit_d2.emitter": "emit_d2.emitter" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.emit_mermaid.emitter": "emit_mermaid.emitter" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.emit_plantuml.emitter": "emit_plantuml.emitter" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.lint_budget.gate": "lint_budget.gate" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.lint_grounding.gates": "gates" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.lint_syntax.validators": "validators" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.lint_topology.gate": "lint_topology.gate" {
  style.fill: transparent
  style.stroke: "#64748b"
}
"archigram.features.reduce_structural.reducer": "reducer" {
  style.fill: transparent
  style.stroke: "#64748b"
}

steps: {
  step_1: {
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.reduce_structural.reducer": "StructuralTopologicalReducer.reduce (x3)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_2: {
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.emit_mermaid.emitter": "MermaidEmitter.emit" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.emit_d2.emitter": "D2Emitter.emit" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_3: {
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.emit_plantuml.emitter": "PlantUmlEmitter.emit" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_4: {
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.lint_grounding.gates": "GroundingAndBoundaryGate.__init__ (x2)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.lint_topology.gate": "TopologicalInvariantGate.__init__ (x2)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_5: {
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.lint_budget.gate": "CognitiveBudgetAndOrphanGate.check" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_6: {
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.lint_syntax.validators": "build_default_validators (x3)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
    "archigram.entrypoints.cli.pipeline" -> "archigram.domain.diagnostics": "LintReport.from_diagnostics" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_7: {
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.digest.generator": "GraphDigestGenerator.to_compact_markdown" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_8: {
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.domain.spec": "parse_curation_spec_yaml (x7)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.lint_grounding.gates": "GroundingAndBoundaryGate.check [loop x2] (x6)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_9: {
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.lint_topology.gate": "TopologicalInvariantGate.check (x3)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_10: {
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.domain.diagnostics": "LintReport.from_diagnostics (x6)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.reduce_structural.reducer": "StructuralTopologicalReducer.reduce (x2)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_11: {
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.lint_budget.gate": "CognitiveBudgetAndOrphanGate.check (x2)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
  step_12: {
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.emit_mermaid.emitter": "MermaidEmitter.emit (x2)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
    "archigram.entrypoints.mcp.propose_loop" -> "archigram.features.lint_syntax.validators": "Gate5SyntaxHarness.validate (x2)" {
      style.stroke: "#3b82f6"
      style.animated: true
    }
  }
}
```
<!-- archigram:end l3_runtime_pipeline_steps_d2 -->

</details>

