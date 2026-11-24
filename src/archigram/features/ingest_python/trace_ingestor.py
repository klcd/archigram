"""Native Python runtime call-trace capture (sys.monitoring / setprofile) and JSON ingestor."""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType, FrameType

_MONITOR_EXTRACTOR = "python_sys_monitoring_v1"
_JSON_EXTRACTOR = "python_trace_v1"
_DEFAULT_CLOCK_DOMAIN = "local_process"
_CANDIDATE_TOOL_IDS = (4, 3, 2, 1, 0, 5)
_NODE_KIND_BY_NAME: dict[str, NodeKind] = {kind.value: kind for kind in NodeKind}
_STATUS_BY_NAME: dict[str, EventStatus] = {status.value: status for status in EventStatus}
_SYNC_BY_NAME: dict[str, SyncKind] = {sync.value: sync for sync in SyncKind}


@dataclass(frozen=True, slots=True)
class _CodeMeta:
    """Cached resolved participant and source metadata for a Python CodeType."""

    module_name: str
    qualname: str
    participant_id: str
    label: str
    kind: NodeKind
    file_path: str
    line_span: tuple[int, int]


@dataclass(slots=True)
class _ActiveFrame:
    """Stack frame tracking live call nesting and the active recorded parent event."""

    code: CodeType
    participant_id: str
    event_id: str | None
    recorded_parent_id: str | None
    recorded_depth: int


@dataclass(slots=True)
class _RecordedEvent:
    """Mutable live event record finalized when the corresponding frame returns or unwinds."""

    event_id: str
    caller_id: str
    callee_id: str
    signature: str
    parent_event_id: str | None
    start_ns: int
    end_ns: int
    call_depth: int
    module_name: str
    qualname: str
    file_path: str
    line_span: tuple[int, int]
    status: EventStatus = EventStatus.OK


@dataclass(slots=True)
class _ParticipantMeta:
    """Participant metadata accumulated during a live trace capture."""

    participant_id: str
    label: str
    kind: NodeKind
    file_path: str
    line_span: tuple[int, int]


def _normalize_file_path(raw_filename: str, cwd: Path) -> str:
    """Convert an absolute Python co_filename to a clean POSIX relative path when possible."""
    path = Path(raw_filename)
    try:
        return path.resolve().relative_to(cwd).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _code_line_span(code: CodeType) -> tuple[int, int]:
    """Compute 1-based inclusive (start_line, end_line) span for a Python CodeType."""
    start_line = max(1, code.co_firstlineno)
    max_line = start_line
    for _, _, lineno in code.co_lines():
        if lineno is not None and lineno > max_line:
            max_line = lineno
    return (start_line, max_line)


def _matches_module_prefixes(module_name: str, prefixes: tuple[str, ...]) -> bool:
    """Return True when module_name matches any configured dotted module prefix."""
    if not module_name:
        return False
    for prefix in prefixes:
        if not prefix:
            continue
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            return True
    return False


def _resolve_participant_identity(
    module_name: str,
    qualname: str,
    granularity: Literal["module", "callable"],
) -> tuple[str, str, NodeKind]:
    """Derive (participant_id, label, NodeKind) for a matching Python code object."""
    if granularity == "module":
        return module_name, module_name.rsplit(".", 1)[-1], NodeKind.MODULE
    if "." in qualname and "<locals>" not in qualname:
        class_name = qualname.rsplit(".", 1)[0]
        return f"{module_name}.{class_name}", class_name, NodeKind.CLASS
    return f"{module_name}.{qualname}", qualname, NodeKind.FUNCTION


def _find_frame_for_code(code: CodeType, initial_frame: FrameType | None) -> FrameType | None:
    """Locate the active Python frame executing code, scanning at most 6 stack hops."""
    if initial_frame is not None:
        return initial_frame
    current = inspect.currentframe()
    frame: FrameType | None = current.f_back if current is not None else None
    for _ in range(6):
        if frame is None:
            return None
        if frame.f_code is code:
            return frame
        frame = frame.f_back
    return None


