# Step 4: Falsifiable Acceptance & Adversarial Mutation Benchmark Evaluation

This report documents the empirical verification of ArchiGram's four falsifiable acceptance guarantees and sub-second end-to-end compilation throughput across the zero-LLM CLI Data Plane (`apply`, `lint`, `diff`) and Bounded 3-Turn MCP Control Plane (`propose`).

All guarantees are continuously enforced via property-based Hypothesis fuzzing in [`tests/fuzz/fuzz_e2e_adversarial_mutations.py`](../../tests/fuzz/fuzz_e2e_adversarial_mutations.py) and `pytest-benchmark` latency suites in [`tests/perf/perf_e2e_benchmarks.py`](../../tests/perf/perf_e2e_benchmarks.py).

---

## 1. Summary of Falsifiable Acceptance Guarantees

| # | Falsifiable Guarantee | Test Workload | Target Invariant | Measured Result | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | **Byte-Identical Reproducibility** | 150–353 node structural graphs & 500–683 span concurrent traces under randomized node/edge/span serialization permutations | Byte-for-byte identical `.mmd`, `.d2`, `.puml`, and `.c4` outputs (`apply --check` exit code `0`) | **100% byte-identical** across all 4 target renderers & permutations | **PASS** |
| **2** | **100% Grounding & Provenance Fidelity** | Multi-tier enterprise structural graphs & concurrent traces compiled into curated views | `0` hallucinated/unbacked edges; `100%` of `OBSERVED` and `DERIVED_DETERMINISTIC` `backing_ids` resolve to input IR; `100%` `protected_nodes` & `preserve_reachability` paths survive | **0 unbacked elements**, **100% protected & reachability path survival** | **PASS** |
| **3** | **Cognitive Compression Within Human Budgets** | Raw `150–500` node structural graphs (leaf telemetry, retry middleware bridges, domain subsystems) & `500–1,000` span traces (`R1` loops + `R5/R6` parallel fan-outs) | `node_budget <= 25`, `edge_budget_max <= 40`, `max_par_branches <= 6`, and Gate 5 syntax validation `valid == True` | Structural: **500 -> 20 nodes, 18 edges** (`25.0x` node compression); Trace: **1,000 -> 20 events, <= 6 branches** (`50.0x` span compression) | **PASS** |
| **4** | **100% Adversarial PR Mutation Detection** | 4 synthetic adversarial PR/spec mutations injected into passing baseline repositories | `100%` deterministic rejection by Gates 1–4 (`passed == False`, CLI `lint` / `apply` / `diff` exit code `1`) | **100% catch rate** across all 4 mutation classes with actionable `repair_hint` and topological witnesses | **PASS** |

---

## 2. Adversarial PR Mutation Matrix & Catch Rates

Every synthetic mutation class in [`test_fuzz_guarantee_4_100_percent_adversarial_mutation_detection_and_cli_gates`](../../tests/fuzz/fuzz_e2e_adversarial_mutations.py) is injected into a verified passing baseline topology and evaluated through both the programmatic 5-Gate pipeline and the CLI (`archigram lint --json`, `archigram apply`, `archigram diff`):

