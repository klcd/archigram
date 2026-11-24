# Parallelism & Concurrency Detection Specification (Runtime Traces + Static Fork-Join)

- **Beads Issue:** `ArchiGram-v2u.4`
- **Status:** Accepted (Wave 0 Foundation Specification)
- **Domain Reference Implementation:** [`src/archigram/domain/concurrency.py`](../../src/archigram/domain/concurrency.py)
- **Canonical IR Alignment:** [`src/archigram/domain/ir.py`](../../src/archigram/domain/ir.py) (`StructuralGraphIR`, `InteractionTraceIR`, `TraceEvent`, `EdgeRelation`, `SyncKind`)

---

## 1. Problem Statement & Architectural Scope

Automated sequence and dataflow diagram generators fail in two opposite directions when rendering concurrent distributed systems:

1. **False Sequentialization:** Flattening concurrent fan-out calls (`Promise.all`, `asyncio.gather`, Go `errgroup`, parallel OTel child spans) into a linear timeline hides critical-path concurrency and misrepresents system latency.
2. **False Parallelism & False Synchronization Barriers:** Comparing unsynchronized server wall clocks across hosts fabricates spurious overlaps (or erases genuine concurrency), while forcing non-Series-Parallel causal graphs (induced-"N" zig-zags from OTel `SpanLinks` or asynchronous callbacks) into rigid UML `par` blocks either invents phantom synchronization barriers or drops real happens-before edges.

ArchiGram solves both failure modes through **four deterministic, provenance-preserving layers** shared across runtime OpenTelemetry (OTLP) traces (`InteractionTraceIR`) and static call graphs (`StructuralGraphIR`):

| Layer | Rule / Algorithm | Input | Output / Semantic Artifact |
| :--- | :--- | :--- | :--- |
| **1. Sibling Sweep** | `R5: fold_parallel_siblings` | Sibling `TraceEvent` / `SpanInterval` records | `ParallelSiblingCluster` → `ParallelFragment` (`par`) |
| **2. Fan-Out Compaction** | `R6: fold_parallel_map` | Homogeneous spans inside an R5 cluster | `ParallelMapSummary` (`par [N concurrent calls]`) |
| **3. Poset Decomposition** | Series-Parallel / N-free Modular Decomposition | Causal DAG (`parent_event_id` + `causal_links`) | `PosetCotreeNode` (`LEAF` / `SERIES` / `PARALLEL` / `PRIME`) |
| **4. Static Fork-Join & Gate 3** | AST/SCIP/DOT Normalization + Topological Lint | Static call graph + reduced views | `FORK_JOIN` / `ASYNC_SPAWN` edges + `preserve_partial_order` |

Every derived fragment, summary, or lowered edge MUST carry a `ProvenanceStamp` with `grade = ProvenanceGrade.DERIVED_DETERMINISTIC`, an explicit `rule_id` (`R5:fold_parallel_siblings`, `R6:fold_parallel_map`, `SP:decompose_causal_poset`, `STATIC:fork_join_normalize`), and complete `backing_ids` referencing the underlying observed `event_id`s or `edge_id`s.

---

## 2. Layer 1 — Clock-Skew-Safe Sibling Sweep (`R5: fold_parallel_siblings`)

### 2.1 Why Cross-Host Wall-Clock Comparison Is Unsound

In distributed OpenTelemetry traces, spans emitted by different hosts, containers, or virtual machines draw timestamps from independent clocks subject to:

- **NTP/chrony skew and slew** (commonly $1\text{–}50\,\text{ms}$, exceeding sub-millisecond RPC durations);
- **VM live-migration pauses and non-monotonic wall-clock jumps**;
- **Negative apparent RTT artifacts** where a downstream server's `SERVER` span reports `start_ns` earlier than the upstream caller's `CLIENT` span `start_ns`.