@dataclass(slots=True)
class _TraceRecorder:
    """Stateful call-stack recorder shared between sys.monitoring and sys.setprofile."""

    trace_id: str
    module_prefixes: tuple[str, ...]
    granularity: Literal["module", "callable"]
    max_events: int
    cwd: Path = field(default_factory=lambda: Path.cwd().resolve())
    origin_ns: int = field(default_factory=time.monotonic_ns)
    last_ns: int = 0
    seq: int = 0
    truncated: bool = False
    code_cache: dict[CodeType, _CodeMeta | None] = field(default_factory=dict)
    stack: list[_ActiveFrame] = field(default_factory=list)
    events: list[_RecordedEvent] = field(default_factory=list)
    events_by_id: dict[str, _RecordedEvent] = field(default_factory=dict)
    participants: dict[str, _ParticipantMeta] = field(default_factory=dict)

    def _next_ns(self) -> int:
        now = max(0, time.monotonic_ns() - self.origin_ns, self.last_ns)
        self.last_ns = now
        return now

    def resolve_code(self, code: CodeType, frame: FrameType | None = None) -> _CodeMeta | None:
        """Resolve and cache participant metadata for code; return None if out-of-scope."""
        if code in self.code_cache:
            return self.code_cache[code]
        filename = code.co_filename
        if not filename or filename.startswith("<"):
            self.code_cache[code] = None
            return None
        target_frame = _find_frame_for_code(code, frame)
        if target_frame is None:
            self.code_cache[code] = None
            return None
        raw_mod = target_frame.f_globals.get("__name__")
        mod_name = raw_mod if isinstance(raw_mod, str) else ""
        if not _matches_module_prefixes(mod_name, self.module_prefixes):
            self.code_cache[code] = None
            return None
        qualname = getattr(code, "co_qualname", code.co_name) or code.co_name
        pid, label, kind = _resolve_participant_identity(mod_name, qualname, self.granularity)
        meta = _CodeMeta(
            module_name=mod_name,
            qualname=qualname,
            participant_id=pid,
            label=label,
            kind=kind,
            file_path=_normalize_file_path(filename, self.cwd),
            line_span=_code_line_span(code),
        )
        self.code_cache[code] = meta
        return meta

    def _register_participant(self, meta: _CodeMeta) -> None:
        if meta.participant_id not in self.participants:
            self.participants[meta.participant_id] = _ParticipantMeta(
                participant_id=meta.participant_id,
                label=meta.label,
                kind=meta.kind,
                file_path=meta.file_path,
                line_span=meta.line_span,
            )

    def on_start(self, code: CodeType, frame: FrameType | None = None) -> bool:
        """Handle frame entry; return False when code is out-of-scope (eligible for DISABLE)."""
        meta = self.resolve_code(code, frame)
        if meta is None:
            return False

        parent_frame = self.stack[-1] if self.stack else None
        is_root = parent_frame is None
        is_cross_participant = (
            parent_frame is not None and parent_frame.participant_id != meta.participant_id
        )
        should_record = self.granularity == "callable" or is_root or is_cross_participant

        if not should_record and parent_frame is not None:
            self.stack.append(
                _ActiveFrame(
                    code=code,
                    participant_id=meta.participant_id,
                    event_id=None,
                    recorded_parent_id=parent_frame.recorded_parent_id,
                    recorded_depth=parent_frame.recorded_depth,
                )
            )
            return True

        if len(self.events) >= self.max_events:
            self.truncated = True
            self.stack.append(
                _ActiveFrame(
                    code=code,
                    participant_id=meta.participant_id,
                    event_id=None,
                    recorded_parent_id=parent_frame.recorded_parent_id if parent_frame else None,
                    recorded_depth=parent_frame.recorded_depth if parent_frame else 0,
                )
            )
            return True

        self._register_participant(meta)
        self.seq += 1
        event_id = f"ev_{self.seq:06d}"
        caller_id = parent_frame.participant_id if parent_frame is not None else meta.participant_id
        parent_eid = parent_frame.recorded_parent_id if parent_frame is not None else None
        depth = (
            0 if parent_eid is None else (parent_frame.recorded_depth + 1 if parent_frame else 0)
        )
        start_ns = self._next_ns()

        rec = _RecordedEvent(
            event_id=event_id,
            caller_id=caller_id,
            callee_id=meta.participant_id,
            signature=meta.qualname,
            parent_event_id=parent_eid,
            start_ns=start_ns,
            end_ns=start_ns,
            call_depth=depth,
            module_name=meta.module_name,
            qualname=meta.qualname,
            file_path=meta.file_path,
            line_span=meta.line_span,
        )
        self.events.append(rec)
        self.events_by_id[event_id] = rec
        self.stack.append(
            _ActiveFrame(
                code=code,
                participant_id=meta.participant_id,
                event_id=event_id,
                recorded_parent_id=event_id,
                recorded_depth=depth,
            )
        )
        return True

    def on_exit(self, code: CodeType, status: EventStatus) -> bool:
        """Handle frame return or unwind; finalize end_ns and status for matching stack frame."""
        meta = self.code_cache.get(code)
        if meta is None:
            return False
        end_ns = self._next_ns()
        while self.stack:
            top = self.stack.pop()
            if top.event_id is not None:
                rec = self.events_by_id[top.event_id]
                rec.end_ns = max(rec.start_ns, end_ns)
                rec.status = status if top.code is code else EventStatus.ERROR
            if top.code is code:
                break
        return True

    def finalize_unclosed(self) -> None:
        """Close any remaining open frames (e.g., abandoned generators) cleanly."""
        end_ns = self._next_ns()
        while self.stack:
            top = self.stack.pop()
            if top.event_id is not None:
                rec = self.events_by_id[top.event_id]
                rec.end_ns = max(rec.start_ns, end_ns)

    def build_ir(self, backend_name: str) -> InteractionTraceIR:
        """Assemble immutable InteractionTraceIR with deterministic content-derived SHA-256."""
        self.finalize_unclosed()
        digest_rows = [
            (
                ev.event_id,
                ev.caller_id,
                ev.callee_id,
                ev.signature,
                ev.parent_event_id,
                ev.call_depth,
                ev.status.value,
                ev.file_path,
                ev.line_span,
            )
            for ev in self.events
        ]
        digest_payload = json.dumps(
            {
                "trace_id": self.trace_id,
                "granularity": self.granularity,
                "events": digest_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        input_sha256 = hashlib.sha256(digest_payload).hexdigest()

        label_counts: dict[str, int] = {}
        for pmeta in self.participants.values():
            label_counts[pmeta.label] = label_counts.get(pmeta.label, 0) + 1

        participants: dict[str, TraceParticipant] = {}
        for pid in sorted(self.participants):
            pmeta = self.participants[pid]
            display_label = (
                ".".join(pid.split(".")[-2:])
                if label_counts.get(pmeta.label, 0) > 1 and "." in pid
                else pmeta.label
            )
            participants[pid] = TraceParticipant(
                participant_id=pmeta.participant_id,
                symbol_id=pmeta.participant_id,
                label=display_label,
                kind=pmeta.kind,
                provenance=ProvenanceStamp(
                    grade=ProvenanceGrade.OBSERVED,
                    extractor=_MONITOR_EXTRACTOR,
                    input_sha256=input_sha256,
                    file_path=pmeta.file_path,
                    line_span=pmeta.line_span,
                ),
            )

        events: dict[str, TraceEvent] = {}
        for ev in self.events:
            events[ev.event_id] = TraceEvent(
                event_id=ev.event_id,
                caller_id=ev.caller_id,
                callee_id=ev.callee_id,
                signature=ev.signature,
                parent_event_id=ev.parent_event_id,
                clock_domain_id=_DEFAULT_CLOCK_DOMAIN,
                start_ns=ev.start_ns,
                end_ns=max(ev.start_ns, ev.end_ns),
                sync_kind=SyncKind.SYNC,
                status=ev.status,
                attributes={
                    "call_depth": str(ev.call_depth),
                    "module": ev.module_name,
                    "qualname": ev.qualname,
                },
                provenance=ProvenanceStamp(
                    grade=ProvenanceGrade.OBSERVED,
                    extractor=_MONITOR_EXTRACTOR,
                    input_sha256=input_sha256,
                    file_path=ev.file_path,
                    line_span=ev.line_span,
                ),
            )

        return InteractionTraceIR(
            trace_id=self.trace_id,
            participants=participants,
            events=events,
            metadata={
                "extractor": _MONITOR_EXTRACTOR,
                "backend": backend_name,
                "granularity": self.granularity,
                "input_sha256": input_sha256,
                "truncated": "true" if self.truncated else "false",
            },
        )


def _acquire_monitoring_tool_id() -> int | None:
    """Acquire a free PEP 669 sys.monitoring tool ID, or return None if unavailable."""
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return None
    for tool_id in _CANDIDATE_TOOL_IDS:
        if monitoring.get_tool(tool_id) is None:
            try:
                monitoring.use_tool_id(tool_id, "archigram_trace")
            except ValueError:
                continue
            return tool_id
    return None


def _run_with_sys_monitoring(
    fn: Callable[[], object],
    recorder: _TraceRecorder,
    tool_id: int,
) -> None:
    """Execute fn under PEP 669 sys.monitoring with guaranteed tool ID cleanup."""
    monitoring = sys.monitoring
    ev_start = monitoring.events.PY_START
    ev_return = monitoring.events.PY_RETURN
    ev_unwind = monitoring.events.PY_UNWIND
    disable = monitoring.DISABLE

    def _on_start(code: CodeType, _offset: int) -> object:
        return None if recorder.on_start(code) else disable

    def _on_return(code: CodeType, _offset: int, _retval: object) -> object:
        return None if recorder.on_exit(code, EventStatus.OK) else disable

    def _on_unwind(code: CodeType, _offset: int, _exc: BaseException) -> None:
        recorder.on_exit(code, EventStatus.ERROR)

    try:
        monitoring.register_callback(tool_id, ev_start, _on_start)
        monitoring.register_callback(tool_id, ev_return, _on_return)
        monitoring.register_callback(tool_id, ev_unwind, _on_unwind)
        monitoring.set_events(tool_id, ev_start | ev_return | ev_unwind)
        fn()
    finally:
        monitoring.set_events(tool_id, 0)
        monitoring.register_callback(tool_id, ev_start, None)
        monitoring.register_callback(tool_id, ev_return, None)
        monitoring.register_callback(tool_id, ev_unwind, None)
        monitoring.free_tool_id(tool_id)
        monitoring.restart_events()


def _run_with_setprofile(fn: Callable[[], object], recorder: _TraceRecorder) -> None:
    """Execute fn under sys.setprofile fallback with guaranteed profiler restoration."""

    def _profiler(frame: FrameType, event: str, _arg: Any) -> None:
        if event == "call":
            recorder.on_start(frame.f_code, frame)
        elif event == "return":
            status = EventStatus.ERROR if sys.exc_info()[0] is not None else EventStatus.OK
            recorder.on_exit(frame.f_code, status)

    previous = sys.getprofile()
    try:
        sys.setprofile(_profiler)
        fn()
    finally:
        sys.setprofile(previous)


def capture_python_trace(
    fn: Callable[[], object],
    *,
    trace_id: str = "python_runtime_trace",
    module_prefixes: tuple[str, ...] = ("archigram",),
    granularity: Literal["module", "callable"] = "module",
    max_events: int = 5000,
) -> InteractionTraceIR:
    """Capture a live Python execution trace into Canonical InteractionTraceIR."""
    if not trace_id.strip():
        msg = "trace_id must not be blank"
        raise ValueError(msg)
    if granularity not in {"module", "callable"}:
        msg = f"Unsupported granularity {granularity!r}: expected 'module' or 'callable'"
        raise ValueError(msg)
    if max_events < 0:
        msg = f"max_events must be >= 0, got {max_events}"
        raise ValueError(msg)

    recorder = _TraceRecorder(
        trace_id=trace_id,
        module_prefixes=module_prefixes,
        granularity=granularity,
        max_events=max_events,
    )
    tool_id = _acquire_monitoring_tool_id()
    if tool_id is not None:
        _run_with_sys_monitoring(fn, recorder, tool_id)
        return recorder.build_ir("sys.monitoring")
    _run_with_setprofile(fn, recorder)
    return recorder.build_ir("sys.setprofile")


def _read_json_source(source: Path | str) -> tuple[str, str, str | None]:
    """Read a JSON trace payload from a Path, file path string, or raw JSON string."""
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        return raw_bytes.decode("utf-8"), hashlib.sha256(raw_bytes).hexdigest(), source.as_posix()

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
                candidate.as_posix(),
            )

    raw_bytes = source.encode("utf-8")
    return source, hashlib.sha256(raw_bytes).hexdigest(), None


def _parse_optional_line_span(raw_span: Any) -> tuple[int, int] | None:
    """Parse optional [start, end] integer pair into a valid 1-based line_span."""
    if (
        isinstance(raw_span, list)
        and len(raw_span) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in raw_span)
    ):
        start = max(1, int(raw_span[0]))
        end = max(start, int(raw_span[1]))
        return (start, end)
    return None


