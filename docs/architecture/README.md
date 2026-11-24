# ArchiGram Architecture Documentation

> **100% Self-Hosted, Zero Hand-Drawn Drift:** Every diagram in this folder is compiled directly from live `src/archigram` AST symbols and a reproducible `sys.monitoring` runtime trace snapshot using ArchiGram's own `CurationSpecV1` specs, verified by all **5 ArchiGram Linter Gates**, and synced into Markdown via `archigram apply --sync-md docs/architecture`.

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
| Trace how `compile_and_lint_pipeline` and `run_propose_loop` execute end-to-end at runtime | **[L3: Runtime Execution & Self-Healing Traces](L3-runtime-traces.md)** | Compacted live sequence (`14` participants, `48` events) |

---

## Declarative Specs, Trace Snapshot & Companion Multi-Target Diagrams

All views are defined declaratively and rendered across four target languages (Mermaid, D2, PlantUML, LikeC4):

- **Structural Curation Spec:** [`diagram-views.yaml`](diagram-views.yaml) (compiled against `src/archigram`)
- **Runtime Trace Curation Spec:** [`trace-views.yaml`](trace-views.yaml) (compiled against [`traces/pipeline-and-repair.ir.json`](traces/pipeline-and-repair.ir.json))
- **Live Trace Snapshot:** [`traces/pipeline-and-repair.ir.json`](traces/pipeline-and-repair.ir.json) (captured via `src/archigram/entrypoints/cli/self_trace.py`)

| Level | View ID | Renderer | Companion Artifact |
| --- | --- | --- | --- |
| L0 | `l0_hexagonal_rings_mermaid` | Mermaid | [`diagrams/l0_hexagonal_rings_mermaid.mmd`](diagrams/l0_hexagonal_rings_mermaid.mmd) |
| L0 | `l0_hexagonal_rings_likec4` | LikeC4 | [`diagrams/l0_hexagonal_rings_likec4.c4`](diagrams/l0_hexagonal_rings_likec4.c4) |
| L0 | `l0_hexagonal_rings_d2` | D2 | [`diagrams/l0_hexagonal_rings_d2.d2`](diagrams/l0_hexagonal_rings_d2.d2) |
| L1 | `l1_feature_slices_mermaid` | Mermaid | [`diagrams/l1_feature_slices_mermaid.mmd`](diagrams/l1_feature_slices_mermaid.mmd) |
| L1 | `l1_feature_slices_d2` | D2 | [`diagrams/l1_feature_slices_d2.d2`](diagrams/l1_feature_slices_d2.d2) |
| L2a | `l2a_domain_and_ports_mermaid` | Mermaid | [`diagrams/l2a_domain_and_ports_mermaid.mmd`](diagrams/l2a_domain_and_ports_mermaid.mmd) |
| L2a | `l2a_domain_and_ports_puml` | PlantUML | [`diagrams/l2a_domain_and_ports_puml.puml`](diagrams/l2a_domain_and_ports_puml.puml) |
| L2b | `l2b_reduction_engine_mermaid` | Mermaid | [`diagrams/l2b_reduction_engine_mermaid.mmd`](diagrams/l2b_reduction_engine_mermaid.mmd) |
| L2b | `l2b_reduction_engine_d2` | D2 | [`diagrams/l2b_reduction_engine_d2.d2`](diagrams/l2b_reduction_engine_d2.d2) |
| L2c | `l2c_five_gate_linter_mermaid` | Mermaid | [`diagrams/l2c_five_gate_linter_mermaid.mmd`](diagrams/l2c_five_gate_linter_mermaid.mmd) |
| L2d | `l2d_cli_and_mcp_adapters_mermaid` | Mermaid | [`diagrams/l2d_cli_and_mcp_adapters_mermaid.mmd`](diagrams/l2d_cli_and_mcp_adapters_mermaid.mmd) |
| L3 | `l3_runtime_pipeline_mermaid` | Mermaid | [`diagrams/l3_runtime_pipeline_mermaid.mmd`](diagrams/l3_runtime_pipeline_mermaid.mmd) |
| L3 | `l3_runtime_pipeline_puml` | PlantUML | [`diagrams/l3_runtime_pipeline_puml.puml`](diagrams/l3_runtime_pipeline_puml.puml) |

---

## Regenerating & Verifying Zero Drift

Run these commands after changing `src/archigram` or either curation spec:

```bash
# 1. Re-capture the deterministic live sys.monitoring runtime trace snapshot
uv run python -m archigram.entrypoints.cli.self_trace

# 2. Emit all L0/L1/L2 companion diagrams and sync inline Markdown blocks
uv run archigram apply src/archigram \
  --spec docs/architecture/diagram-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture

# 3. Emit all L3 trace companion diagrams and sync inline Markdown blocks
uv run archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
  --spec docs/architecture/trace-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture
```

To verify in CI (or locally) that every spec passes all 5 Gates and every Markdown block is byte-for-byte in sync with zero drift:

```bash
uv run archigram apply src/archigram \
  --spec docs/architecture/diagram-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture \
  --check

uv run archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
  --spec docs/architecture/trace-views.yaml \
  --out-dir docs/architecture/diagrams \
  --sync-md docs/architecture \
  --check
```
