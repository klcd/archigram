"""Hypothesis property-based fuzz suite for R1-R6 + SP-Poset Interaction Trace Compaction."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EventStatus,
    InteractionTraceIR,
    NodeKind,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import TraceCompactionConfig
from archigram.features.reduce_trace import (
    TraceCompactionEngine,
    TraceFragmentKind,
    collect_trace_backing_ids,
)
from archigram.ports.contracts import TraceCompactorPort

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="otel_trace",
    input_sha256="a" * 64,
)


def _make_participants(ids: tuple[str, ...]) -> dict[str, TraceParticipant]:
    return {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc.{pid}",
            label=pid.upper(),
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )
        for pid in ids
    }


@settings(max_examples=25)
@given(
    loop_iters=st.integers(min_value=500, max_value=520),
    fanout_degree=st.integers(min_value=50, max_value=60),
    max_par_branches=st.integers(min_value=2, max_value=8),
)
def test_500_iteration_loops_and_50_way_fanouts_compact_deterministically_with_full_provenance(
    loop_iters: int,
    fanout_degree: int,
    max_par_branches: int,
) -> None:
    participants = _make_participants(("gateway", "auth", "shard_worker"))
    events: dict[str, TraceEvent] = {}

    root = TraceEvent(
        event_id="ev_root",
        caller_id="gateway",
        callee_id="gateway",
        signature="POST /batch",
        parent_event_id=None,
        clock_domain_id="hostA:1",
        start_ns=0,
        end_ns=100_000_000,
        provenance=_OBS_PROV,
    )
    events[root.event_id] = root

    cursor = 1_000
    for idx in range(loop_iters):
        eid = f"ev_loop_{idx:04d}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="gateway",
            callee_id="auth",
            signature="verify_token",
            parent_event_id="ev_root",
            clock_domain_id="hostA:1",
            start_ns=cursor,
            end_ns=cursor + 500,
            provenance=_OBS_PROV,
        )
        cursor += 1_000

    fanout_start = cursor + 10_000
    for idx in range(fanout_degree):
        eid = f"ev_fan_{idx:04d}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="gateway",
            callee_id="shard_worker",
            signature="fetch_shard",
            parent_event_id="ev_root",
            clock_domain_id="hostA:1",
            start_ns=fanout_start + idx * 10,
            end_ns=fanout_start + 50_000,
            provenance=_OBS_PROV,
        )

    trace = InteractionTraceIR(
        trace_id="trace_500_50",
        participants=participants,
        events=events,
    )
    config = TraceCompactionConfig(
        fold_loops=True,
        fold_parallel_siblings=True,
        fold_parallel_map=True,
        max_par_branches=max_par_branches,
        parallel_epsilon_ns=100,
    )
    engine = TraceCompactionEngine()
    assert isinstance(engine, TraceCompactorPort)

    compacted, fragments = engine.compact_with_fragments(trace, config)
    compacted_again = engine.compact(trace, config)
    assert compacted == compacted_again

    # 1 root + 1 folded loop event + 1 folded parallel map event = 3 events
    assert len(compacted.events) == 3
    assert collect_trace_backing_ids(compacted) == frozenset(events.keys())

    loop_events = [
        ev for ev in compacted.events.values() if ev.attributes.get("loop_count") == str(loop_iters)
    ]
    assert len(loop_events) == 1
    assert loop_events[0].provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
    assert len(loop_events[0].provenance.backing_ids) == loop_iters

    map_events = [
        ev
        for ev in compacted.events.values()
        if ev.attributes.get("parallel_map_count") == str(fanout_degree)
    ]
    assert len(map_events) == 1
    assert map_events[0].provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
    assert len(map_events[0].provenance.backing_ids) == fanout_degree
    assert len(fragments) == 1
    child_kinds = {c.kind for c in fragments[0].children}
    assert TraceFragmentKind.LOOP in child_kinds
    assert TraceFragmentKind.PARALLEL_MAP in child_kinds


@settings(max_examples=60)
@given(
    span_count=st.integers(min_value=2, max_value=14),
    distinct_clocks=st.booleans(),
    sequential_gaps=st.booleans(),
    epsilon_ns=st.integers(min_value=0, max_value=50),
)
def test_r5_clock_domain_isolation_and_overlap_precision(
    span_count: int,
    distinct_clocks: bool,
    sequential_gaps: bool,
    epsilon_ns: int,
) -> None:
    participants = _make_participants(("api", "db", "cache"))
    events: dict[str, TraceEvent] = {}

    for idx in range(span_count):
        clock_id = f"clock_{idx}" if distinct_clocks else "clock_shared"
        if sequential_gaps:
            start_ns = idx * (1_000 + epsilon_ns)
            end_ns = start_ns + 500
        else:
            start_ns = 100 + idx * 5
            end_ns = start_ns + 5_000 + epsilon_ns

        eid = f"ev_{idx:02d}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="api",
            callee_id="db" if idx % 2 == 0 else "cache",
            signature=f"op_{idx}",
            parent_event_id=None,
            clock_domain_id=clock_id,
            start_ns=start_ns,
            end_ns=end_ns,
            provenance=_OBS_PROV,
        )

    trace = InteractionTraceIR(participants=participants, events=events)
    config = TraceCompactionConfig(
        fold_loops=False,
        fold_parallel_siblings=True,
        fold_parallel_map=False,
        decompose_series_parallel=False,
        parallel_epsilon_ns=epsilon_ns,
    )
    engine = TraceCompactionEngine()
    compacted, fragments = engine.compact_with_fragments(trace, config)

    par_tagged = [ev for ev in compacted.events.values() if "par_group" in ev.attributes]
    if distinct_clocks or sequential_gaps:
        assert par_tagged == []
        assert all(f.kind is not TraceFragmentKind.PAR for f in fragments)
    else:
        assert len(par_tagged) == span_count
        assert all(ev.sync_kind is SyncKind.FORK_JOIN_BRANCH for ev in par_tagged)
        assert len(fragments) == 1
        assert fragments[0].kind is TraceFragmentKind.PAR


@settings(max_examples=50)
@given(
    base_time=st.integers(min_value=0, max_value=10_000),
    extra_tail=st.booleans(),
)
def test_induced_n_prime_poset_lowers_to_async_fire_forget_without_cycles(
    base_time: int,
    extra_tail: bool,
) -> None:
    participants = _make_participants(("p_a", "p_b", "p_c", "p_d"))
    # Induced-N zig-zag poset: a < c, b < c, b < d
    ev_a = TraceEvent(
        event_id="ev_a",
        caller_id="p_a",
        callee_id="p_b",
        signature="step_a",
        clock_domain_id="clock_1",
        start_ns=base_time + 10,
        end_ns=base_time + 100,
        provenance=_OBS_PROV,
    )
    ev_b = TraceEvent(
        event_id="ev_b",
        caller_id="p_b",
        callee_id="p_c",
        signature="step_b",
        clock_domain_id="clock_2",
        start_ns=base_time + 20,
        end_ns=base_time + 120,
        provenance=_OBS_PROV,
    )
    ev_c = TraceEvent(
        event_id="ev_c",
        caller_id="p_c",
        callee_id="p_d",
        signature="step_c",
        causal_links=("ev_a", "ev_b"),
        clock_domain_id="clock_1",
        start_ns=base_time + 200,
        end_ns=base_time + 300,
        provenance=_OBS_PROV,
    )
    ev_d = TraceEvent(
        event_id="ev_d",
        caller_id="p_d",
        callee_id="p_a",
        signature="step_d",
        causal_links=("ev_b",),
        clock_domain_id="clock_2",
        start_ns=base_time + 210,
        end_ns=base_time + 310,
        provenance=_OBS_PROV,
    )
    events = {"ev_a": ev_a, "ev_b": ev_b, "ev_c": ev_c, "ev_d": ev_d}
    if extra_tail:
        events["ev_tail"] = TraceEvent(
            event_id="ev_tail",
            caller_id="p_a",
            callee_id="p_d",
            signature="finalize",
            causal_links=("ev_c", "ev_d"),
            clock_domain_id="clock_1",
            start_ns=base_time + 400,
            end_ns=base_time + 500,
            provenance=_OBS_PROV,
        )

    trace = InteractionTraceIR(participants=participants, events=events)
    config = TraceCompactionConfig(
        fold_loops=False,
        fold_parallel_siblings=False,
        fold_parallel_map=False,
        decompose_series_parallel=True,
    )
    engine = TraceCompactionEngine()
    compacted = engine.compact(trace, config)

    for wid in ("ev_a", "ev_b", "ev_c", "ev_d"):
        ev = compacted.events[wid]
        assert ev.attributes.get("poset_module") == "prime"
        assert ev.attributes.get("prime_async_lowered") == "true"
        assert ev.sync_kind is SyncKind.ASYNC_FIRE_FORGET

    assert compacted.events["ev_c"].causal_links == ("ev_a", "ev_b")
    assert compacted.events["ev_d"].causal_links == ("ev_b",)
    assert collect_trace_backing_ids(compacted) == frozenset(events.keys())


@settings(max_examples=50)
@given(
    recursion_depth=st.integers(min_value=3, max_value=12),
    mutual=st.booleans(),
    max_depth=st.integers(min_value=3, max_value=6),
    with_opt_leaf=st.booleans(),
)
def test_r4_recursion_cut_depth_truncation_and_r2_r3_loop_with_opt_compaction(
    recursion_depth: int,
    mutual: bool,
    max_depth: int,
    with_opt_leaf: bool,
) -> None:
    participants = _make_participants(("svc_a", "svc_b", "svc_cache"))
    events: dict[str, TraceEvent] = {}

    # Chain of recursive calls
    parent_id: str | None = None
    for idx in range(recursion_depth):
        eid = f"rec_{idx:02d}"
        if mutual:
            caller = "svc_a" if idx % 2 == 0 else "svc_b"
            callee = "svc_b" if idx % 2 == 0 else "svc_a"
        else:
            caller = "svc_a"
            callee = "svc_a"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=caller,
            callee_id=callee,
            signature="compute_tree",
            parent_event_id=parent_id,
            clock_domain_id="hostA:1",
            start_ns=idx * 100,
            end_ns=(recursion_depth * 200) - idx * 10,
            status=EventStatus.ERROR if idx == recursion_depth - 1 else EventStatus.OK,
            provenance=_OBS_PROV,
        )
        parent_id = eid

    # Two sequential multi-call subtrees at root for R2 / R3 loop folding
    for iter_idx in range(3):
        root_id = f"iter_{iter_idx}"
        base_ns = 100_000 + iter_idx * 10_000
        events[root_id] = TraceEvent(
            event_id=root_id,
            caller_id="svc_a",
            callee_id="svc_b",
            signature="handle_item",
            parent_event_id=None,
            clock_domain_id="hostA:1",
            start_ns=base_ns,
            end_ns=base_ns + 5_000,
            provenance=_OBS_PROV,
        )
        child_id = f"iter_{iter_idx}_db"
        events[child_id] = TraceEvent(
            event_id=child_id,
            caller_id="svc_b",
            callee_id="svc_a",
            signature="query_item",
            parent_event_id=root_id,
            clock_domain_id="hostA:1",
            start_ns=base_ns + 100,
            end_ns=base_ns + 2_000,
            provenance=_OBS_PROV,
        )
        if with_opt_leaf and iter_idx == 1:
            opt_id = f"iter_{iter_idx}_opt_cache"
            events[opt_id] = TraceEvent(
                event_id=opt_id,
                caller_id="svc_b",
                callee_id="svc_cache",
                signature="populate_cache",
                parent_event_id=root_id,
                clock_domain_id="hostA:1",
                start_ns=base_ns + 2_500,
                end_ns=base_ns + 3_500,
                provenance=_OBS_PROV,
            )

    trace = InteractionTraceIR(participants=participants, events=events)
    config = TraceCompactionConfig(
        cut_recursion=True,
        max_call_depth=max_depth,
        fold_loops=True,
        fold_loop_with_opt=True,
        fold_parallel_siblings=True,
        fold_parallel_map=True,
    )
    engine = TraceCompactionEngine()
    compacted = engine.compact(trace, config)

    # 100% of original event_ids must be accounted for in surviving events + backing_ids
    assert collect_trace_backing_ids(compacted) == frozenset(events.keys())

    # Recursive chain was cut
    rec_cuts = [
        ev for ev in compacted.events.values() if ev.attributes.get("recursion_cut") == "true"
    ]
    assert len(rec_cuts) == 1
    assert rec_cuts[0].status is EventStatus.ERROR

    # 3 iterations of handle_item were folded into 1 loop representative
    loop_reps = [ev for ev in compacted.events.values() if ev.attributes.get("loop_count") == "3"]
    assert len(loop_reps) == 1
    if with_opt_leaf:
        assert "opt_condition" in loop_reps[0].attributes


@settings(max_examples=80)
@given(
    ev_count=st.integers(min_value=2, max_value=18),
    sync_kinds=st.lists(st.sampled_from(list(SyncKind)), min_size=18, max_size=18),
    parent_choices=st.lists(st.integers(min_value=-1, max_value=16), min_size=18, max_size=18),
    starts=st.lists(st.integers(min_value=0, max_value=300), min_size=18, max_size=18),
    flags=st.tuples(st.booleans(), st.booleans(), st.booleans()),
)
def test_mixed_sync_kind_compaction_never_upgrades_async_backing_events(
    ev_count: int,
    sync_kinds: list[SyncKind],
    parent_choices: list[int],
    starts: list[int],
    flags: tuple[bool, bool, bool],
) -> None:
    fold_loops, fold_opt, cut_rec = flags
    participants = _make_participants(("svc_0", "svc_1"))
    e_ids = [f"ev_{idx:02d}" for idx in range(ev_count)]
    events: dict[str, TraceEvent] = {}
    for idx, eid in enumerate(e_ids):
        p_raw = parent_choices[idx]
        parent_id = e_ids[p_raw % idx] if idx > 0 and p_raw >= 0 else None
        start_ns = starts[idx]
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="svc_0",
            callee_id="svc_1" if idx % 3 == 1 else "svc_0",
            signature="rpc_call()",
            parent_event_id=parent_id,
            clock_domain_id="clk_a",
            start_ns=start_ns,
            end_ns=start_ns + 10,
            sync_kind=sync_kinds[idx],
            provenance=_OBS_PROV,
        )

    trace = InteractionTraceIR(participants=participants, events=events)
    config = TraceCompactionConfig(
        fold_loops=fold_loops,
        fold_loop_with_opt=fold_opt,
        cut_recursion=cut_rec,
        fold_parallel_siblings=True,
        fold_parallel_map=True,
        max_call_depth=4,
    )
    compacted = TraceCompactionEngine().compact(trace, config)

    assert collect_trace_backing_ids(compacted) == frozenset(events.keys())
    for surv in compacted.events.values():
        if surv.sync_kind is SyncKind.SYNC:
            backing_ids = (*surv.provenance.backing_ids, surv.event_id)
            assert all(
                events[bid].sync_kind is not SyncKind.ASYNC_FIRE_FORGET
                for bid in backing_ids
                if bid in events
            )