def _parse_spec_participants(
    raw_participants: Any,
    input_sha256: str,
    default_file: str | None,
) -> dict[str, TraceParticipant]:
    """Parse optional declarative participant metadata entries from a trace spec."""
    participants: dict[str, TraceParticipant] = {}
    items: list[Any] = (
        list(raw_participants.values())
        if isinstance(raw_participants, dict)
        else raw_participants
        if isinstance(raw_participants, list)
        else []
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("participant_id") or item.get("id") or "").strip()
        if not pid:
            continue
        label = str(item.get("label") or pid.rsplit(".", 1)[-1]).strip() or pid
        raw_kind = str(item.get("kind") or NodeKind.MODULE.value).strip().lower()
        kind = _NODE_KIND_BY_NAME.get(raw_kind, NodeKind.MODULE)
        file_path = str(item.get("file_path") or "").strip() or default_file
        participants[pid] = TraceParticipant(
            participant_id=pid,
            symbol_id=str(item.get("symbol_id") or pid).strip() or pid,
            label=label,
            kind=kind,
            provenance=ProvenanceStamp(
                grade=ProvenanceGrade.OBSERVED,
                extractor=_JSON_EXTRACTOR,
                input_sha256=input_sha256,
                file_path=file_path,
                line_span=_parse_optional_line_span(item.get("line_span")),
            ),
        )
    return participants


