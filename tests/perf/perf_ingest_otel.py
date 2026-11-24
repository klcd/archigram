"""Performance benchmark suite for OpenTelemetry (OTLP) JSON trace ingestion (1,000 spans)."""

from __future__ import annotations

import json
from typing import Any

from archigram.features.ingest_otel import OtelTraceIngestor


def _build_1000_span_otlp_payload(
    service_count: int = 10,
    spans_per_service: int = 100,
) -> str:
    """Build a 1,000-span concurrent multi-service OTLP JSON trace fixture."""
    trace_id = "0123456789abcdef0123456789abcdef"
    resource_spans: list[dict[str, Any]] = []

    for svc_idx in range(service_count):
        svc_name = f"service-{svc_idx:02d}"
        host_id = f"host-{svc_idx:02d}.prod.internal"
        pid = str(1000 + svc_idx)
        spans: list[dict[str, Any]] = []

        for s_idx in range(spans_per_service):
            global_idx = svc_idx * spans_per_service + s_idx
            span_id = f"span_{global_idx:04d}"
            if global_idx == 0:
                parent_id = ""
            elif s_idx == 0:
                parent_id = f"span_{(svc_idx - 1) * spans_per_service:04d}"
            else:
                parent_id = f"span_{svc_idx * spans_per_service:04d}"

            start_ns = 1_700_000_000_000_000_000 + (s_idx // 8) * 100_000 + (s_idx % 8) * 5_000
            end_ns = start_ns + 50_000
            is_async = s_idx % 10 == 9
            kind = 4 if is_async else (2 if s_idx == 0 else 3)

            attrs = [
                {"key": "rpc.system", "value": {"stringValue": "grpc"}},
                {"key": "worker.slot", "value": {"intValue": s_idx % 8}},
            ]
            if is_async:
                attrs.append({"key": "messaging.system", "value": {"stringValue": "kafka"}})
                attrs.append(
                    {
                        "key": "messaging.destination.name",
                        "value": {"stringValue": f"events-{svc_idx:02d}"},
                    }
                )

            links: list[dict[str, str]] = []
            if global_idx >= 2 and s_idx % 5 == 0:
                links.append({"traceId": trace_id, "spanId": f"span_{global_idx - 2:04d}"})

            spans.append(
                {
                    "traceId": trace_id,
                    "spanId": span_id,
                    "parentSpanId": parent_id,
                    "name": f"{svc_name}.op_{s_idx % 5}",
                    "kind": kind,
                    "startTimeUnixNano": str(start_ns),
                    "endTimeUnixNano": str(end_ns),
                    "status": {"code": 1},
                    "attributes": attrs,
                    "links": links,
                }
            )

        resource_spans.append(
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": svc_name}},
                        {"key": "service.namespace", "value": {"stringValue": "bench"}},
                        {"key": "host.id", "value": {"stringValue": host_id}},
                        {"key": "process.pid", "value": {"stringValue": pid}},
                        {
                            "key": "service.instance.id",
                            "value": {"stringValue": f"inst-{svc_idx:02d}"},
                        },
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "archigram.bench.tracer"},
                        "spans": spans,
                    }
                ],
            }
        )

    return json.dumps({"resourceSpans": resource_spans})


_OTLP_1000_FIXTURE = _build_1000_span_otlp_payload(10, 100)
_INGESTOR = OtelTraceIngestor()


def test_ingest_1000_span_otlp_trace_benchmark(benchmark: Any) -> None:
    ir = benchmark(_INGESTOR.ingest, _OTLP_1000_FIXTURE)
    assert len(ir.events) == 1000
    # 10 service participants + 10 synthetic kafka queue participants = 20
    assert len(ir.participants) == 20