Consequently, comparing `[start_ns, end_ns]` intervals across distinct hosts is **strictly forbidden** in ArchiGram. Cross-host ordering is derived solely from explicit causal edges (`parent_event_id` and OTel `SpanLinks` / `causal_links`), never from wall-clock subtraction.

### 2.2 Clock-Domain Isolation Contract

Every `TraceEvent` (and projected `SpanInterval`) carries a mandatory non-blank `clock_domain_id` identifying the **caller's** monotonic clock source (canonically `<host.id>:<process.pid>` or `<service.instance.id>`).

- **Grouping Key:** Outbound child spans are partitioned strictly by `(parent_event_id, clock_domain_id)`.
- **Zero Cross-Domain Clustering:** Two spans $i, j$ with `clock_domain_id_i != clock_domain_id_j` or `parent_event_id_i != parent_event_id_j` can **never** enter the same R5 `ParallelSiblingCluster`, even if their raw numeric timestamps overlap.
- **Missing Clock Metadata Fallback:** If an OTLP ingestor encounters spans lacking reliable host/process clock identity, it MUST assign each such span a unique synthetic singleton `clock_domain_id` (e.g., `unknown:<event_id>`) so R5 fails safely without inventing ungrounded parallelism.

### 2.3 Exact Overlap Relation & $O(n \log n)$ Sweep-Line Algorithm

Within a single `(parent_event_id, clock_domain_id)` partition, each span defines a closed non-degenerate interval $I_i = [s_i, e_i]$ with $0 \le s_i \le e_i$. Given a non-negative jitter tolerance $\varepsilon = \text{epsilon\_ns} \ge 0$ (configurable in Curation Spec v1 `diagram-views.yaml`, default `0` or e.g. `100_000` ns for sub-100µs timer jitter):

Two sibling spans $I_i, I_j$ ($i \ne j$) **strictly overlap** iff:

$$\max(s_i, s_j) < \min(e_i, e_j) - \varepsilon \iff \min(e_i, e_j) - \max(s_i, s_j) > \varepsilon.$$

**Exact Sweep-Line Reduction:**

1. **Sub-$\varepsilon$ Filtering:** Any interval with duration $e_i - s_i \le \varepsilon$ satisfies $\min(e_i, e_j) - \max(s_i, s_j) \le e_i - s_i \le \varepsilon$ against *every* interval $I_j$, so it is an isolated vertex in the overlap graph $G_\varepsilon$ and is excluded before sweeping.
2. **Canonical Sort:** Sort remaining eligible intervals by `(start_ns, end_ns, event_id)`.
3. **Single-Pass Connected Components:** Because all remaining intervals satisfy $e - s > \varepsilon$, whenever $s_i \le s_j$, the overlap condition simplifies identically to $s_j < e_i - \varepsilon$. Sweeping in ascending `start_ns` order while tracking `max_end = max(e_k for k in current_component)`:
   - If `span.start_ns < max_end - epsilon_ns`, append `span` to the current component and update `max_end = max(max_end, span.end_ns)`.
   - Otherwise, close the current component (emitting a `ParallelSiblingCluster` iff `len(current) >= 2`) and start a new component at `span`.
4. **Fragment Emission:** Every connected component of size $\ge 2$ becomes a `ParallelSiblingCluster` (and renders as a `ParallelFragment` / UML `par ... and ... end` block). Disjoint or touching sequential intervals ($e_i \le s_j + \varepsilon$) never cluster.

Implemented in [`cluster_overlapping_siblings`](../../src/archigram/domain/concurrency.py) in $O(n \log n)$ time per `(parent_event_id, clock_domain_id)` group.

---

## 3. Layer 2 — Homogeneous Parallel Fan-Out Compaction (`R6: fold_parallel_map`)

### 3.1 Motivation

