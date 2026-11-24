# LikeC4 Native Predicate Curation Coverage & Capability Boundary Spike

- **Beads Issue:** `ArchiGram-v2u.3`
- **Status:** Completed
- **LikeC4 Version Benchmarked:** `likec4` CLI `1.61.1` (Node 22 + Langium + Graphviz WASM)
- **Graph Engine Baseline:** `rustworkx` (`PyDiGraph`) on Python 3.12
- **Domain Classifier:** [`src/archigram/domain/likec4_boundary.py`](../../src/archigram/domain/likec4_boundary.py)

## 1. Executive Summary

This spike empirically evaluates how much of ArchiGram's L1/L2/L3 architecture curation and sequence-trace projection can be delegated directly to LikeC4's native DSL (`view of`, `include`/`exclude`/`where`, automatic parent-edge rollups, `dynamic view` with `par`/`loop`/`opt`, and `likec4 validate --json --no-layout`) versus where ArchiGram's deterministic `rustworkx` topological/trace reduction engine and Gate 3 invariants are strictly required.

**Core Architectural Finding:**
1. **LikeC4 excels as a hierarchical C4 projection & interactive exploration target** (`NATIVE_LIKEC4_ONLY`): parent-edge rollups, tag/kind `where` filtering, and `navigateTo` drill-down work out of the box with zero custom reducer logic.
2. **LikeC4 `exclude` is non-transitive and silently severs dependency paths**: excluding an intermediate non-parented component (e.g., `RetryMiddleware` in `OrderController -> RetryMiddleware -> OrdersDBClient -> OrdersDB`) deletes incident edges without splicing, breaking end-to-end reachability while `likec4 validate --json --no-layout` still reports `"valid": true, "totalErrors": 0`.
3. **LikeC4 lacks topological lint gates, OOP compartments, and automated trace compaction**: it performs no cycle checks (`DAGWouldCycle`) on merged concepts, no `preserve_reachability` or `max_fanout_product` biclique checks, no UML class field/method compartments, and no automated `R1-R6` / Series-Parallel trace folding. Furthermore, `likec4 gen mermaid|d2|plantuml` flattens `dynamic view` `par`/`loop`/`opt` fragments into static node-edge flowcharts.

Consequently, ArchiGram partitions views across three deterministic routing strategies in [`route_curation_strategy`](../../src/archigram/domain/likec4_boundary.py):
- `NATIVE_LIKEC4_ONLY` — Delegate view predicates directly to `.c4`.
- `HYBRID_PRE_REDUCE_THEN_LIKEC4` — Run ArchiGram's `rustworkx` structural/trace reducer + Gate 3 topological linter first, record `derived_deterministic` provenance, then emit grounded `.c4`.
- `ARCHIGRAM_NATIVE_EMITTER_REQUIRED` — Bypass LikeC4 rendering/codegen and emit directly via ArchiGram's `emit_mermaid`, `emit_d2`, or `emit_plantuml` (mandatory for OOP class compartments and preserving `par`/`loop`/`opt` sequence fragments in text DSLs).

---

## 2. Reproducible Benchmark Fixtures & Empirical Timings

Two synthetic fixtures were validated and exported using `likec4` `1.61.1`:
1. **Micro C4 + Cycle/Dynamic Fixture (14 elements, 10 edges):** Models `user -> orders.api.controller -> orders.api.retry -> orders.api.db_client -> orders.db`, async `orders.api.controller -> payments.worker`, and cross-system `auth.auth_core -> auth.auth_utils -> billing.billing_core -> billing.billing_utils`.
2. **Mid-Scale SCIP-Equivalent Fixture (137 total elements: 1 actor + 4 systems × 3 containers × 10 components = 120 leaf components, 186 edges):** Each container includes `#core`, `#middleware` (`comp_2`, `comp_7`), and `#noisy` (`comp_9`) components with sequential chains, cross-container `reads`, and telemetry edges.

### 2.1 Measured CLI & Graph Engine Timings

| Operation | Target / Scale | Mean Wall Time (ms) | Notes |
| --- | --- | ---: | --- |
| `likec4 validate --json --no-layout` | 137 elements, 186 edges, 5 views | **1,860.7 ms** | Syntax + Langium semantic validation (no layout) |
| `likec4 validate --json` (with layout) | 137 elements, 186 edges, 5 views | **2,120.4 ms** | Includes Graphviz WASM layout validation |
| `likec4 export json` | 137 elements, 186 edges, 5 views | **1,829.4 ms** | Computes resolved view nodes/edges + coordinates |
| `likec4 gen mermaid` | 137 elements, 5 views | **1,899.6 ms** | Generates `.mmd` files (`graph TB` / `graph LR`) |
| `likec4 gen d2` | 137 elements, 5 views | **1,795.2 ms** | Generates `.d2` files |
| `rustworkx` 12-node bypass + SCC condensation | 120 nodes, 186 edges (100-run mean) | **0.040 ms** | `remove_node_retain_edges_by_key` + `strongly_connected_components` |
| `route_curation_strategy` classifier | 500 view profiles | **3.47 ms** | ~6.9 us/view; verified in `tests/perf/perf_likec4_boundary.py` |