| Mutation ID | Adversarial Fault Injected | Catching Gate | Diagnostic Code(s) | Witness / Repair Verification | Catch Rate |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Mutation A** (*Symbol Drift / Rename*) | Renaming an observed symbol referenced by `invariants.protected_nodes` or `scope.focal_nodes` (`edge.api_gateway` -> `edge.api_gateway_drifted_v2`, or ungrounded focal node) | **Gate 1** (`GATE_1_GROUNDING`) | `UNKNOWN_SYMBOL_ID` | Identifies drifted symbol in `offending_symbols` and suggests closest observed symbol via edit distance | **100%** (`exit_code == 1`) |
| **Mutation B** (*Naive Bridge Pruning*) | Targeting internal middleware retry proxies (`mw.*`, where `in_degree > 0` and `out_degree > 0`) with `prune_leaves` instead of `bypass_vertices` | **Gate 2** (`GATE_2_BOUNDARY_LEAF`) & **Gate 3** (`GATE_3_TOPOLOGY_POSET`) | `PRUNE_INTERNAL_BRIDGE`, `REACHABILITY_SEVERED` | Gate 2 flags non-leaf bridge violation and recommends `bypass_vertices`; dropping middleware bridges triggers Gate 3 reachability severance with shortest-path bottleneck attribution | **100%** (`exit_code == 1`) |
| **Mutation C** (*False Cycle Merge*) | Grouping `dom_00.utils` (`auth/utils`) and `dom_01.utils` (`billing/utils`) into a shared `subsys.shared_utils` `collapse_partition` across acyclic `dom_00 -> dom_01` flow | **Gate 3** (`GATE_3_TOPOLOGY_POSET`) | `INDUCED_PARTITION_CYCLE` | Emits closed directed cycle `witness_path` (`subsys.dom_01 -> subsys.shared_utils -> subsys.dom_01`) and instructs splitting `member_globs` or adding `exclude_ids` | **100%** (`exit_code == 1`) |
| **Mutation D** (*Async-to-Sync Corruption & Budget Blowout*) | (1) Upgrading an `ASYNC_FIRE_FORGET` backing edge to synchronous `SYNC` or exceeding `max_fanout_product` on `bypass_vertices`; (2) Dropping `collapse_partition` rules so raw nodes exceed `node_budget <= 25` | **Gate 3** (`GATE_3_TOPOLOGY_POSET`) & **Gate 4** (`GATE_4_BUDGET_ORPHAN`) | `ASYNC_UPGRADED_TO_SYNC`, `BICLIQUE_FANOUT_EXCEEDED`, `NODE_BUDGET_EXCEEDED` | Gate 3 pinpoints the upgraded async backing edge ID (`e_async_gateway_audit`) and fanout product; Gate 4 reports exact node overflow against `node_budget.max_count = 25` | **100%** (`exit_code == 1`) |

---

## 3. End-to-End Sub-Second Throughput Benchmarks

Measured via `pytest tests/perf/perf_e2e_benchmarks.py --benchmark-only --benchmark-min-rounds=5 --benchmark-warmup=off` on Apple Silicon (Python 3.13, `rustworkx` C/Rust-backed graph engine):

| Benchmark Suite | Workload Description | Min (ms) | Median (ms) | Mean (ms) | StdDev (ms) | Throughput (ops/sec) |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| `test_perf_e2e_500_node_structural_curation_5gate_lint_and_4_target_emission` | **500-node** enterprise structural graph (15 domain tiers, 240 telemetry leaf sinks, 15 middleware proxies, 5 edge/storage nodes) -> `prune_leaves` + `bypass_vertices` + `collapse_partition` + DAG `condense_sccs` + `transitive_reduction` -> **Gates 1–5 Linter** -> **4-target emission** (`.mmd`, `.d2`, `.puml`, `.c4`) | **83.11 ms** | **84.71 ms** | **85.54 ms** | 2.50 ms | **11.69 ops/s** |
| `test_perf_e2e_1000_span_trace_compaction_5gate_lint_and_sequence_emission` | **1,000-span** concurrent multi-service trace (540 `R1` contiguous loop calls + 9 waves of 50-branch concurrent fan-out spans across multiple clock domains) -> `R1–R6` + Series-Parallel Poset compaction -> **Gates 1–5 Linter** -> **4-target sequence emission** (`.mmd`, `.d2`, `.puml`, `.c4`) | **42.52 ms** | **43.64 ms** | **44.65 ms** | 4.55 ms | **22.40 ops/s** |

### Cognitive Compression Ratios Observed in Benchmarks

- **500-Node Structural Graph (`enterprise_500_node_benchmark`):**
  - Raw input: `500` nodes, `752` directed edges.
  - Curated output per view: `20` nodes (`node_budget <= 25`), `18` edges (`edge_budget_max <= 40`).
  - Compression ratio: **25.0x node reduction**, **41.8x edge reduction**, **100% protected entrypoint-to-ledger reachability preserved**, compiled and syntax-validated across all 4 target languages in **84.71 ms** median (**~21.2 ms per view**).
- **1,000-Span Concurrent Interaction Trace (`enterprise_1000_span_trace`):**
  - Raw input: `5` service participants, `1,000` concurrent/looping trace events.
  - Curated output per view: `5` participants (`node_budget <= 25`), `20` compacted representative events (`edge_budget_max <= 40`), max concurrent sibling branches folded to `<= 6` (`max_par_branches = 6`).
  - Compression ratio: **50.0x event reduction** with **100% backing span provenance coverage** (`collect_trace_backing_ids(curated) == frozenset(observed.events)`), compiled and syntax-validated across all 4 target languages in **43.64 ms** median (**~10.9 ms per view**).
