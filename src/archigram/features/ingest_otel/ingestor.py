"""OpenTelemetry (OTLP) JSON causal and concurrent trace ingestor into InteractionTraceIR."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EventStatus,
    InteractionTraceIR,
    NodeKind,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.ports.contracts import TraceIngestorPort

_PRODUCER_KINDS = frozenset({"4", "span_kind_producer", "producer"})
_CONSUMER_KINDS = frozenset({"5", "span_kind_consumer", "consumer"})
_ASYNC_KINDS = _PRODUCER_KINDS | _CONSUMER_KINDS
_CLIENT_KINDS = frozenset({"3", "span_kind_client", "client"})

_STATUS_OK_CODES = frozenset({"1", "status_code_ok", "ok"})
_STATUS_ERR_CODES = frozenset({"2", "status_code_error", "error"})

_ASYNC_SYNC_VALUES = frozenset({"async", "async_fire_forget", "fire_and_forget", "true", "1"})
_FORK_JOIN_VALUES = frozenset({"fork_join", "fork_join_branch", "parallel", "true", "1"})


def _read_trace_source(
    source: Path | str | bytes,
    source_name: str,
) -> tuple[str, str, str | None]:
    """Resolve Path, JSON string/path, or bytes into (json_text, sha256_hex, file_path)."""
    default_file = None if source_name in {"", "<memory>"} else source_name
    if isinstance(source, bytes):
        return (
            source.decode("utf-8"),
            hashlib.sha256(source).hexdigest(),
            default_file,
        )
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        return (
            raw_bytes.decode("utf-8"),
            hashlib.sha256(raw_bytes).hexdigest(),
            str(source),
        )

    stripped = source.lstrip()
    if not stripped.startswith(("{", "[")) and "\n" not in source and len(source) <= 1024:
        candidate = Path(source)
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if is_file:
            raw_bytes = candidate.read_bytes()
            return (
                raw_bytes.decode("utf-8"),
                hashlib.sha256(raw_bytes).hexdigest(),
                str(candidate),
            )

    raw_bytes = source.encode("utf-8")
    return source, hashlib.sha256(raw_bytes).hexdigest(), default_file


def _extract_otlp_any_value(val_obj: Any) -> str:
    """Unwrap an OTLP AnyValue dictionary or primitive into a flat string."""
    if not isinstance(val_obj, dict):
        return str(val_obj).strip()
    for key in ("stringValue", "string_value", "intValue", "int_value", "doubleValue", "boolValue"):
        if key in val_obj and val_obj[key] is not None:
            val = val_obj[key]
            if isinstance(val, bool):
                return "true" if val else "false"
            return str(val).strip()
    if "arrayValue" in val_obj and isinstance(val_obj["arrayValue"], dict):
        values = val_obj["arrayValue"].get("values", [])
        if isinstance(values, list):
            return ",".join(_extract_otlp_any_value(item) for item in values)
    return ""


def _parse_otlp_attributes(raw_attrs: Any) -> dict[str, str]:
    """Normalize OTLP key-value array or dict into a sorted string dictionary."""
    if not raw_attrs:
        return {}
    result: dict[str, str] = {}
    if isinstance(raw_attrs, dict):
        for k, v in raw_attrs.items():
            key = str(k).strip()
            if key and v is not None:
                result[key] = _extract_otlp_any_value(v)
    elif isinstance(raw_attrs, list):
        for item in raw_attrs:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            val_raw = item.get("value", item.get("val", ""))
            result[key] = _extract_otlp_any_value(val_raw)
    return dict(sorted(result.items()))


def _compute_resource_fingerprint(res_attrs: dict[str, str], service_id: str) -> str:
    """Compute isolated host/process clock domain ID for a resource."""
    host = (
        res_attrs.get("host.id")
        or res_attrs.get("host.name")
        or res_attrs.get("k8s.pod.uid")
        or res_attrs.get("container.id")
        or "local-host"
    )
    pid = res_attrs.get("process.pid") or res_attrs.get("process.runtime.id") or "0"
    instance = (
        res_attrs.get("service.instance.id") or res_attrs.get("telemetry.sdk.instance") or "default"
    )
    return f"clk:{service_id}@{host}:{pid}:{instance}"


def _resolve_resource_identity(
    res_attrs: dict[str, str],
    fallback_idx: int,
) -> tuple[str, str, NodeKind]:
    """Determine (participant_id, label, NodeKind) for an OTLP Resource."""
    svc_name = res_attrs.get("service.name", "").strip()
    svc_ns = res_attrs.get("service.namespace", "").strip()
    db_sys = res_attrs.get("db.system", "").strip()
    msg_sys = res_attrs.get("messaging.system", "").strip()
    peer_svc = res_attrs.get("peer.service", "").strip()

    if svc_name:
        pid = (
            f"{svc_ns}.{svc_name}" if svc_ns and not svc_name.startswith(f"{svc_ns}.") else svc_name
        )
        label = svc_name
    elif db_sys:
        db_name = res_attrs.get("db.name") or res_attrs.get("db.namespace") or db_sys
        pid = f"db.{db_sys}.{db_name}" if db_name != db_sys else f"db.{db_sys}"
        label = db_name
    elif msg_sys:
        dest = res_attrs.get("messaging.destination.name") or msg_sys
        pid = f"queue.{msg_sys}.{dest}" if dest != msg_sys else f"queue.{msg_sys}"
        label = dest
    elif peer_svc:
        pid = f"ext.{peer_svc}"
        label = peer_svc
    else:
        host = res_attrs.get("host.id") or res_attrs.get("host.name") or f"resource_{fallback_idx}"
        pid = f"service.{host}"
        label = host

    if db_sys:
        kind = NodeKind.DATABASE
    elif msg_sys:
        kind = NodeKind.QUEUE
    elif not svc_name and peer_svc:
        kind = NodeKind.EXTERNAL
    else:
        kind = NodeKind.SERVICE
    return pid, label, kind


def _parse_nano_timestamp(raw_val: Any, field_name: str) -> int:
    """Parse OTLP startTimeUnixNano / endTimeUnixNano (int or numeric string) into >= 0 int."""
    if raw_val is None or raw_val == "":
        return 0
    if isinstance(raw_val, bool):
        msg = f"Invalid boolean timestamp for {field_name}"
        raise ValueError(msg)
    try:
        ns = int(raw_val)
    except (ValueError, TypeError) as exc:
        msg = f"Invalid OTLP nanosecond timestamp for {field_name}: {raw_val!r}"
        raise ValueError(msg) from exc
    if ns < 0:
        msg = f"Negative OTLP nanosecond timestamp for {field_name}: {ns}"
        raise ValueError(msg)
    return ns


def _normalize_kind_token(raw_kind: Any) -> str:
    """Normalize OTLP SpanKind int or enum name to lowercase token."""
    if raw_kind is None:
        return "unspecified"
    return str(raw_kind).strip().lower()


def _resolve_sync_kind(kind_token: str, span_attrs: dict[str, str]) -> SyncKind:
    """Classify span execution synchrony into SYNC, ASYNC_FIRE_FORGET, or FORK_JOIN_BRANCH."""
    explicit_sync = (
        (
            span_attrs.get("archigram.sync_kind")
            or span_attrs.get("sync_kind")
            or span_attrs.get("concurrency.mode")
            or ""
        )
        .strip()
        .lower()
    )
    if (
        explicit_sync in _FORK_JOIN_VALUES
        or span_attrs.get("fork_join", "").strip().lower() in _FORK_JOIN_VALUES
    ):
        return SyncKind.FORK_JOIN_BRANCH
    if (
        explicit_sync in _ASYNC_SYNC_VALUES
        or span_attrs.get("async", "").strip().lower() in _ASYNC_SYNC_VALUES
    ):
        return SyncKind.ASYNC_FIRE_FORGET
    if kind_token in _ASYNC_KINDS or "messaging.system" in span_attrs:
        return SyncKind.ASYNC_FIRE_FORGET
    return SyncKind.SYNC


def _resolve_event_status(raw_status: Any, span_attrs: dict[str, str]) -> EventStatus:
    """Map OTLP status object or status.code attribute into canonical EventStatus."""
    code_raw: Any = None
    if isinstance(raw_status, dict):
        code_raw = raw_status.get("code", raw_status.get("statusCode"))
    elif raw_status is not None:
        code_raw = raw_status
    if code_raw is None:
        code_raw = span_attrs.get("otel.status_code")
    token = str(code_raw or "").strip().lower()
    if token in _STATUS_OK_CODES:
        return EventStatus.OK
    if token in _STATUS_ERR_CODES:
        return EventStatus.ERROR
    return EventStatus.UNSET


@dataclass(frozen=True, slots=True)
class _RawSpanRecord:
    """Intermediate normalized OTLP span record prior to cross-span resolution."""

    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    kind_token: str
    start_ns: int
    end_ns: int
    status: EventStatus
    span_attrs: dict[str, str]
    link_span_ids: tuple[str, ...]
    resource_participant_id: str
    resource_label: str
    resource_kind: NodeKind
    resource_clock_domain_id: str
    scope_name: str


def _extract_span_links(raw_links: Any, self_span_id: str) -> tuple[str, ...]:
    """Extract unique non-self linked spanIds in deterministic order."""
    if not isinstance(raw_links, list):
        return ()
    seen: set[str] = set()
    links: list[str] = []
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        target = str(item.get("spanId") or item.get("span_id") or "").strip()
        if target and target != self_span_id and target not in seen:
            seen.add(target)
            links.append(target)
    return tuple(links)


def _parse_single_span(
    span_obj: dict[str, Any],
    res_pid: str,
    res_label: str,
    res_kind: NodeKind,
    res_clock: str,
    scope_name: str,
) -> _RawSpanRecord | None:
    """Parse one OTLP span dictionary into _RawSpanRecord."""
    span_id = str(span_obj.get("spanId") or span_obj.get("span_id") or "").strip()
    if not span_id:
        return None
    trace_id = str(span_obj.get("traceId") or span_obj.get("trace_id") or "").strip()
    raw_parent = str(span_obj.get("parentSpanId") or span_obj.get("parent_span_id") or "").strip()
    if not raw_parent or raw_parent == span_id or set(raw_parent) == {"0"}:
        parent_span_id = None
    else:
        parent_span_id = raw_parent

    name = str(span_obj.get("name") or "").strip() or span_id
    kind_token = _normalize_kind_token(span_obj.get("kind"))
    start_ns = _parse_nano_timestamp(
        span_obj.get("startTimeUnixNano", span_obj.get("start_time_unix_nano", 0)),
        "startTimeUnixNano",
    )
    end_ns = _parse_nano_timestamp(
        span_obj.get("endTimeUnixNano", span_obj.get("end_time_unix_nano", start_ns)),
        "endTimeUnixNano",
    )
    if end_ns < start_ns:
        msg = (
            f"OTLP span '{span_id}' has endTimeUnixNano ({end_ns}) < startTimeUnixNano ({start_ns})"
        )
        raise ValueError(msg)

    span_attrs = _parse_otlp_attributes(span_obj.get("attributes"))
    if scope_name:
        span_attrs.setdefault("otel.scope.name", scope_name)
    status = _resolve_event_status(span_obj.get("status"), span_attrs)
    links = _extract_span_links(span_obj.get("links"), span_id)

    return _RawSpanRecord(
        span_id=span_id,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        name=name,
        kind_token=kind_token,
        start_ns=start_ns,
        end_ns=end_ns,
        status=status,
        span_attrs=span_attrs,
        link_span_ids=links,
        resource_participant_id=res_pid,
        resource_label=res_label,
        resource_kind=res_kind,
        resource_clock_domain_id=res_clock,
        scope_name=scope_name,
    )


def _collect_resource_entry_spans(
    res_entry: dict[str, Any],
    res_idx: int,
) -> list[_RawSpanRecord]:
    """Extract all valid spans from a single OTLP ResourceSpans object."""
    res_obj = res_entry.get("resource")
    res_attrs = _parse_otlp_attributes(
        res_obj.get("attributes") if isinstance(res_obj, dict) else None
    )
    res_pid, res_label, res_kind = _resolve_resource_identity(res_attrs, res_idx)
    res_clock = _compute_resource_fingerprint(res_attrs, res_pid)

    scope_list = (
        res_entry.get("scopeSpans")
        or res_entry.get("scope_spans")
        or res_entry.get("instrumentationLibrarySpans")
        or []
    )
    if not isinstance(scope_list, list):
        return []

    records: list[_RawSpanRecord] = []
    for scope_entry in scope_list:
        if not isinstance(scope_entry, dict):
            continue
        scope_obj = scope_entry.get("scope") or scope_entry.get("instrumentationLibrary")
        scope_name = str(scope_obj.get("name", "")).strip() if isinstance(scope_obj, dict) else ""
        spans_list = scope_entry.get("spans", [])
        if not isinstance(spans_list, list):
            continue
        for span_obj in spans_list:
            if isinstance(span_obj, dict):
                rec = _parse_single_span(
                    span_obj, res_pid, res_label, res_kind, res_clock, scope_name
                )
                if rec is not None:
                    records.append(rec)
    return records


def _collect_raw_spans(payload: dict[str, Any]) -> list[_RawSpanRecord]:
    """Traverse ExportTraceServiceRequest resourceSpans -> scopeSpans -> spans."""
    res_spans = payload.get("resourceSpans")
    if res_spans is None:
        res_spans = payload.get("resource_spans", [])
    if not isinstance(res_spans, list):
        msg = "OTLP payload 'resourceSpans' must be a JSON array"
        raise ValueError(msg)

    records: list[_RawSpanRecord] = []
    for res_idx, res_entry in enumerate(res_spans):
        if isinstance(res_entry, dict):
            records.extend(_collect_resource_entry_spans(res_entry, res_idx))
    return records


def _infer_synthetic_target(
    rec: _RawSpanRecord,
) -> tuple[str, str, NodeKind] | None:
    """Detect virtual database, messaging queue, or external peer target from span attributes."""
    attrs = rec.span_attrs
    db_sys = attrs.get("db.system", "").strip()
    if db_sys:
        db_name = attrs.get("db.name") or attrs.get("db.namespace") or db_sys
        pid = f"db.{db_sys}.{db_name}" if db_name != db_sys else f"db.{db_sys}"
        return pid, db_name, NodeKind.DATABASE

    msg_sys = attrs.get("messaging.system", "").strip()
    msg_dest = (
        attrs.get("messaging.destination.name") or attrs.get("messaging.destination") or ""
    ).strip()
    if msg_sys or msg_dest or rec.kind_token in _PRODUCER_KINDS:
        if rec.kind_token in _CONSUMER_KINDS and not (msg_sys or msg_dest):
            return None
        broker = msg_sys or "broker"
        dest = msg_dest or broker
        pid = f"queue.{broker}.{dest}" if dest != broker else f"queue.{broker}"
        return pid, dest, NodeKind.QUEUE

    peer = (
        attrs.get("peer.service") or attrs.get("net.peer.name") or attrs.get("server.address") or ""
    ).strip()
    if peer and rec.kind_token in _CLIENT_KINDS:
        return f"ext.{peer}", peer, NodeKind.EXTERNAL
    return None


def _is_reachable(start: str, target: str, adj: dict[str, set[str]]) -> bool:
    """Return True if target is reachable from start in directed graph adj."""
    if start == target:
        return True
    visited: set[str] = {start}
    queue: deque[str] = deque([start])
    while queue:
        curr = queue.popleft()
        for nxt in adj.get(curr, ()):
            if nxt == target:
                return True
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return False


def _sanitize_causal_dag(
    by_id: dict[str, _RawSpanRecord],
) -> tuple[dict[str, str | None], dict[str, tuple[str, ...]]]:
    """Filter missing references and deterministically break any parent or link cycles."""
    adj: dict[str, set[str]] = {sid: set() for sid in by_id}
    clean_parents: dict[str, str | None] = {}

    for sid in sorted(by_id):
        parent = by_id[sid].parent_span_id
        if parent is not None and parent in by_id and not _is_reachable(sid, parent, adj):
            clean_parents[sid] = parent
            adj[parent].add(sid)
        else:
            clean_parents[sid] = None

    clean_links: dict[str, tuple[str, ...]] = {}
    for sid in sorted(by_id):
        accepted: list[str] = []
        for link_id in sorted(by_id[sid].link_span_ids):
            if link_id in by_id and link_id != sid and not _is_reachable(sid, link_id, adj):
                adj[link_id].add(sid)
                accepted.append(link_id)
        clean_links[sid] = tuple(accepted)

    return clean_parents, clean_links


def _resolve_caller_and_callee(
    rec: _RawSpanRecord,
    parent_rec: _RawSpanRecord | None,
    participants: dict[str, TraceParticipant],
    stamp: ProvenanceStamp,
) -> tuple[str, str, str]:
    """Determine (caller_id, callee_id, clock_domain_id) enforcing caller clock isolation."""
    clock_domain_id = (
        parent_rec.resource_clock_domain_id
        if parent_rec is not None
        else rec.resource_clock_domain_id
    )
    synthetic = _infer_synthetic_target(rec)
    if synthetic is not None:
        target_pid, target_label, target_kind = synthetic
        if target_pid not in participants:
            participants[target_pid] = TraceParticipant(
                participant_id=target_pid,
                symbol_id=target_pid,
                label=target_label,
                kind=target_kind,
                provenance=stamp,
            )
        if rec.kind_token in _CONSUMER_KINDS:
            return target_pid, rec.resource_participant_id, clock_domain_id
        return rec.resource_participant_id, target_pid, clock_domain_id

    caller_id = (
        parent_rec.resource_participant_id
        if parent_rec is not None
        else rec.resource_participant_id
    )
    return caller_id, rec.resource_participant_id, clock_domain_id


def _build_interaction_trace(
    records: list[_RawSpanRecord],
    stamp: ProvenanceStamp,
) -> InteractionTraceIR:
    """Assemble validated InteractionTraceIR from normalized span records."""
    by_id: dict[str, _RawSpanRecord] = {}
    for rec in sorted(records, key=lambda r: (r.start_ns, r.end_ns, r.span_id)):
        by_id.setdefault(rec.span_id, rec)

    participants: dict[str, TraceParticipant] = {}
    for sid in sorted(by_id):
        rec = by_id[sid]
        pid = rec.resource_participant_id
        if pid not in participants:
            participants[pid] = TraceParticipant(
                participant_id=pid,
                symbol_id=pid,
                label=rec.resource_label,
                kind=rec.resource_kind,
                provenance=stamp,
            )

    clean_parents, clean_links = _sanitize_causal_dag(by_id)
    events: dict[str, TraceEvent] = {}
    trace_ids: list[str] = []

    for sid in sorted(by_id):
        rec = by_id[sid]
        if rec.trace_id:
            trace_ids.append(rec.trace_id)
        parent_id = clean_parents[sid]
        parent_rec = by_id.get(parent_id) if parent_id is not None else None
        caller_id, callee_id, clock_domain_id = _resolve_caller_and_callee(
            rec, parent_rec, participants, stamp
        )
        events[sid] = TraceEvent(
            event_id=sid,
            caller_id=caller_id,
            callee_id=callee_id,
            signature=rec.name,
            parent_event_id=parent_id,
            causal_links=clean_links[sid],
            clock_domain_id=clock_domain_id,
            start_ns=rec.start_ns,
            end_ns=rec.end_ns,
            sync_kind=_resolve_sync_kind(rec.kind_token, rec.span_attrs),
            status=rec.status,
            attributes=rec.span_attrs,
            provenance=stamp,
        )

    canonical_trace_id = min(trace_ids) if trace_ids else "default"
    sorted_participants = {k: participants[k] for k in sorted(participants)}
    return InteractionTraceIR(
        trace_id=canonical_trace_id,
        participants=sorted_participants,
        events=events,
        metadata={"source_format": "otlp_json"},
    )


class OtelTraceIngestor(TraceIngestorPort):
    """Ingest OpenTelemetry (OTLP) JSON trace payloads into Canonical InteractionTraceIR."""

    source_format: str = "otlp_json"

    @property
    def extractor_name(self) -> str:
        """Canonical extractor identifier recorded in OBSERVED provenance stamps."""
        return "otlp-json"

    def ingest(
        self,
        source: Path | str | bytes | None = None,
        *,
        raw_payload: Path | str | bytes | None = None,
        source_name: str = "<memory>",
    ) -> InteractionTraceIR:
        """Parse an OTLP ExportTraceServiceRequest JSON payload into InteractionTraceIR."""
        effective_source = raw_payload if raw_payload is not None else source
        if effective_source is None:
            msg = "Cannot ingest missing OpenTelemetry OTLP JSON payload"
            raise ValueError(msg)
        json_text, input_sha256, file_path = _read_trace_source(effective_source, source_name)
        if not json_text.strip():
            msg = "Cannot ingest empty OpenTelemetry OTLP JSON payload"
            raise ValueError(msg)

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as exc:
            msg = f"Invalid OpenTelemetry OTLP JSON payload: {exc}"
            raise ValueError(msg) from exc

        if not isinstance(parsed, dict):
            msg = "OpenTelemetry OTLP JSON root must be an object"
            raise ValueError(msg)

        records = _collect_raw_spans(parsed)
        stamp = ProvenanceStamp(
            grade=ProvenanceGrade.OBSERVED,
            extractor=self.extractor_name,
            input_sha256=input_sha256,
            file_path=file_path,
        )
        return _build_interaction_trace(records, stamp)