def _ensure_spec_participant(
    pid: str,
    participants: dict[str, TraceParticipant],
    input_sha256: str,
    file_path: str | None,
) -> None:
    """Register a default MODULE TraceParticipant if not already present."""
    if pid not in participants:
        participants[pid] = TraceParticipant(
            participant_id=pid,
            symbol_id=pid,
            label=pid.rsplit(".", 1)[-1] or pid,
            kind=NodeKind.MODULE,
            provenance=ProvenanceStamp(
                grade=ProvenanceGrade.OBSERVED,
                extractor=_JSON_EXTRACTOR,
                input_sha256=input_sha256,
                file_path=file_path,
            ),
        )


def _build_event_from_spec(
    item: dict[str, Any],
    idx: int,
    default_clock: str,
    input_sha256: str,
    default_file: str | None,
) -> TraceEvent:
    """Convert one declarative JSON event spec dictionary into a validated TraceEvent."""
    event_id = str(item.get("event_id") or f"ev_{idx + 1:06d}").strip()
    caller_id = str(item.get("caller_id") or item.get("caller") or "").strip()
    callee_id = str(item.get("callee_id") or item.get("callee") or "").strip()
    if not caller_id or not callee_id:
        msg = f"Trace spec event '{event_id}' requires non-blank caller and callee"
        raise ValueError(msg)
    signature = str(item.get("signature") or item.get("name") or "call()").strip() or "call()"
    raw_parent = item.get("parent_event_id")
    parent_id = (
        str(raw_parent).strip() if raw_parent is not None and str(raw_parent).strip() else None
    )
    raw_links = item.get("causal_links")
    causal_links = (
        tuple(str(link).strip() for link in raw_links if str(link).strip())
        if isinstance(raw_links, list)
        else ()
    )
    start_ns = max(0, int(item.get("start_ns", idx * 1000)))
    end_ns = max(start_ns, int(item.get("end_ns", start_ns)))
    status = _STATUS_BY_NAME.get(str(item.get("status", "ok")).strip().lower(), EventStatus.OK)
    sync_kind = _SYNC_BY_NAME.get(str(item.get("sync_kind", "sync")).strip().lower(), SyncKind.SYNC)
    raw_attrs = item.get("attributes")
    attrs = (
        {str(k): str(v) for k, v in sorted(raw_attrs.items()) if str(k).strip()}
        if isinstance(raw_attrs, dict)
        else {}
    )
    file_path = str(item.get("file_path") or "").strip() or default_file
    return TraceEvent(
        event_id=event_id,
        caller_id=caller_id,
        callee_id=callee_id,
        signature=signature,
        parent_event_id=parent_id,
        causal_links=causal_links,
        clock_domain_id=str(item.get("clock_domain_id") or default_clock).strip() or default_clock,
        start_ns=start_ns,
        end_ns=end_ns,
        sync_kind=sync_kind,
        status=status,
        attributes=attrs,
        provenance=ProvenanceStamp(
            grade=ProvenanceGrade.OBSERVED,
            extractor=_JSON_EXTRACTOR,
            input_sha256=input_sha256,
            file_path=file_path,
            line_span=_parse_optional_line_span(item.get("line_span")),
        ),
    )


