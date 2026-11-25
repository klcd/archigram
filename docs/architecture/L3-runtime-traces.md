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

## Compaction Ordering Contract (Gate 3, `preserve_partial_order: true`)

Let `P_obs` be the transitive closure of `parent_event_id` + `causal_links` in the observed `InteractionTraceIR`, and `P_cur` the same closure in the curated (compacted) trace. For a curated event `u`, let `β(u)` be its backing observed event IDs (its own `event_id` if observed, plus `provenance.backing_ids` for `DERIVED_DETERMINISTIC` representatives). Gate 3 (`TopologicalInvariantGate`) enforces, for every pair of **distinct** surviving curated events `u ≠ v` whose backing sets are non-empty and disjoint:

| Relation | Rule | Diagnostic |
|---|---|---|
| `u <cur v` and some `b_v ∈ β(v)`, `b_u ∈ β(u)` with `b_v <obs b_u` | **Forbidden** — order reversed | `PARTIAL_ORDER_VIOLATED` |
| Direct curated edge `u → v` (`parent_event_id` / `causal_links`) with **no** `b_u <obs b_v` | **Forbidden** — invented order (false barrier between concurrent/independent events) | `PARTIAL_ORDER_FALSE_BARRIER` |
| Some `b_u <obs b_v` but `u ≮cur v` | **Forbidden** — dropped order (reported once per minimal missing pair, not per transitive pair) | `PARTIAL_ORDER_LOST_EDGE` |
| Curated `SYNC` event backed by an `ASYNC_FIRE_FORGET` event | **Forbidden** — async upgraded to sync | `ASYNC_UPGRADED_TO_SYNC` |

Every rejection lists the curated event IDs **and** the backing observed event IDs in `offending_symbols`, so the failure can be traced to raw spans.

**What may be abstracted away.** Order *internal to one contracted representative* — i.e. pairs whose backing sets overlap because the events were folded into the same node by `R1–R3` loop folding, `R4` recursion cuts, `max_call_depth` truncation, or `R6` parallel-map folding — is deliberately dropped. Transitive middle nodes may be bypassed (`a → mid → b` contracted to `rep(a, mid) → b`) as long as every ordering **between** distinct survivors is retained. Compaction carries `causal_links` of folded events onto their surviving representative so valid compaction satisfies the contract by construction.

### Canonical validation outcomes

| # | Case | Observed | Curated | Result |
|---|---|---|---|---|
| 1 | Reversed order | `a → b` | `b → a` | Rejected: `PARTIAL_ORDER_VIOLATED` |
| 2 | Invented order | `a ∥ b` | `a → b` | Rejected: `PARTIAL_ORDER_FALSE_BARRIER` |
| 3 | Dropped order | `a → b` (or `a → mid → b`) | `a`, `b` survive unordered | Rejected: `PARTIAL_ORDER_LOST_EDGE` |
| 4 | Legitimate contraction | loops, recursion, depth cuts, parallel maps, bypassed middle node | `TraceCompactionEngine` output | Accepted |
| 5 | Async fire-and-forget | `caller → async_child`, `async_child ∥ caller_next` | same shape | Accepted; upgrading `async_child` to `SYNC` → `ASYNC_UPGRADED_TO_SYNC`; inventing `async_child → caller_next` → `PARTIAL_ORDER_FALSE_BARRIER` |

These cases are exercised by `tests/fuzz/fuzz_trace_compaction_guarantees.py` and benchmarked by `tests/perf/perf_trace_compaction_guarantees.py`.

### Guarantees on the recorded trace vs. instrumentation coverage

**Enforced on the recorded trace (deterministic, checked by Gate 3).** Modulo the contractions above, the compacted trace preserves, reverses, invents or drops nothing relative to whatever `parent_event_id`, `causal_links`, `clock_domain_id` and `sync_kind` edges are present in the observed `InteractionTraceIR`; and `ASYNC_FIRE_FORGET` is never upgraded to `SYNC`. Events in different `clock_domain_id`s are never ordered by timestamp alone.

**Dependent on instrumentation coverage (not provable from the trace).** The checker can only reason about edges that were recorded. Therefore:

- Work in untraced code (C extensions, native threads, subprocesses outside the `sys.monitoring` / OTLP scope) has no events, so ordering through it is invisible.
- Omitted OTLP `SpanLinks` / `causal_links` make genuinely ordered spans look concurrent; compaction may then fold or parallelise them (`par` / parallel-map) without a violation being detectable.
- A missing or inconsistent `clock_domain_id` degrades to conservative singleton domains: fewer overlaps are inferred, so fewer `par` folds, never extra ordering.
- Async idioms the tracer/extractor does not emit as `ASYNC_FIRE_FORGET` (e.g. un-awaited tasks, queue hand-offs, static call-graph imports) are treated as `SYNC` backing and cannot be protected by the non-upgrade rule.

---

## Curated View — Mermaid Sequence Diagram (`l3_runtime_pipeline_mermaid`)

<!-- archigram:start l3_runtime_pipeline_mermaid -->
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
    "archigram.entrypoints.cli.pipeline" -> "archigram.features.emit_mermaid.emitter": "MermaidEmitter.__init__ (x2)" {
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