> **Latency Implication:** Every `likec4` CLI invocation incurs ~1.8s Node.js + WASM startup overhead. Pre-computing structural reductions and Gate 3 invariants in `rustworkx` takes **40 microseconds** (~46,500x faster), meaning ArchiGram can enforce all mathematical lint gates before paying a single `likec4 validate --json --no-layout` Gate 5 syntax check.

---

## 3. Concrete Scenario Analysis

### 3.1 Scenario (a): Where LikeC4 Suffices Natively (`NATIVE_LIKEC4_ONLY`)

LikeC4 natively handles hierarchical abstraction and metadata filtering when no intermediate edge-splicing or topological invariant checks are needed:

```likec4
// base-model.c4 (excerpt)
model {
  user = actor 'End User'
  orders = system 'Orders System' {
    #core
    api = container 'Orders API' {
      controller = component 'OrderController' { #core }
      retry = component 'RetryMiddleware' { #middleware }
      validator = component 'PayloadValidator' { #core }
      db_client = component 'OrdersDBClient' { #infra }
    }
    db = database 'Orders DB' { #infra }
  }
  payments = system 'Payments Gateway' {
    #core
    worker = component 'PaymentWorker'
  }
  user -> orders.api.controller 'POST /orders'
  orders.api.controller -[calls]-> orders.api.retry 'delegates'
  orders.api.retry -[calls]-> orders.api.db_client 'executes'
  orders.api.db_client -[writes]-> orders.db 'INSERT order'
  orders.api.controller -[async]-> payments.worker 'charge async'
}

// curated-views.c4 (native predicates)
views {
  view index {
    title 'L1 System Landscape (Native Rollup)'
    include *
  }
  view orders_containers of orders {
    title 'L2 Orders System (Where Tag Filter)'
    include *
    exclude * where tag is #deprecated
  }
}
```

**Empirical `likec4 export json` verification:**
- **Micro fixture `index`:** Rolled up 14 model elements into **5 top-level nodes** (`user`, `auth`, `orders`, `billing`, `payments`) and **3 edges** (`user -> orders`, `orders -> payments`, `auth -> billing`).
- **Micro fixture `orders_containers`:** Rolled up `orders.api.*` to `orders.api` and `payments.worker` to `payments`, yielding **5 nodes** and **3 edges** (`user -> orders.api`, `orders.api -> orders.db`, `orders.api -> payments`).
- **Mid-scale `landscape`:** Collapsed 137 elements / 186 edges cleanly into **5 nodes, 4 edges**.
- **Mid-scale `sys0_containers` (`exclude * where tag is #noisy`):** Filtered telemetry leaf components cleanly into **6 nodes, 4 edges**.

---

### 3.2 Scenario (b): Silent Dependency Path Severing on `exclude` (`HYBRID_PRE_REDUCE_THEN_LIKEC4`)

When `RetryMiddleware` (`orders.api.retry`) is a sibling/non-parented node along the path `OrderController -> RetryMiddleware -> OrdersDBClient -> OrdersDB`, LikeC4's `exclude` predicate drops the node **and deletes all incident edges** rather than contracting/splicing the path:

```likec4
views {
  view orders_api_exclude_retry of orders.api {
    include *
    include orders.db
    exclude orders.api.retry
  }
}
```

**Empirical `likec4 export json` comparison:**
- **`orders_api_full`:** 8 nodes, **6 edges** (`controller -> retry`, `retry -> db_client`, `db_client -> db`, plus `user -> controller`, `controller -> validator`, `controller -> payments`).
- **`orders_api_exclude_retry` (and `exclude * where tag is #middleware`):** 7 nodes, **4 edges** (`user -> controller`, `controller -> validator`, `controller -> payments`, `db_client -> db`).
  - `OrderController -> OrdersDBClient` and `OrderController -> OrdersDB` **disappear completely**.
- **Mid-scale `sys0_cont0_full` vs `sys0_cont0_no_mw`:** Edge count drops from **17 to 12**. Reachability checks confirm:
  - `sys_0.cont_0.comp_1 -> sys_0.cont_0.comp_3`: `True` (full) → **`False`** (excluded `#middleware`)
  - `sys_0.cont_0.comp_6 -> sys_0.cont_0.comp_8`: `True` (full) → **`False`** (excluded `#middleware`)
  - Cross-container `sys_0.cont_0.comp_2 -> sys_0.cont_1` is also severed.
- **`likec4 validate --json --no-layout`:** Exit code `0`, `"valid": true, "totalErrors": 0, "totalWarnings": 0`.

**ArchiGram Resolution:** `reduce_structural` executes `rustworkx.PyDiGraph.remove_node_retain_edges_by_key` to splice `OrderController -> OrdersDBClient` with `ProvenanceGrade.DERIVED_DETERMINISTIC` and `backing_ids=("orders.api.retry", ...)` while Gate 3 verifies `preserve_reachability` and `max_fanout_product`.

---

### 3.3 Scenario (c): Missing Topological Lint Gates, OOP Compartments, & Trace Compaction