Real production traces routinely contain scatter-gather loops (`Promise.all(items.map(fetchItem))`, `asyncio.gather(*(fetch(x) for x in ids))`, Go `errgroup` over $N$ shards). Applying R5 alone yields a valid `par` block with $N = 25\text{–}500$ identical branches, violating Gate 4 cognitive density budgets and producing unreadable sequence diagrams.

### 3.2 Compaction Rule & Thresholds

Rule `R6: fold_parallel_map` executes **after** R5 inside each `ParallelSiblingCluster`:

1. **Homogeneity Key:** Partition spans within a single `ParallelSiblingCluster` by `(callee_id, normalized_signature)` where `normalized_signature` is the route/method template stripped of high-cardinality entity IDs (e.g., `GET /v1/inventory/{sku}`, `ShardClient.Query`).
2. **Threshold Compaction:** Let `max_par_branches >= 2` be the Curation Spec v1 branch budget (default `4`).
   - If a homogeneous bucket has cardinality $k \ge \text{max\_par\_branches}$, collapse all $k$ spans into one `ParallelMapSummary`:
     - `concurrency_count = k`
     - `backing_ids = tuple(span.event_id for span in members)` (exact canonical order preserved)
     - `window_start_ns = min(span.start_ns)`, `window_end_ns = max(span.end_ns)`
     - `ProvenanceStamp(grade=DERIVED_DETERMINISTIC, rule_id="R6:fold_parallel_map", backing_ids=backing_ids)`
   - Homogeneous buckets with $k < \text{max\_par\_branches}$ remain explicit individual `par` operands.
3. **Diagram Rendering:**
   - **Pure homogeneous cluster:** Emits `par [N concurrent calls]` containing the single representative interaction `<caller> ->> <callee>: <normalized_signature>`.
   - **Mixed heterogeneous cluster** (e.g., 12 parallel `Inventory.Check` calls concurrent with 1 `Fraud.Score` call): Renders a `par` block with two operands—one compacted `[12 concurrent calls]` operand and one explicit `Fraud.Score` operand.

Implemented in [`fold_parallel_map`](../../src/archigram/domain/concurrency.py).

---

## 4. Layer 3 — Series-Parallel Poset Modular Decomposition & Induced-"N" Lowering

### 4.1 Causal Poset Construction

When a trace includes OTel `SpanLinks` (`TraceEvent.causal_links`), async queue handoffs, or multi-hop fork-join synchronization, parent-child trees alone cannot represent the full happens-before order. ArchiGram constructs the causal DAG $G = (V, E)$ from:

- Tree edges `(parent_event_id, event_id)` for every event with `parent_event_id is not None`;
- Cross-branch causal edges `(link_id, event_id)` for every `link_id in event.causal_links`.