def _ingest_declarative_spec(
    payload: dict[str, Any],
    input_sha256: str,
    default_file: str | None,
) -> InteractionTraceIR:
    """Build InteractionTraceIR from a deterministic trace specification JSON object."""
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        msg = "Trace specification JSON must contain an 'events' array"
        raise ValueError(msg)
    trace_id = str(payload.get("trace_id") or "python_trace").strip() or "python_trace"
    default_clock = str(payload.get("clock_domain_id") or _DEFAULT_CLOCK_DOMAIN).strip()
    participants = _parse_spec_participants(payload.get("participants"), input_sha256, default_file)
    events: dict[str, TraceEvent] = {}

    for idx, entry in enumerate(raw_events):
        if not isinstance(entry, dict):
            msg = f"Trace spec event at index {idx} must be a JSON object"
            raise ValueError(msg)
        ev = _build_event_from_spec(entry, idx, default_clock, input_sha256, default_file)
        _ensure_spec_participant(ev.caller_id, participants, input_sha256, ev.provenance.file_path)
        _ensure_spec_participant(ev.callee_id, participants, input_sha256, ev.provenance.file_path)
        events[ev.event_id] = ev

    sorted_participants = {k: participants[k] for k in sorted(participants)}
    sorted_events = {k: events[k] for k in sorted(events)}
    return InteractionTraceIR(
        trace_id=trace_id,
        participants=sorted_participants,
        events=sorted_events,
        metadata={
            "extractor": _JSON_EXTRACTOR,
            "input_sha256": input_sha256,
        },
    )


class PythonTraceIngestor(TraceIngestorPort):
    """Ingestor for saved InteractionTraceIR JSON payloads and declarative Python trace specs."""

    @property
    def extractor_name(self) -> str:
        return _JSON_EXTRACTOR

    def ingest(self, source: Path | str) -> InteractionTraceIR:
        """Deserialize a saved InteractionTraceIR JSON or parse a deterministic trace spec JSON."""
        raw_text, input_sha256, default_file = _read_json_source(source)
        try:
            payload: Any = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            msg = f"Invalid Python trace JSON payload: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(payload, dict):
            msg = "Python trace JSON payload must be a JSON object"
            raise ValueError(msg)
        if payload.get("ir_mode") == "interaction_trace" or isinstance(payload.get("events"), dict):
            return InteractionTraceIR.model_validate(payload)
        return _ingest_declarative_spec(payload, input_sha256, default_file)
