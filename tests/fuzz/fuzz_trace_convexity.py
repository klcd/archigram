"""Hypothesis fuzz tests verifying trace contractions remain convex and cycle-free."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.concurrency import decompose_causal_poset
from archigram.domain.ir import InteractionTraceIR, TraceEvent, TraceParticipant
from archigram.domain.spec import (
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
)
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.reduce_trace import TraceCompactionEngine, collect_trace_backing_ids

_OBS = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz-convexity",
    input_sha256="e" * 64,
)
_VIEW_ID = "convex-trace-view"


def _ev(
    eid: str,
    *,
    start_ns: int,
    end_ns: int,
    parent: str | None = None,
    links: tuple[str, ...] = (),
    sig: str = "step()",
) -> TraceEvent:
    return TraceEvent(
        event_id=eid,
        caller_id="p0",
        callee_id="p1",
        signature=sig,
        parent_event_id=parent,
        causal_links=links,
        clock_domain_id="clk0",
        start_ns=start_ns,
        end_ns=end_ns,
        provenance=_OBS,
    )


def _ir(*events: TraceEvent) -> InteractionTraceIR:
    participants = {
        pid: TraceParticipant(
            participant_id=pid, symbol_id=f"svc/{pid}", label=pid, provenance=_OBS
        )
        for pid in ("p0", "p1")
    }
    return InteractionTraceIR(participants=participants, events={e.event_id: e for e in events})


def _dag_edges(ir: InteractionTraceIR) -> list[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for eid, ev in ir.events.items():
        if ev.parent_event_id is not None and ev.parent_event_id in ir.events:
            edges.add((ev.parent_event_id, eid))
        for link in ev.causal_links:
            if link in ir.events:
                edges.add((link, eid))
    return sorted(edges)


def _assert_acyclic_and_valid(observed: InteractionTraceIR, compacted: InteractionTraceIR) -> None:
    node_ids = sorted(compacted.events.keys())
    decompose_causal_poset(node_ids, _dag_edges(compacted))
    assert collect_trace_backing_ids(compacted) == frozenset(observed.events.keys())

    view = DiagramViewSpec(
        id=_VIEW_ID,
        title="Convexity Gate 3 View",
        ir_mode=IRMode.INTERACTION_TRACE,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(preserve_partial_order=True),
    )
    report = TopologicalInvariantGate().check(
        observed, CurationSpecV1(views=(view,)), {_VIEW_ID: compacted}
    )
    assert report.passed is True, report.diagnostics


def _all_rules_config(
    *,
    max_par_branches: int = 2,
    max_call_depth: int | None = None,
    cut_recursion: bool = True,
) -> TraceCompactionConfig:
    return TraceCompactionConfig(
        fold_loops=True,
        fold_loop_with_opt=True,
        cut_recursion=cut_recursion,
        fold_parallel_siblings=True,
        parallel_epsilon_ns=1_000,
        fold_parallel_map=True,
        max_par_branches=max_par_branches,
        decompose_series_parallel=True,
        max_call_depth=max_call_depth,
    )


@settings(max_examples=50, deadline=None)
@given(
    prefix_len=st.integers(min_value=2, max_value=5),
    suffix_len=st.integers(min_value=2, max_value=5),
    with_child=st.booleans(),
    bridge_at_child=st.booleans(),
)
def test_loop_non_convex_bridge_splits_without_cycle(
    prefix_len: int,
    suffix_len: int,
    with_child: bool,
    bridge_at_child: bool,
) -> None:
    """External bridge ``it_{prefix-1} -> ext -> it_{prefix}`` must split instead of cycling."""
    total = prefix_len + suffix_len
    events: list[TraceEvent] = [_ev("root", start_ns=0, end_ns=100_000, sig="root()")]
    pre_target = (
        f"it{prefix_len - 1}_c" if (with_child and bridge_at_child) else f"it{prefix_len - 1}"
    )
    ext_ns = prefix_len * 1_000 + 500
    events.append(
        _ev(
            "ext",
            start_ns=ext_ns,
            end_ns=ext_ns + 100,
            parent=None,
            links=(pre_target,),
            sig="ext()",
        )
    )

    for idx in range(total):
        t0 = idx * 1_000 + 100
        it_links = ("ext",) if (idx == prefix_len and not (with_child and bridge_at_child)) else ()
        events.append(
            _ev(
                f"it{idx}",
                start_ns=t0,
                end_ns=t0 + 300,
                parent="root",
                links=it_links,
                sig="step()",
            )
        )
        if with_child:
            c_links = ("ext",) if (idx == prefix_len and bridge_at_child) else ()
            events.append(
                _ev(
                    f"it{idx}_c",
                    start_ns=t0 + 20,
                    end_ns=t0 + 200,
                    parent=f"it{idx}",
                    links=c_links,
                    sig="leaf()",
                )
            )

    observed = _ir(*events)
    compacted = TraceCompactionEngine().compact(observed, _all_rules_config())

    _assert_acyclic_and_valid(observed, compacted)
    assert len(compacted.events) < len(observed.events)


@settings(max_examples=40, deadline=None)
@given(
    iterations=st.integers(min_value=2, max_value=6), rep_idx=st.integers(min_value=0, max_value=5)
)
def test_loop_opt_leaf_reentry_link_never_induces_cycle(iterations: int, rep_idx: int) -> None:
    """R3 ``opt_leaf`` stays alive under ``rep``, so ``opt_leaf -> sibling`` must not fold."""
    rep_pos = rep_idx % iterations
    other_pos = (rep_pos + 1) % iterations
    events: list[TraceEvent] = [_ev("root", start_ns=0, end_ns=100_000, sig="root()")]
    for idx in range(iterations):
        t0 = (idx + 1) * 1_000
        links = ("opt_leaf",) if idx == other_pos else ()
        events.append(
            _ev(f"it{idx}", start_ns=t0, end_ns=t0 + 400, parent="root", links=links, sig="iter()")
        )
        events.append(
            _ev(
                f"it{idx}_c",
                start_ns=t0 + 10,
                end_ns=t0 + 200,
                parent=f"it{idx}",
                sig="inner()",
            )
        )
        if idx == rep_pos:
            events.append(
                _ev(
                    "opt_leaf",
                    start_ns=t0 + 210,
                    end_ns=t0 + 300,
                    parent=f"it{idx}",
                    sig="maybe()",
                )
            )

    observed = _ir(*events)
    compacted = TraceCompactionEngine().compact(observed, _all_rules_config())

    _assert_acyclic_and_valid(observed, compacted)


@settings(max_examples=50, deadline=None)
@given(
    pre=st.integers(min_value=2, max_value=5),
    post=st.integers(min_value=2, max_value=5),
    poison_first=st.booleans(),
)
def test_parallel_map_non_convex_bridge_never_induces_cycle(
    pre: int,
    post: int,
    poison_first: bool,
) -> None:
    """External bridges between overlapping fanout workers must exclude conflicting branches."""
    total = pre + post
    events: list[TraceEvent] = [_ev("root", start_ns=0, end_ns=100_000, sig="root()")]
    ext_links = ("m0",) if poison_first else tuple(f"m{i}" for i in range(pre))
    events.append(
        _ev("ext", start_ns=2_500, end_ns=2_600, parent=None, links=ext_links, sig="barrier()")
    )
    for idx in range(total):
        reentry = (poison_first and idx > 0) or (not poison_first and idx >= pre)
        events.append(
            _ev(
                f"m{idx}",
                start_ns=1_000 + idx,
                end_ns=2_000 + idx,
                parent="root",
                links=("ext",) if reentry else (),
                sig="work()",
            )
        )

    observed = _ir(*events)
    compacted = TraceCompactionEngine().compact(observed, _all_rules_config(max_par_branches=2))

    _assert_acyclic_and_valid(observed, compacted)
    if poison_first and post >= 2:
        assert "m0" in compacted.events
        assert len(compacted.events) < len(observed.events)


@settings(max_examples=40, deadline=None)
@given(
    depth=st.integers(min_value=3, max_value=8),
    max_depth=st.integers(min_value=1, max_value=3),
    cut_recursion=st.booleans(),
)
def test_recursion_and_depth_cut_non_convex_subtree_stays_acyclic(
    depth: int,
    max_depth: int,
    cut_recursion: bool,
) -> None:
    """Subtree exited by ``c0 -> ext`` and re-entered at ``ext -> c_{last}`` skips unsafe cuts."""
    events: list[TraceEvent] = [_ev("root", start_ns=0, end_ns=100_000, sig="root()")]
    events.append(_ev("ext", start_ns=50, end_ns=80, parent=None, links=("c0",), sig="ext()"))
    for level in range(depth):
        t0 = 100 + level * 10
        sig = "recur()" if cut_recursion else f"lvl_{level}()"
        links = ("ext",) if level == depth - 1 else ()
        events.append(
            _ev(
                f"c{level}",
                start_ns=t0,
                end_ns=t0 + 200,
                parent=f"c{level - 1}" if level else "root",
                links=links,
                sig=sig,
            )
        )

    observed = _ir(*events)
    cfg = _all_rules_config(max_call_depth=max_depth, cut_recursion=cut_recursion)
    compacted = TraceCompactionEngine().compact(observed, cfg)

    _assert_acyclic_and_valid(observed, compacted)


@settings(max_examples=80, deadline=None)
@given(data=st.data())
def test_random_cyclic_trap_dag_always_compacts_safely(data: st.DataObject) -> None:
    """Randomized topological forests with cross-subtree ``causal_links`` remain valid DAGs."""
    n = data.draw(st.integers(min_value=4, max_value=18))
    overlap = data.draw(st.booleans())
    sigs = ("step()", "recur()", "work()", "aux()")

    events: list[TraceEvent] = []
    for idx in range(n):
        parent = (
            None
            if idx == 0 or data.draw(st.booleans())
            else f"e{data.draw(st.integers(min_value=0, max_value=idx - 1))}"
        )
        prior_ids = [f"e{p}" for p in range(idx) if f"e{p}" != parent]
        links_list: list[str] = []
        if prior_ids:
            link_count = data.draw(st.integers(min_value=0, max_value=min(3, len(prior_ids))))
            if link_count:
                links_list = data.draw(
                    st.lists(
                        st.sampled_from(prior_ids),
                        min_size=link_count,
                        max_size=link_count,
                        unique=True,
                    )
                )
        t0 = (1_000 + idx) if overlap else (idx * 100 + 10)
        t1 = (2_000 + idx) if overlap else (t0 + 50)
        events.append(
            _ev(
                f"e{idx}",
                start_ns=t0,
                end_ns=t1,
                parent=parent,
                links=tuple(links_list),
                sig=data.draw(st.sampled_from(sigs)),
            )
        )

    observed = _ir(*events)
    cfg = TraceCompactionConfig(
        fold_loops=data.draw(st.booleans()),
        fold_loop_with_opt=data.draw(st.booleans()),
        cut_recursion=data.draw(st.booleans()),
        fold_parallel_siblings=data.draw(st.booleans()),
        parallel_epsilon_ns=1_000,
        fold_parallel_map=data.draw(st.booleans()),
        max_par_branches=data.draw(st.integers(min_value=2, max_value=4)),
        decompose_series_parallel=data.draw(st.booleans()),
        max_call_depth=data.draw(st.one_of(st.none(), st.integers(min_value=1, max_value=4))),
    )
    compacted = TraceCompactionEngine().compact(observed, cfg)

    _assert_acyclic_and_valid(observed, compacted)