After validating acyclicity (Kahn's topological sort; cycles raise `ValueError`), ArchiGram computes the **transitive closure** strict partial order $P = (V, <)$ via bitmask propagation in $O((|V| + |E|) \cdot \lceil |V| / w \rceil)$ time.

### 4.2 Grabowski (1981) & Valdes–Tarjan–Lawler (1982) Modular Decomposition

Define the undirected **comparability graph** $G_P = (V, \{\{u, v\} : u < v \lor v < u\})$ and its complement, the **incomparability graph** $\overline{G}_P = (V, \{\{u, v\} : u \parallel v\})$.

Every non-empty subposet $Q \subseteq P$ decomposes canonically into a `PosetCotreeNode` with `kind: PosetModuleKind`:

1. **`LEAF`** (`|Q| = 1`): Single event `node_id`; `concurrency_mode = SEQUENTIAL`.
2. **`PARALLEL`** ($G_Q$ disconnected): Connected components $Q_1, \dots, Q_k$ ($k \ge 2$) of $G_Q$ are pairwise incomparable ($Q_1 \parallel \dots \parallel Q_k$). Maps directly to a UML/Mermaid/PlantUML `par` fragment (`concurrency_mode = FORK_JOIN`).
3. **`SERIES`** ($\overline{G}_Q$ disconnected): Connected components $Q_1, \dots, Q_k$ ($k \ge 2$) of $\overline{G}_Q$ form a **unique strict total order** $Q_1 < Q_2 < \dots < Q_k$ where $\forall i < j, \forall x \in Q_i, \forall y \in Q_j: x < y$ (`concurrency_mode = SEQUENTIAL`).
4. **`PRIME`** (both $G_Q$ and $\overline{G}_Q$ connected, $|Q| \ge 4$): Indecomposable core containing an **induced "N" (zig-zag) subposet**. By Gallai's modular decomposition theorem, $Q$ partitions uniquely into $r \ge 4$ **maximal proper modules** $M_1, \dots, M_r$. Each $M_i$ is recursively decomposed, while the `PRIME` node stores:
   - `prime_child_edges`: the unique **transitive reduction** (Hasse cover edges) of the quotient poset $Q / \{M_1, \dots, M_r\}$;
   - `induced_n_witness = (a, b, c, d)`: an exact 4-tuple of distinct events where $a < c$, $b < c$, $b < d$, and $a \parallel b$, $c \parallel d$, $a \parallel d$;
   - `concurrency_mode = WEAK_SEQUENCING`.

**Fundamental Theorem (Grabowski 1981; Valdes, Tarjan & Lawler 1982):**
A finite causal poset $P = (V, <)$ decomposes into `LEAF`, `SERIES`, and `PARALLEL` modules with **zero `PRIME` nodes** if and only if $P$ is **N-free** (i.e., [`find_induced_n_witness`](../../src/archigram/domain/concurrency.py) returns `None`). Furthermore, `decompose_causal_poset` is **100% lossless**: [`reconstruct_causal_order`](../../src/archigram/domain/concurrency.py) recovers the exact transitive closure $P = (V, <)$ for all DAGs, including those with nested `PRIME` cores.

### 4.3 Why `par` Blocks Cannot Truthfully Render an Induced-"N"

Consider the minimal induced-"N" poset on $\{a, b, c, d\}$ with exact relations $a < c$, $b < c$, $b < d$ (and $a \parallel d$):

```text
  a       b
   \     / \
    v   v   v
      c     d
```

Every attempt to express this poset using only `SERIES` and `PARALLEL` (`par`) fails Gate 3:

- `(a || b) ; (c || d)` **invents a false synchronization barrier** $a < d$ that never occurred at runtime (violates soundness).
- `(a ; c) || (b ; d)` **erases the real happens-before edge** $b < c$ (violates completeness).

### 4.4 Deterministic `PRIME` Lowering to Weak-Sequencing Sequence Diagrams

When an emitter encounters a `PosetCotreeNode` with `kind == PosetModuleKind.PRIME`:

1. **Preserve Nested Structure:** Recursively lower each maximal proper module child $M_i$ (e.g., if $M_i$ is a `PARALLEL` fan-out feeding into the N-core, emit its `par` structure intact).
2. **Quotient Order Without Phantom Barriers:** Emit the quotient cover edges `prime_child_edges` in a canonical linear extension (lexicographic Kahn topological order over module indices) using **asynchronous weak-sequencing notation** instead of a synthetic global `par` barrier:
   - **Mermaid (`sequenceDiagram`):**
     - Emit asynchronous open-head messages using `-)` (e.g., `Caller -) Callee: signature`).
     - Attach a deterministic note/comment annotating non-tree join edges (`SpanLink` dependencies) and the `induced_n_witness` (`%% weak-sequencing prime core: witness (a, b, c, d)`), making clear that vertical line order is a valid linear extension rather than a blocking join barrier.
   - **PlantUML (`@startuml`):**
     - Emit `!pragma teoz true` at the diagram header to activate the Teoz layout engine.
     - Emit asynchronous messages (`->>`) and use Teoz `&` parallel-start operators for concurrent minimal modules together with named `{start}` / `{end}` anchors to express staggered causal dependencies (`b < c` and `b < d` without forcing `a < d`).
3. **Diagnostic Transparency:** Surface `induced_n_witness` in structured ArchiGram diagnostics so engineers and LLM reviewers can see *why* a trace region lowered to weak sequencing rather than a `par` box.

---

## 5. Layer 4 — Static Fork-Join Normalization & Gate 3 Partial-Order Invariants

### 5.1 Static Concurrency Idiom Mapping (`StructuralGraphIR`)

Static ingestors (`ingest_scip`, `ingest_dot` / `go-callvis`) MUST classify concurrent invocations into ArchiGram's two distinct Canonical IR edge semantics rather than collapsing them into generic synchronous `CALLS`:

| Ecosystem / Extractor | Static Idiom | `EdgeRelation` | `SyncKind` | `parallel_region_id` |
| :--- | :--- | :--- | :--- | :--- |
| **TypeScript / JS** (`ingest_scip`) | `Promise.all([...])`, `Promise.allSettled([...])`, `Promise.race([...])` | `FORK_JOIN` | `FORK_JOIN_BRANCH` | Shared deterministic ID per call-site |
| **TypeScript / JS** (`ingest_scip`) | Unawaited `asyncFn()` / `void promise` / detached `.then()` | `ASYNC_SPAWN` | `ASYNC_FIRE_FORGET` | `None` |
| **Python** (`ingest_scip`) | `asyncio.gather(...)`, `async with asyncio.TaskGroup() as tg: tg.create_task(...)` | `FORK_JOIN` | `FORK_JOIN_BRANCH` | Shared deterministic ID per block/call-site |
| **Python** (`ingest_scip`) | Unawaited `asyncio.create_task(...)` / `loop.create_task(...)` outside `gather`/`TaskGroup` | `ASYNC_SPAWN` | `ASYNC_FIRE_FORGET` | `None` |
| **Go** (`ingest_scip`, `ingest_dot`) | `errgroup.Group` (`.Go()` + `.Wait()`), `sync.WaitGroup` (`.Add()` + `go` + `.Wait()`) | `FORK_JOIN` | `FORK_JOIN_BRANCH` | Shared deterministic ID per group variable |
| **Go** (`go-callvis` DOT) | Bare `go fn()` / `go-callvis` goroutine edge (`arrowhead=empty` / `odot` / `style=dashed`) | `ASYNC_SPAWN` | `ASYNC_FIRE_FORGET` | `None` |
| **All Languages** | Standard awaited / blocking function call | `CALLS` | `SYNC` | `None` |

All edges sharing a `parallel_region_id` from the same caller represent a statically grounded `PARALLEL` (`FORK_JOIN`) module whose join barrier is explicitly owned by the caller.

### 5.2 Gate 3 (`lint_topology`) Concurrency & Partial-Order Invariants

When curation operators (`bypass_vertices`, `collapse_partition`, `prune_leaves`, `trace_compaction`) reduce a structural graph or trace, Gate 3 enforces two non-negotiable invariants whenever concurrency metadata or `preserve_partial_order: true` is active:

#### Invariant A — Sync-Kind Composition Lattice (No Silent Blocking Upgrade)

When `bypass_vertices` removes an intermediate vertex $v$ and composes incoming edge $e_1 = (u \to v, s_1)$ with outgoing edge $e_2 = (v \to w, s_2)$ into a derived edge $(u \to w, s_1 \otimes s_2)$, the composed `SyncKind` MUST obey the conservative absorption lattice:

| $s_1 \otimes s_2$ | `SYNC` | `FORK_JOIN_BRANCH` | `ASYNC_FIRE_FORGET` |
| :--- | :--- | :--- | :--- |
| **`SYNC`** | `SYNC` | `FORK_JOIN_BRANCH` | `ASYNC_FIRE_FORGET` |
| **`FORK_JOIN_BRANCH`** | `FORK_JOIN_BRANCH` | `FORK_JOIN_BRANCH` | `ASYNC_FIRE_FORGET` |
| **`ASYNC_FIRE_FORGET`** | `ASYNC_FIRE_FORGET` | `ASYNC_FIRE_FORGET` | `ASYNC_FIRE_FORGET` |

- **Hard Gate 3 Error (`CONCURRENCY_SYNC_UPGRADE`):** If any bypassed path containing an `ASYNC_FIRE_FORGET` edge is emitted as `SYNC` or `FORK_JOIN_BRANCH`, Gate 3 rejects the curated graph. Bypassing an intermediary can **never** claim the caller blocks on an asynchronous fire-and-forget task.

#### Invariant B — Exact Partial-Order Preservation (`preserve_partial_order`)

Let $P = (V, <)$ be the ground-truth causal partial order of the input trace (transitive closure of `parent_event_id` + `causal_links`), and let $P' = (V', <')$ be the causal partial order reconstructed from the curated cotree/diagram over retained events $V' \subseteq V$ (via `reconstruct_causal_order`).

Gate 3 enforces exact induced-subposet preservation:

$$\forall u, v \in V': \quad (u <' v) \iff (u < v).$$

