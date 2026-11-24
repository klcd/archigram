"""Hypothesis property-based fuzz suite for OpenTelemetry (OTLP) JSON trace ingestion."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade
from archigram.domain.concurrency import SpanInterval, cluster_overlapping_siblings
from archigram.domain.ir import EventStatus, NodeKind, SyncKind
from archigram.features.ingest_otel import OtelTraceIngestor
from archigram.ports.contracts import TraceIngestorPort


@dataclass(frozen=True)
class _GeneratedOtelTrace:
    """Fuzz fixture bundling an OTLP JSON dict with ground-truth semantic expectations."""

    payload: dict[str, Any]
    trace_id: str
    root_span_id: str
    root_clock_domain: str
    rpc_child_ids: tuple[str, ...]
    remote_leaf_id: str
    remote_host_clock_domain: str
    producer_span_id: str
    consumer_span_id: str
    fork_join_span_id: str
    db_span_id: str
    ext_span_id: str
    expected_status_map: dict[str, EventStatus]


def _kv_attr(key: str, val: str | int | bool) -> dict[str, Any]:
    if isinstance(val, bool):
        return {"key": key, "value": {"boolValue": val}}
    if isinstance(val, int):
        return {"key": key, "value": {"intValue": val}}
    return {"key": key, "value": {"stringValue": val}}


@st.composite
def otlp_trace_fixture_strategy(draw: st.DrawFn) -> _GeneratedOtelTrace:
    """Generate multi-resource OTLP JSON payloads with sync, async, links, and clock domains."""
    trace_id = draw(st.from_regex(r"^[0-9a-f]{16}$", fullmatch=True))
    host_a = draw(st.from_regex(r"^host-a-[0-9]{1,3}$", fullmatch=True))
    host_b = draw(st.from_regex(r"^host-b-[0-9]{1,3}$", fullmatch=True))
    host_c = draw(st.from_regex(r"^host-c-[0-9]{1,3}$", fullmatch=True))
    pid_a = str(draw(st.integers(min_value=100, max_value=499)))
    pid_b = str(draw(st.integers(min_value=500, max_value=899)))
    pid_c = str(draw(st.integers(min_value=900, max_value=1299)))

    use_str_timestamps = draw(st.booleans())
    use_enum_kinds = draw(st.booleans())

    def _fmt_ts(val: int) -> int | str:
        return str(val) if use_str_timestamps else val

    base_ns = draw(st.integers(min_value=1_000_000, max_value=10_000_000))
    root_id = "span_00_root"
    rpc_1_id = "span_01_rpc_b"
    rpc_2_id = "span_02_rpc_c"
    remote_leaf_id = "span_03_remote_leaf_c"
    producer_id = "span_04_producer"
    consumer_id = "span_05_consumer"
    fork_join_id = "span_06_fork_join"
    db_span_id = "span_07_db_query"
    ext_span_id = "span_08_ext_http"

    status_choices: list[tuple[Any, EventStatus]] = [
        ({"code": 1}, EventStatus.OK),
        ({"code": "STATUS_CODE_OK"}, EventStatus.OK),
        ({"code": 2}, EventStatus.ERROR),
        ({"code": "STATUS_CODE_ERROR"}, EventStatus.ERROR),
        ({"code": 0}, EventStatus.UNSET),
    ]
    root_status_raw, root_status = draw(st.sampled_from(status_choices))
    db_status_raw, db_status = draw(st.sampled_from(status_choices))

    res_a_spans = [
        {
            "traceId": trace_id,
            "spanId": root_id,
            "parentSpanId": "",
            "name": "POST /checkout",
            "kind": "SPAN_KIND_SERVER" if use_enum_kinds else 2,
            "startTimeUnixNano": _fmt_ts(base_ns),
            "endTimeUnixNano": _fmt_ts(base_ns + 50_000),
            "status": root_status_raw,
            "attributes": [_kv_attr("http.method", "POST")],
        },
        {
            "traceId": trace_id,
            "spanId": producer_id,
            "parentSpanId": root_id,
            "name": "publish order.created",
            "kind": "SPAN_KIND_PRODUCER" if use_enum_kinds else 4,
            "startTimeUnixNano": _fmt_ts(base_ns + 1_000),
            "endTimeUnixNano": _fmt_ts(base_ns + 5_000),
            "attributes": [
                _kv_attr("messaging.system", "kafka"),
                _kv_attr("messaging.destination.name", "orders"),
            ],
        },
        {
            "traceId": trace_id,
            "spanId": fork_join_id,
            "parentSpanId": root_id,
            "name": "parallel_enrich",
            "kind": "SPAN_KIND_INTERNAL" if use_enum_kinds else 1,
            "startTimeUnixNano": _fmt_ts(base_ns + 2_000),
            "endTimeUnixNano": _fmt_ts(base_ns + 12_000),
            "attributes": [_kv_attr("archigram.sync_kind", "fork_join_branch")],
        },
        {
            "traceId": trace_id,
            "spanId": ext_span_id,
            "parentSpanId": root_id,
            "name": "POST https://stripe.example/charge",
            "kind": "SPAN_KIND_CLIENT" if use_enum_kinds else 3,
            "startTimeUnixNano": _fmt_ts(base_ns + 3_000),
            "endTimeUnixNano": _fmt_ts(base_ns + 9_000),
            "attributes": [_kv_attr("peer.service", "stripe-gateway")],
        },
    ]

    res_b_spans = [
        {
            "traceId": trace_id,
            "spanId": rpc_1_id,
            "parentSpanId": root_id,
            "name": "Inventory.Reserve",
            "kind": "SPAN_KIND_SERVER" if use_enum_kinds else 2,
            "startTimeUnixNano": _fmt_ts(base_ns + 1_500),
            "endTimeUnixNano": _fmt_ts(base_ns + 15_000),
            "attributes": [_kv_attr("rpc.system", "grpc")],
        },
        {
            "traceId": trace_id,
            "spanId": db_span_id,
            "parentSpanId": rpc_1_id,
            "name": "SELECT stock FROM items",
            "kind": "SPAN_KIND_CLIENT" if use_enum_kinds else 3,
            "startTimeUnixNano": _fmt_ts(base_ns + 2_500),
            "endTimeUnixNano": _fmt_ts(base_ns + 8_500),
            "status": db_status_raw,
            "attributes": [
                _kv_attr("db.system", "postgresql"),
                _kv_attr("db.name", "inventory_db"),
            ],
        },
    ]

    # Host C has a heavily skewed wall clock (+10 seconds offset) to test clock domain isolation
    skew_ns = 10_000_000_000
    res_c_spans = [
        {
            "traceId": trace_id,
            "spanId": rpc_2_id,
            "parentSpanId": root_id,
            "name": "Pricing.Quote",
            "kind": "SPAN_KIND_SERVER" if use_enum_kinds else 2,
            "startTimeUnixNano": _fmt_ts(base_ns + 2_000),
            "endTimeUnixNano": _fmt_ts(base_ns + 14_000),
            "attributes": [_kv_attr("rpc.system", "grpc")],
        },
        {
            "traceId": trace_id,
            "spanId": remote_leaf_id,
            "parentSpanId": rpc_2_id,
            "name": "Pricing.ComputeTax",
            "kind": "SPAN_KIND_INTERNAL" if use_enum_kinds else 1,
            "startTimeUnixNano": _fmt_ts(base_ns + skew_ns),
            "endTimeUnixNano": _fmt_ts(base_ns + skew_ns + 4_000),
            "attributes": [],
        },
        {
            "traceId": trace_id,
            "spanId": consumer_id,
            "parentSpanId": "missing_external_parent_999",
            "name": "consume order.created",
            "kind": "SPAN_KIND_CONSUMER" if use_enum_kinds else 5,
            "startTimeUnixNano": _fmt_ts(base_ns + 20_000),
            "endTimeUnixNano": _fmt_ts(base_ns + 35_000),
            "attributes": [
                _kv_attr("messaging.system", "kafka"),
                _kv_attr("messaging.destination.name", "orders"),
            ],
            "links": [
                {"traceId": trace_id, "spanId": producer_id},
                {"traceId": trace_id, "spanId": rpc_1_id},
                {"traceId": trace_id, "spanId": consumer_id},  # self-link must be filtered
                {"traceId": trace_id, "spanId": "unknown_external_span"},  # missing link filtered
            ],
        },
    ]

    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _kv_attr("service.name", "checkout-api"),
                        _kv_attr("service.namespace", "shop"),
                        _kv_attr("host.id", host_a),
                        _kv_attr("process.pid", pid_a),
                        _kv_attr("service.instance.id", "inst-a"),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "otel.http"}, "spans": res_a_spans}],
            },
            {
                "resource": {
                    "attributes": [
                        _kv_attr("service.name", "inventory-svc"),
                        _kv_attr("service.namespace", "shop"),
                        _kv_attr("host.id", host_b),
                        _kv_attr("process.pid", pid_b),
                        _kv_attr("service.instance.id", "inst-b"),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "otel.grpc"}, "spans": res_b_spans}],
            },
            {
                "resource": {
                    "attributes": [
                        _kv_attr("service.name", "pricing-svc"),
                        _kv_attr("service.namespace", "shop"),
                        _kv_attr("host.id", host_c),
                        _kv_attr("process.pid", pid_c),
                        _kv_attr("service.instance.id", "inst-c"),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "otel.worker"}, "spans": res_c_spans}],
            },
        ]
    }

    return _GeneratedOtelTrace(
        payload=payload,
        trace_id=trace_id,
        root_span_id=root_id,
        root_clock_domain=f"clk:shop.checkout-api@{host_a}:{pid_a}:inst-a",
        rpc_child_ids=(rpc_1_id, rpc_2_id),
        remote_leaf_id=remote_leaf_id,
        remote_host_clock_domain=f"clk:shop.pricing-svc@{host_c}:{pid_c}:inst-c",
        producer_span_id=producer_id,
        consumer_span_id=consumer_id,
        fork_join_span_id=fork_join_id,
        db_span_id=db_span_id,
        ext_span_id=ext_span_id,
        expected_status_map={
            root_id: root_status,
            db_span_id: db_status,
            producer_id: EventStatus.UNSET,
        },
    )


def _assert_participant_kinds_and_provenance(
    ir: Any,
    expected_sha: str,
) -> None:
    assert ir.participants["shop.checkout-api"].kind is NodeKind.SERVICE
    assert ir.participants["shop.inventory-svc"].kind is NodeKind.SERVICE
    assert ir.participants["shop.pricing-svc"].kind is NodeKind.SERVICE
    assert ir.participants["db.postgresql.inventory_db"].kind is NodeKind.DATABASE
    assert ir.participants["queue.kafka.orders"].kind is NodeKind.QUEUE
    assert ir.participants["ext.stripe-gateway"].kind is NodeKind.EXTERNAL

    for participant in ir.participants.values():
        assert participant.provenance.grade is ProvenanceGrade.OBSERVED
        assert participant.provenance.extractor == "otlp-json"
        assert participant.provenance.input_sha256 == expected_sha


@settings(max_examples=35, deadline=None)
@given(fixture=otlp_trace_fixture_strategy())
def test_ingest_otel_causal_dag_clock_isolation_and_sync_semantics(
    fixture: _GeneratedOtelTrace,
) -> None:
    ingestor = OtelTraceIngestor()
    assert isinstance(ingestor, TraceIngestorPort)
    assert ingestor.source_format == "otlp_json"
    assert ingestor.extractor_name == "otlp-json"

    raw_json = json.dumps(fixture.payload)
    expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    ir = ingestor.ingest(raw_json)

    assert ir.trace_id == fixture.trace_id
    assert ir.metadata["source_format"] == "otlp_json"
    _assert_participant_kinds_and_provenance(ir, expected_sha)

    root_ev = ir.events[fixture.root_span_id]
    assert root_ev.clock_domain_id == fixture.root_clock_domain
    assert root_ev.parent_event_id is None

    for rpc_id in fixture.rpc_child_ids:
        child_ev = ir.events[rpc_id]
        assert child_ev.parent_event_id == fixture.root_span_id
        assert child_ev.clock_domain_id == fixture.root_clock_domain
        assert child_ev.caller_id == "shop.checkout-api"
        assert child_ev.sync_kind is SyncKind.SYNC

    leaf_ev = ir.events[fixture.remote_leaf_id]
    assert leaf_ev.clock_domain_id == fixture.remote_host_clock_domain
    assert leaf_ev.clock_domain_id != fixture.root_clock_domain

    prod_ev = ir.events[fixture.producer_span_id]
    assert prod_ev.sync_kind is SyncKind.ASYNC_FIRE_FORGET
    assert prod_ev.caller_id == "shop.checkout-api"
    assert prod_ev.callee_id == "queue.kafka.orders"

    cons_ev = ir.events[fixture.consumer_span_id]
    assert cons_ev.sync_kind is SyncKind.ASYNC_FIRE_FORGET
    assert cons_ev.parent_event_id is None
    assert cons_ev.caller_id == "queue.kafka.orders"
    assert cons_ev.callee_id == "shop.pricing-svc"
    assert set(cons_ev.causal_links) == {fixture.producer_span_id, fixture.rpc_child_ids[0]}

    assert ir.events[fixture.fork_join_span_id].sync_kind is SyncKind.FORK_JOIN_BRANCH
    assert ir.events[fixture.db_span_id].callee_id == "db.postgresql.inventory_db"
    assert ir.events[fixture.ext_span_id].callee_id == "ext.stripe-gateway"

    for span_id, exp_status in fixture.expected_status_map.items():
        assert ir.events[span_id].status is exp_status


@settings(max_examples=25, deadline=None)
@given(
    fixture=otlp_trace_fixture_strategy(),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_ingest_otel_permutation_invariance_and_bytes_file_equivalence(
    fixture: _GeneratedOtelTrace,
    seed: int,
) -> None:
    """Permuting resourceSpans and spans order produces identical events & participants."""
    ingestor = OtelTraceIngestor()
    canonical_json = json.dumps(fixture.payload)
    ir_baseline = ingestor.ingest(canonical_json)

    # Permute resourceSpans and inner spans deterministically using seed
    permuted_payload = json.loads(canonical_json)
    res_list = permuted_payload["resourceSpans"]
    if seed % 2 == 1:
        res_list.reverse()
    for res_entry in res_list:
        for scope_entry in res_entry["scopeSpans"]:
            spans = scope_entry["spans"]
            shift = seed % max(1, len(spans))
            scope_entry["spans"] = spans[shift:] + spans[:shift]

    ir_permuted = ingestor.ingest(json.dumps(permuted_payload).encode("utf-8"))

    assert list(ir_baseline.participants.keys()) == list(ir_permuted.participants.keys())
    assert list(ir_baseline.events.keys()) == list(ir_permuted.events.keys())
    for eid, ev in ir_baseline.events.items():
        other = ir_permuted.events[eid]
        assert ev.caller_id == other.caller_id
        assert ev.callee_id == other.callee_id
        assert ev.parent_event_id == other.parent_event_id
        assert ev.causal_links == other.causal_links
        assert ev.clock_domain_id == other.clock_domain_id
        assert ev.sync_kind is other.sync_kind
        assert ev.start_ns == other.start_ns
        assert ev.end_ns == other.end_ns

    with tempfile.TemporaryDirectory() as tmp_dir:
        trace_file = Path(tmp_dir) / "trace.json"
        trace_file.write_text(canonical_json, encoding="utf-8")
        ir_from_path = ingestor.ingest(trace_file)
        assert ir_from_path.model_dump(exclude={"participants", "events"}) == (
            ir_baseline.model_dump(exclude={"participants", "events"})
        )
        for ev in ir_from_path.events.values():
            assert ev.provenance.file_path == str(trace_file)


@settings(max_examples=25, deadline=None)
@given(
    start_ns=st.integers(min_value=1_000, max_value=500_000),
    duration_ns=st.integers(min_value=500, max_value=20_000),
)
def test_ingest_otel_prevents_false_concurrency_across_skewed_remote_root_clocks(
    start_ns: int,
    duration_ns: int,
) -> None:
    """Two root spans on different hosts with identical wall-clock intervals never cluster in R5."""
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _kv_attr("service.name", "edge-proxy"),
                        _kv_attr("host.id", "node-west-1"),
                        _kv_attr("process.pid", "101"),
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aaaa1111bbbb2222",
                                "spanId": "root_west",
                                "name": "HandleWest",
                                "kind": 2,
                                "startTimeUnixNano": start_ns,
                                "endTimeUnixNano": start_ns + duration_ns,
                            },
                            {
                                "traceId": "aaaa1111bbbb2222",
                                "spanId": "cyclic_a",
                                "parentSpanId": "cyclic_b",
                                "name": "CycleA",
                                "kind": 1,
                                "startTimeUnixNano": start_ns,
                                "endTimeUnixNano": start_ns + duration_ns,
                                "links": [{"spanId": "cyclic_b"}],
                            },
                        ]
                    }
                ],
            },
            {
                "resource": {
                    "attributes": [
                        _kv_attr("service.name", "edge-proxy"),
                        _kv_attr("host.id", "node-east-2"),
                        _kv_attr("process.pid", "202"),
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aaaa1111bbbb2222",
                                "spanId": "root_east",
                                "name": "HandleEast",
                                "kind": 2,
                                "startTimeUnixNano": start_ns,
                                "endTimeUnixNano": start_ns + duration_ns,
                            },
                            {
                                "traceId": "aaaa1111bbbb2222",
                                "spanId": "cyclic_b",
                                "parentSpanId": "cyclic_a",
                                "name": "CycleB",
                                "kind": 1,
                                "startTimeUnixNano": start_ns,
                                "endTimeUnixNano": start_ns + duration_ns,
                                "links": [{"spanId": "cyclic_a"}],
                            },
                        ]
                    }
                ],
            },
        ]
    }

    ir = OtelTraceIngestor().ingest(json.dumps(payload))
    assert ir.events["root_west"].clock_domain_id != ir.events["root_east"].clock_domain_id

    root_intervals = [
        SpanInterval(
            event_id=ev.event_id,
            parent_event_id=ev.parent_event_id,
            clock_domain_id=ev.clock_domain_id,
            start_ns=ev.start_ns,
            end_ns=ev.end_ns,
        )
        for eid, ev in ir.events.items()
        if eid in {"root_west", "root_east"}
    ]
    assert cluster_overlapping_siblings(root_intervals) == ()
