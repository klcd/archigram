"""Hypothesis fuzz suite for the runtime trace-compaction ordering contract (Gate 3).

Canonical cases (see ``docs/architecture/L3-runtime-traces.md``):

1. Reversed order  -> REJECTED ``PARTIAL_ORDER_VIOLATED``       (backing IDs named)
2. Invented order  -> REJECTED ``PARTIAL_ORDER_FALSE_BARRIER``  (backing IDs named)
3. Dropped order   -> REJECTED ``PARTIAL_ORDER_LOST_EDGE``      (backing IDs named)
4. Legitimate contraction (loops, recursion, depth cuts, parallel maps, bypassed middle nodes)
   -> ACCEPTED, with full backing coverage.
5. Async fire-and-forget -> ACCEPTED when spawn precedence holds and the continuation is not
   joined; REJECTED (``ASYNC_UPGRADED_TO_SYNC`` / ``PARTIAL_ORDER_FALSE_BARRIER``) otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    InteractionTraceIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
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

if TYPE_CHECKING:
    from archigram.domain.diagnostics import LintReport

_OBS = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz-trace-guarantees",
    input_sha256="c" * 64,
)
_VIEW_ID = "trace-view"
_PO_CODES = frozenset(
    {
        "PARTIAL_ORDER_VIOLATED",
        "PARTIAL_ORDER_FALSE_BARRIER",
        "PARTIAL_ORDER_LOST_EDGE",
        "ASYNC_UPGRADED_TO_SYNC",
    }
)


def _ev(
    eid: str,
    *,
    idx: int,
    parent: str | None = None,
    links: tuple[str, ...] = (),
    backing: tuple[str, ...] | None = None,
    sig: str | None = None,
) -> TraceEvent:
    """Build a sequential, non-overlapping event; ``backing`` makes it a derived representative."""
    provenance = (
        _OBS
        if backing is None
        else ProvenanceStamp(
            grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
            rule_id="fuzz-rep",
            backing_ids=backing,
        )
    )
    return TraceEvent(
        event_id=eid,
        caller_id="p0",
        callee_id="p1",
        signature=sig or f"op_{eid}()",
        parent_event_id=parent,
        causal_links=links,
        clock_domain_id="clk0",
        start_ns=idx * 10,
        end_ns=idx * 10 + 5,
        provenance=provenance,
    )


def _ir(*events: TraceEvent) -> InteractionTraceIR:
    participants = {
        pid: TraceParticipant(
            participant_id=pid, symbol_id=f"svc/{pid}", label=pid, provenance=_OBS
        )
        for pid in ("p0", "p1")
    }
    return InteractionTraceIR(participants=participants, events={e.event_id: e for e in events})


def _gate3(observed: InteractionTraceIR, curated: InteractionTraceIR) -> LintReport:
    view = DiagramViewSpec(
        id=_VIEW_ID,
        title="Trace View",
        ir_mode=IRMode.INTERACTION_TRACE,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(preserve_partial_order=True),
    )
    spec = CurationSpecV1(views=(view,))
    return TopologicalInvariantGate().check(observed, spec, {_VIEW_ID: curated})


def _codes(report: LintReport) -> set[str]:
    return {d.code for d in report.diagnostics}


def _chain(n: int) -> list[TraceEvent]:
    """Observed causal chain ev0 -> ev1 -> ... via parent_event_id."""
    return [
        _ev(f"ev{i}", idx=i, parent=f"ev{i - 1}" if i else None, sig=f"op_{i}()") for i in range(n)
    ]


def _compact(trace: InteractionTraceIR, **overrides: object) -> InteractionTraceIR:
    base: dict[str, object] = {
        "fold_loops": False,
        "fold_loop_with_opt": False,
        "cut_recursion": False,
        "fold_parallel_siblings": False,
        "fold_parallel_map": False,
        "decompose_series_parallel": False,
        "max_call_depth": None,
    }
    config = TraceCompactionConfig.model_validate({**base, **overrides})
    return TraceCompactionEngine().compact(trace, config)


# ---------------------------------------------------------------------------
# Case 1: reversed order
# ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(n=st.integers(min_value=2, max_value=8), via_backing=st.booleans())
def test_case1_reversed_order_is_rejected_with_backing_ids(n: int, via_backing: bool) -> None:
    """Observed ``first -> last``; curated asserts ``last -> first`` => PARTIAL_ORDER_VIOLATED."""
    observed = _ir(*_chain(n))
    last = f"ev{n - 1}"
    if via_backing:
        curated = _ir(
            _ev("rep_last", idx=0, backing=(last,)),
            _ev("rep_first", idx=1, links=("rep_last",), backing=("ev0",)),
        )
    else:
        curated = _ir(_ev(last, idx=0), _ev("ev0", idx=1, links=(last,)))

    report = _gate3(observed, curated)

    assert report.passed is False
    assert _codes(report) == {"PARTIAL_ORDER_VIOLATED"}
    symbols = {s for d in report.diagnostics for s in d.offending_symbols}
    assert {"ev0", last} <= symbols
    if via_backing:
        assert {"rep_first", "rep_last"} <= symbols


# ---------------------------------------------------------------------------
# Case 2: invented order
# ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    k=st.integers(min_value=2, max_value=6),
    pair=st.tuples(st.integers(0, 5), st.integers(0, 5)),
    via_parent=st.booleans(),
    via_backing=st.booleans(),
)
def test_case2_invented_order_is_rejected_with_backing_ids(
    k: int, pair: tuple[int, int], via_parent: bool, via_backing: bool
) -> None:
    """Observed concurrent siblings ``a || b``; curated adds ``a -> b`` => FALSE_BARRIER."""
    i, j = pair[0] % k, pair[1] % k
    if i == j:
        j = (j + 1) % k
    observed = _ir(
        _ev("root", idx=0),
        *[_ev(f"s{x}", idx=x + 1, parent="root") for x in range(k)],
    )

    def sibling(x: int) -> TraceEvent:
        eid = f"r{x}" if via_backing else f"s{x}"
        backing = (f"s{x}",) if via_backing else None
        parent = "root"
        links: tuple[str, ...] = ()
        if x == j:
            src = f"r{i}" if via_backing else f"s{i}"
            if via_parent:
                parent = src
            else:
                links = (src,)
        return _ev(eid, idx=x + 1, parent=parent, links=links, backing=backing)

    curated = _ir(_ev("root", idx=0), *[sibling(x) for x in range(k)])
    report = _gate3(observed, curated)

    assert report.passed is False
    assert _codes(report) == {"PARTIAL_ORDER_FALSE_BARRIER"}
    (diag,) = report.diagnostics
    prefix = "r" if via_backing else "s"
    assert diag.witness_path == (f"{prefix}{i}", f"{prefix}{j}")
    assert {f"s{i}", f"s{j}"} <= set(diag.offending_symbols)


# ---------------------------------------------------------------------------
# Case 3: dropped order
# ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(n=st.integers(min_value=3, max_value=9), cut=st.integers(min_value=0, max_value=7))
def test_case3_dropped_edge_in_surviving_chain_reports_single_minimal_pair(
    n: int, cut: int
) -> None:
    """Observed chain, curated keeps every event but severs one link => one LOST_EDGE."""
    cut %= n - 1
    curated_events = [
        _ev(
            f"ev{i}",
            idx=i,
            parent=f"ev{i - 1}" if i and i != cut + 1 else None,
            sig=f"op_{i}()",
        )
        for i in range(n)
    ]
    report = _gate3(_ir(*_chain(n)), _ir(*curated_events))

    assert report.passed is False
    assert _codes(report) == {"PARTIAL_ORDER_LOST_EDGE"}
    (diag,) = report.diagnostics  # minimal pair only: no O(N^2) transitive noise
    assert diag.offending_symbols == (f"ev{cut}", f"ev{cut + 1}")
    assert diag.witness_path == (f"ev{cut}", f"ev{cut + 1}")


@settings(max_examples=60, deadline=None)
@given(n=st.integers(min_value=3, max_value=10), split=st.integers(min_value=1, max_value=8))
def test_case3_dropped_order_between_distinct_representatives_names_backing(
    n: int, split: int
) -> None:
    """Observed ``a -> mid -> b``: two reps survive with no link => LOST_EDGE on backing IDs."""
    split = 1 + (split - 1) % (n - 1)
    head = tuple(f"ev{i}" for i in range(split))
    tail = tuple(f"ev{i}" for i in range(split, n))
    curated = _ir(
        _ev("rep_head", idx=0, backing=head),
        _ev("rep_tail", idx=1, backing=tail),
    )
    report = _gate3(_ir(*_chain(n)), curated)

    assert report.passed is False
    assert _codes(report) == {"PARTIAL_ORDER_LOST_EDGE"}
    (diag,) = report.diagnostics
    assert diag.offending_symbols[:2] == ("rep_head", "rep_tail")
    assert set(diag.offending_symbols[2:]) <= {*head, *tail}
    assert diag.offending_symbols[2] in head
    assert diag.offending_symbols[3] in tail


# ---------------------------------------------------------------------------
# Case 4: legitimate contraction
# ---------------------------------------------------------------------------


def _assert_accepted(observed: InteractionTraceIR, curated: InteractionTraceIR) -> None:
    report = _gate3(observed, curated)
    assert not (_codes(report) & _PO_CODES), report.diagnostics
    assert report.passed is True
    assert frozenset(observed.events) <= collect_trace_backing_ids(curated)


@settings(max_examples=60, deadline=None)
@given(
    iterations=st.integers(min_value=2, max_value=8),
    linked_mask=st.integers(min_value=1, max_value=255),
    with_child=st.booleans(),
)
def test_case4_loop_folding_carries_links_of_folded_iterations(
    iterations: int, linked_mask: int, with_child: bool
) -> None:
    """Loop iterations that depend on an earlier event fold without dropping that order."""
    events = [_ev("root", idx=0), _ev("pre", idx=1, parent="root")]
    idx = 2
    for it in range(iterations):
        linked = bool(linked_mask >> it & 1)
        events.append(
            _ev(
                f"it{it}",
                idx=idx,
                parent="root",
                links=("pre",) if linked else (),
                sig="step()",
            )
        )
        idx += 1
        if with_child:
            events.append(_ev(f"it{it}_c", idx=idx, parent=f"it{it}", sig="leaf()"))
            idx += 1
    events.append(_ev("post", idx=idx, parent="root", links=(f"it{iterations - 1}",), sig="post()"))
    observed = _ir(*events)

    curated = _compact(observed, fold_loops=True, fold_loop_with_opt=True)

    assert len(curated.events) < len(observed.events)
    _assert_accepted(observed, curated)


@settings(max_examples=60, deadline=None)
@given(
    depth=st.integers(min_value=3, max_value=8),
    max_depth=st.integers(min_value=1, max_value=4),
    recursive=st.booleans(),
)
def test_case4_depth_and_recursion_cuts_keep_cross_representative_order(
    depth: int, max_depth: int, recursive: bool
) -> None:
    """Deep chain under ``root`` with a prior and a later sibling linked into the chain."""
    events = [_ev("root", idx=0), _ev("before", idx=1, parent="root", sig="before()")]
    for level in range(depth):
        events.append(
            _ev(
                f"c{level}",
                idx=2 + level,
                parent=f"c{level - 1}" if level else "root",
                links=("before",) if level == depth - 1 else (),
                sig="recur()" if recursive else f"lvl_{level}()",
            )
        )
    events.append(
        _ev("after", idx=2 + depth, parent="root", links=(f"c{depth - 1}",), sig="after()")
    )
    observed = _ir(*events)

    curated = _compact(observed, max_call_depth=max_depth, cut_recursion=recursive)

    _assert_accepted(observed, curated)


@settings(max_examples=60, deadline=None)
@given(mids=st.integers(min_value=1, max_value=5))
def test_case4_bypassed_middle_nodes_contract_into_representative(mids: int) -> None:
    """``a -> mid... -> b`` contracts to ``rep(a, mid) -> b`` via a depth cut at ``a``."""
    events = [_ev("root", idx=0), _ev("a", idx=1, parent="root", sig="a()")]
    for m in range(mids):
        events.append(_ev(f"mid{m}", idx=2 + m, parent=f"mid{m - 1}" if m else "a", sig=f"m{m}()"))
    events.append(_ev("b", idx=2 + mids, parent="root", links=(f"mid{mids - 1}",), sig="b()"))
    observed = _ir(*events)

    curated = _compact(observed, max_call_depth=2)

    assert "mid0" not in curated.events
    assert "a" in curated.events
    assert curated.events["b"].causal_links == ("a",)
    _assert_accepted(observed, curated)


@settings(max_examples=40, deadline=None)
@given(width=st.integers(min_value=6, max_value=14), with_child=st.booleans())
def test_case4_parallel_map_fold_preserves_join_order(width: int, with_child: bool) -> None:
    """Homogeneous overlapping fan-out folds to one rep; the joining event still follows it."""
    events = [_ev("root", idx=0)]
    for w in range(width):
        events.append(
            TraceEvent(
                event_id=f"w{w}",
                caller_id="p0",
                callee_id="p1",
                signature="work()",
                parent_event_id="root",
                clock_domain_id="clk0",
                start_ns=1_000 + w,
                end_ns=2_000 + w,
                provenance=_OBS,
            )
        )
        if with_child:
            events.append(
                TraceEvent(
                    event_id=f"w{w}_c",
                    caller_id="p1",
                    callee_id="p0",
                    signature="leaf()",
                    parent_event_id=f"w{w}",
                    clock_domain_id="clk0",
                    start_ns=1_100 + w,
                    end_ns=1_200 + w,
                    provenance=_OBS,
                )
            )
    events.append(
        TraceEvent(
            event_id="join",
            caller_id="p0",
            callee_id="p1",
            signature="join()",
            parent_event_id="root",
            causal_links=tuple(f"w{w}" for w in range(width)),
            clock_domain_id="clk0",
            start_ns=10_000,
            end_ns=10_005,
            provenance=_OBS,
        )
    )
    observed = _ir(*events)

    curated = _compact(
        observed,
        fold_parallel_siblings=True,
        fold_parallel_map=True,
        parallel_epsilon_ns=100,
    )

    assert len(curated.events) < len(observed.events)
    _assert_accepted(observed, curated)


_SIGS = st.sampled_from(("a()", "b()", "c()"))


@st.composite
def _random_trace(draw: st.DrawFn) -> InteractionTraceIR:
    """Random call forest under ``root``; links only point to *earlier* top-level subtrees.

    That direction rule keeps every contraction convex (no cycle can be induced), so any Gate 3
    ordering diagnostic on the compacted result is a genuine compaction bug.
    """
    tops = draw(st.integers(min_value=1, max_value=4))
    events = [_ev("root", idx=0)]
    members: list[list[str]] = []
    idx = 1
    for top in range(tops):
        group: list[str] = []
        for _ in range(draw(st.integers(min_value=1, max_value=7))):
            eid = f"t{top}_{len(group)}"
            parent = "root" if not group else draw(st.sampled_from(["root", *group]))
            earlier = [m for g in members for m in g]
            links = (draw(st.sampled_from(earlier)),) if earlier and draw(st.booleans()) else ()
            events.append(_ev(eid, idx=idx, parent=parent, links=links, sig=draw(_SIGS)))
            group.append(eid)
            idx += 1
        members.append(group)
    return _ir(*events)


@settings(max_examples=150, deadline=None)
@given(
    trace=_random_trace(),
    fold_loops=st.booleans(),
    fold_opt=st.booleans(),
    cut_recursion=st.booleans(),
    max_depth=st.one_of(st.none(), st.integers(min_value=1, max_value=4)),
)
def test_case4_random_compaction_never_violates_ordering_contract(
    trace: InteractionTraceIR,
    fold_loops: bool,
    fold_opt: bool,
    cut_recursion: bool,
    max_depth: int | None,
) -> None:
    """Any R1-R4/depth compaction of a random trace satisfies the ordering contract."""
    curated = _compact(
        trace,
        fold_loops=fold_loops,
        fold_loop_with_opt=fold_opt,
        cut_recursion=cut_recursion,
        max_call_depth=max_depth,
    )
    _assert_accepted(trace, curated)


# ---------------------------------------------------------------------------
# Case 5: async fire-and-forget
# ---------------------------------------------------------------------------


def _async_trace(continuation: int) -> list[TraceEvent]:
    """``caller`` spawns ``async_child`` and keeps running ``next0 -> next1 -> ...``."""
    events = [
        _ev("caller", idx=0),
        _ev("async_child", idx=1, parent="caller").model_copy(
            update={"sync_kind": SyncKind.ASYNC_FIRE_FORGET}
        ),
    ]
    for i in range(continuation):
        events.append(_ev(f"next{i}", idx=2 + i, parent=f"next{i - 1}" if i else "caller"))
    return events


@settings(max_examples=50, deadline=None)
@given(continuation=st.integers(min_value=1, max_value=5), compact=st.booleans())
def test_case5_async_spawn_with_concurrent_continuation_is_accepted(
    continuation: int, compact: bool
) -> None:
    """``caller -> async_child`` and ``async_child || next*``: no false barrier, no lost edge."""
    observed = _ir(*_async_trace(continuation))
    curated = (
        _compact(observed, fold_loops=True, decompose_series_parallel=True) if compact else observed
    )

    _assert_accepted(observed, curated)
    assert curated.events["async_child"].sync_kind is SyncKind.ASYNC_FIRE_FORGET


@settings(max_examples=50, deadline=None)
@given(continuation=st.integers(min_value=1, max_value=5), via_backing=st.booleans())
def test_case5_async_upgrade_to_sync_is_rejected_with_backing_ids(
    continuation: int, via_backing: bool
) -> None:
    """Curated ``SYNC`` event backed by ``ASYNC_FIRE_FORGET`` => ASYNC_UPGRADED_TO_SYNC."""
    observed = _ir(*_async_trace(continuation))
    child_id = "rep_child" if via_backing else "async_child"
    corrupted = [
        _ev("caller", idx=0),
        _ev(
            child_id,
            idx=1,
            parent="caller",
            backing=("async_child",) if via_backing else None,
        ),
        *[
            _ev(f"next{i}", idx=2 + i, parent=f"next{i - 1}" if i else "caller")
            for i in range(continuation)
        ],
    ]
    report = _gate3(observed, _ir(*corrupted))

    assert report.passed is False
    assert _codes(report) == {"ASYNC_UPGRADED_TO_SYNC"}
    (diag,) = report.diagnostics
    assert diag.offending_symbols[0] == child_id
    assert "async_child" in diag.offending_symbols


@settings(max_examples=50, deadline=None)
@given(continuation=st.integers(min_value=1, max_value=5))
def test_case5_inventing_join_on_async_child_is_rejected(continuation: int) -> None:
    """Curated ``async_child -> next0`` is a false barrier on the concurrent continuation."""
    observed = _ir(*_async_trace(continuation))
    curated_events = _async_trace(continuation)
    curated_events[2] = _ev("next0", idx=2, parent="caller", links=("async_child",))
    report = _gate3(observed, _ir(*curated_events))

    assert report.passed is False
    assert _codes(report) == {"PARTIAL_ORDER_FALSE_BARRIER"}
    (diag,) = report.diagnostics
    assert diag.witness_path == ("async_child", "next0")
    assert {"async_child", "next0"} <= set(diag.offending_symbols)


@settings(max_examples=40, deadline=None)
@given(base=st.integers(min_value=0, max_value=1_000), extra_tail=st.booleans())
def test_case5_prime_poset_lowering_to_async_preserves_causal_links(
    base: int, extra_tail: bool
) -> None:
    """Induced-N poset lowered to ASYNC_FIRE_FORGET keeps every link and passes Gate 3."""

    def ev(eid: str, at: int, links: tuple[str, ...]) -> TraceEvent:
        return TraceEvent(
            event_id=eid,
            caller_id="p0",
            callee_id="p1",
            signature=f"{eid}()",
            causal_links=links,
            clock_domain_id="clk0",
            start_ns=base + at,
            end_ns=base + at + 5,
            provenance=_OBS,
        )

    events = [ev("a", 10, ()), ev("b", 20, ()), ev("c", 200, ("a", "b")), ev("d", 210, ("b",))]
    if extra_tail:
        events.append(ev("tail", 400, ("c", "d")))
    observed = _ir(*events)

    curated = _compact(observed, decompose_series_parallel=True)

    for wid in "abcd":
        assert curated.events[wid].sync_kind is SyncKind.ASYNC_FIRE_FORGET
        assert curated.events[wid].causal_links == observed.events[wid].causal_links
    _assert_accepted(observed, curated)