- **Soundness Check (No Phantom Barriers — `PARTIAL_ORDER_FALSE_BARRIER`):** Fails if $\exists u, v \in V'$ with $u <' v$ while $u \parallel v$ in $P$.
- **Completeness Check (No Dropped Causality — `PARTIAL_ORDER_LOST_EDGE`):** Fails if $\exists u, v \in V'$ with $u < v$ in $P$ while $u \not<' v$ in $P'$.
- **Witness Reporting:** Whenever a reduction attempts to replace a `PRIME` module with a `SERIES`/`PARALLEL` block, Gate 3 attaches the `induced_n_witness = (a, b, c, d)` proving why the transformation violated `preserve_partial_order`.

---

## 6. Curation Spec v1 Parameter Alignment & Performance Budgets

### 6.1 Curation Spec v1 (`diagram-views.yaml`) Fields

| Parameter | Scope | Default | Semantics |
| :--- | :--- | :--- | :--- |
| `epsilon_ns` | `R5` / `R6` | `0` | Clock jitter tolerance in nanoseconds ($\ge 0$) for same-domain interval overlap |
| `max_par_branches` | `R6` | `4` | Minimum concurrent homogeneous sibling count ($\ge 2$) triggering `ParallelMapSummary` compaction |
| `preserve_partial_order` | Gate 3 | `true` (traces) | Enforces $\forall u, v \in V': (u <' v) \iff (u < v)$ and forbids `ASYNC_FIRE_FORGET` blocking upgrades |

### 6.2 Complexity & Verification Budgets

- **`cluster_overlapping_siblings` (R5):** $O(n \log n)$ per `(parent_event_id, clock_domain_id)` group; benchmarked on **1,000 sibling spans** in [`tests/perf/perf_concurrency_poset.py`](../../tests/perf/perf_concurrency_poset.py).
- **`decompose_causal_poset` (SP-Poset + Gallai Prime Decomposition):** Bitmask-accelerated transitive closure and complement-graph component extraction; benchmarked on **100-node fork-join DAGs** in [`tests/perf/perf_concurrency_poset.py`](../../tests/perf/perf_concurrency_poset.py).
- **Property-Based Fuzz Verification:** Exhaustive Hypothesis invariants covering clock-domain isolation, exact overlap-graph connected-component equivalence, R6 fan-out compaction provenance, Grabowski/Valdes–Tarjan–Lawler N-free characterization (`zero PRIME nodes ⇔ find_induced_n_witness is None`), and lossless causal reachability reconstruction live in [`tests/fuzz/fuzz_concurrency_poset.py`](../../tests/fuzz/fuzz_concurrency_poset.py).