| Gap Category | Synthetic Failure Case | LikeC4 `1.61.1` Observed Behavior | ArchiGram Engine & Gate 3 Enforcement |
| --- | --- | --- | --- |
| **Cycle-Induced Concept Merge (`DAGWouldCycle`)** | `auth_core -> auth_utils -> billing_core -> billing_utils`; merging `auth_utils` + `billing_utils` into `shared_utils` yields `shared_utils -> billing_core -> shared_utils`. | `likec4 validate --json --no-layout` returns `"valid": true, "totalErrors": 0`. Also cannot merge cross-parent elements purely inside a `view` predicate. | `collapse_partition` invokes `contract_nodes(check_cycle=True)` raising `DAGWouldCycle` with cycle witness in Gate 3. |
| **Reachability Invariant (`preserve_reachability`)** | Protected `(OrderController, OrdersDB)` path severed by `exclude` or pruning. | No assertion syntax; silently omits edges. | Gate 3 checks path preservation from observed IR to curated view and reports bottleneck witness. |
| **Biclique Explosion Guard (`max_fanout_product`)** | Bypassing a hub with `in_degree = 20, out_degree = 20` generates $20 \times 20 = 400$ edges. | No bypass operator and no `in_degree * out_degree` cognitive budget guard. | Gate 3 rejects any `bypass_vertices` target exceeding `max_fanout_product` (default `25`). |
| **OOP Class Compartments (`oop_compartments`)** | Class hierarchy with visibility (`+method()`, `-field: Type`), generics, and association/composition links. | No UML class compartment model or `classDiagram` emitter. | Routed to `ARCHIGRAM_NATIVE_EMITTER_REQUIRED` (`ingest_class` → `emit_mermaid` / `emit_plantuml`). |
| **Trace Compaction (`R1-R6` + SP-Poset) & Export Fidelity** | 500-iteration loop + 50-way concurrent OTLP fan-out + induced-N causal poset; exporting `par`/`loop`/`opt` to `.mmd`/`.d2`/`.puml`. | `dynamic view` parses `par`/`loop`/`opt` into `sequenceLayout` JSON, but rejects `alt` (`Expecting token of type '}'`), performs **zero** automatic `R1-R6` folding from spans, and `likec4 gen mermaid/d2/plantuml` flattens dynamic views to static flowcharts (`graph LR`). | `reduce_trace` compacts `R1-R6` + Valdes-Tarjan-Lawler SP-poset first; emits `.c4` `dynamic view` for LikeC4 UI or routes directly to `emit_mermaid`/`emit_plantuml` when sequence DSL export is requested. |

---

## 4. Capability Matrix (Implemented in `likec4_boundary.py`)

| Operator / Invariant (`CurationOperatorKind`) | Native LikeC4 Support | ArchiGram Pre-Reducer / Gate | Strategy Classification |
| --- | --- | --- | --- |
| `HIERICAL_ROLLUP` | Yes (`view of`, parent rollups) | Optional | `NATIVE_LIKEC4_ONLY` |
| `TAG_METADATA_FILTER` | Yes (`include`/`exclude ... where`) | Optional | `NATIVE_LIKEC4_ONLY` |
| `INTERACTIVE_DRILL_DOWN` | Yes (`navigateTo`) | No | `NATIVE_LIKEC4_ONLY` |
| `EXPLICIT_DYNAMIC_FLOW` | Yes (`dynamic view` `par`/`loop`/`opt`) | No | `NATIVE_LIKEC4_ONLY` |
| `PRUNE_LEAVES` | No (no degree-0 iterative prune) | `reduce_structural` | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `BYPASS_VERTICES` | No (`exclude` severs paths) | `reduce_structural` + `DERIVED_DETERMINISTIC` | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `COLLAPSE_PARTITION` | No (cross-parent merges unsupported) | `contract_nodes(check_cycle=True)` | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `K_HOP_SLICE` | Partial (`-> * ->` unguarded) | `reduce_structural` asymmetric depth slice | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `TRANSITIVE_REDUCTION` | No | SCC condensation + `transitive_reduction` | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `TRACE_COMPACTION` | No (manual DSL only; no `R1-R6`) | `reduce_trace` (`R1-R6` + SP-poset) | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `PRESERVE_REACHABILITY` | No | Gate 3 (`lint_topology`) | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `FORBID_NEW_CYCLES` | No (`validate` allows cycles) | Gate 3 (`DAGWouldCycle`) | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `MAX_FANOUT_PRODUCT_GUARD` | No | Gate 3 (`in_deg * out_deg <= max`) | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `PRESERVE_PARTIAL_ORDER` | No | Gate 3 concurrency happens-before guard | `HYBRID_PRE_REDUCE_THEN_LIKEC4` |
| `OOP_COMPARTMENTS` | No | `emit_mermaid` / `emit_plantuml` | `ARCHIGRAM_NATIVE_EMITTER_REQUIRED` |
| `EXPORT_SEQUENCE_FRAGMENTS` | No (`likec4 gen` flattens to static graph) | `emit_mermaid` / `emit_plantuml` | `ARCHIGRAM_NATIVE_EMITTER_REQUIRED` |
