"""Gate 5 browser-free headless syntax validators for emitted diagram targets."""

from __future__ import annotations

import contextlib
import io
import itertools
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pydot

from archigram.ports.contracts import SyntaxValidationResult, SyntaxValidatorPort

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_MERMAID_DIRECTIONS = frozenset({"TD", "TB", "BT", "RL", "LR"})
_MERMAID_SEQ_OPENERS = frozenset({"par", "loop", "opt", "alt", "critical", "break", "rect", "box"})
_MERMAID_SEQ_BRANCH_PARENTS: dict[str, frozenset[str]] = {
    "and": frozenset({"par"}),
    "else": frozenset({"alt"}),
    "option": frozenset({"critical"}),
}
_MERMAID_SKIP_LABEL_PREFIXES = (
    "style ",
    "classdef ",
    "linkstyle ",
    "class ",
    "click ",
    "direction ",
    "acctitle:",
    "accdescr",
)
_MERMAID_COMPOUND_DELIMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("(((", (")))",)),
    ("[[", ("]]",)),
    ("([", ("])",)),
    ("[(", (")]",)),
    ("((", ("))",)),
    ("{{", ("}}",)),
    ("[/", ("/]", "\\]")),
    ("[\\", ("\\]", "/]")),
    ("[", ("]",)),
    ("(", (")",)),
    ("{", ("}",)),
)
_MERMAID_SEQ_MSG_RE = re.compile(r"(?:<<-->>|<<->>|-->>|->>|-->|->|--x|-x|--\)|-\))[^:]*:\s*(.*)$")
_MERMAID_PARTICIPANT_RE = re.compile(
    r"^(?:create\s+)?(?:participant|actor)\s+([^\s]+)(?:\s+as\s+.+)?$",
    re.IGNORECASE,
)
_HTML_ENTITY_RE = re.compile(r"(?:#(?:\d+|x[0-9a-fA-F]+)|&[A-Za-z][A-Za-z0-9]*);")

_D2_BAD_ARROW_RE = re.compile(r"(?<![<-])(?:-->|<--|->>|<<-|==>|---)(?![>-])")
_D2_ARROW_RE = re.compile(r"<->|->|<-|--")

_PUML_FRAG_OPEN_RE = re.compile(
    r"^(alt|opt|loop|par|break|critical|group)(?:\s+.*)?$",
    re.IGNORECASE,
)
_PUML_ELSE_RE = re.compile(r"^else(?:\s+.*)?$", re.IGNORECASE)
_PUML_SKINPARAM_SPACE_RE = re.compile(r"\s+<<")
_PUML_STEREO_INNER_SPACE_RE = re.compile(r"<<[^>]*\s+[^>]*>>")

_LIKEC4_TOP_KEYWORDS = frozenset(
    {"specification", "model", "views", "global", "deployment", "import"}
)
_LIKEC4_REQUIRED_BLOCKS = frozenset({"specification", "model", "views"})
_LIKEC4_ARROW_RE = re.compile(r"-\[[^\]]*\]->|->|<-")

_DOT_GRAPH_HDR_RE = re.compile(r"^(?:strict\s+)?(digraph|graph)\b", re.IGNORECASE)
_DOT_TRAILING_HDR_RE = re.compile(
    r"^(?:strict\s+)?(?:digraph|graph)(?:\s+[A-Za-z_\"0-9.-]+)?\s*\{",
    re.IGNORECASE,
)

_FORMAT_ALIASES: dict[str, str] = {
    "mermaid": "mermaid",
    "mmd": "mermaid",
    ".mmd": "mermaid",
    "d2": "d2",
    ".d2": "d2",
    "plantuml": "plantuml",
    "puml": "plantuml",
    ".puml": "plantuml",
    "likec4": "likec4",
    "c4": "likec4",
    ".c4": "likec4",
    "dot": "dot",
    ".dot": "dot",
    "graphviz": "dot",
}


@dataclass(frozen=True, slots=True)
class _MaskOptions:
    """Configuration for stripping comments and masking string contents."""

    single_quotes: bool = False
    hash_comments: bool = False
    slash_comments: bool = False
    block_comments: bool = False
    percent_comments: bool = False
    puml_tick_comments: bool = False


def _run_cli_check(
    executable: str,
    args: tuple[str, ...],
    source: str,
    timeout: float,
) -> list[str]:
    binary = shutil.which(executable)
    if binary is None:
        return []
    try:
        proc = subprocess.run(
            [binary, *args],
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"{executable} CLI check failed: {exc}"]
    if proc.returncode == 0:
        return []
    output = (proc.stderr or proc.stdout).strip()
    if not output:
        return [f"{executable} CLI exited with status {proc.returncode}"]
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return ["; ".join(lines[:4])]


def _find_closing_quote(line: str, start: int, quote_char: str) -> tuple[int, bool]:
    idx = start
    while idx < len(line):
        next_q = line.find(quote_char, idx)
        if next_q == -1:
            return len(line), True
        pos = next_q - 1
        backslashes = 0
        while pos >= start and line[pos] == "\\":
            backslashes += 1
            pos -= 1
        if backslashes % 2 == 0:
            return next_q + 1, False
        idx = next_q + 1
    return len(line), True


def _starts_line_comment(
    line: str,
    idx: int,
    options: _MaskOptions,
) -> bool:
    rest = line[idx:]
    if options.hash_comments and rest.startswith("#"):
        return True
    if options.slash_comments and rest.startswith("//"):
        return True
    if options.percent_comments and rest.startswith("%%"):
        return True
    return (
        options.puml_tick_comments
        and idx == len(line) - len(line.lstrip())
        and rest.startswith("'")
    )


def _needs_masking(line: str, options: _MaskOptions) -> bool:
    if '"' in line or "'" in line or "\\" in line:
        return True
    if options.hash_comments and "#" in line:
        return True
    if (options.slash_comments or options.block_comments) and "/" in line:
        return True
    return options.percent_comments and "%" in line


def _mask_line(
    line: str,
    options: _MaskOptions,
    in_block_comment: bool,
) -> tuple[str, bool, str | None]:
    if not in_block_comment and not _needs_masking(line, options):
        return line, False, None
    out: list[str] = []
    idx = 0
    unclosed: str | None = None
    length = len(line)

    while idx < length:
        if in_block_comment:
            end_tok = "*/" if line.find("*/", idx) != -1 else "'/"
            end_pos = line.find(end_tok, idx)
            if end_pos == -1 or (end_tok == "'/" and not options.puml_tick_comments):
                out.append(" " * (length - idx))
                break
            out.append(" " * (end_pos + 2 - idx))
            idx = end_pos + 2
            in_block_comment = False
            continue
        if options.block_comments and (
            line.startswith("/*", idx)
            or (options.puml_tick_comments and line.startswith("/'", idx))
        ):
            in_block_comment = True
            out.append("  ")
            idx += 2
            continue
        if _starts_line_comment(line, idx, options):
            break
        ch = line[idx]
        if ch == '"' or (options.single_quotes and ch == "'"):
            end_idx, still_open = _find_closing_quote(line, idx + 1, ch)
            if still_open:
                unclosed = ch
                out.append(ch + " " * (end_idx - idx - 1))
            else:
                out.append(ch + " " * (end_idx - idx - 2) + ch)
            idx = end_idx
            continue
        out.append(ch)
        idx += 1

    return "".join(out), in_block_comment, unclosed


def _mask_source(source: str, options: _MaskOptions) -> tuple[list[str], list[str]]:
    masked: list[str] = []
    errors: list[str] = []
    in_block = False

    for line_no, raw_line in enumerate(source.splitlines(), start=1):
        cleaned, in_block, unclosed_quote = _mask_line(raw_line, options, in_block)
        if unclosed_quote is not None:
            errors.append(f"Line {line_no}: unclosed {unclosed_quote} string literal")
        masked.append(cleaned)

    if in_block:
        errors.append("Unclosed block comment")
    return masked, errors


_ALL_DELIM_RE = re.compile(r"[()\[\]{}]")


def _check_delimiter_balance(
    lines: Sequence[str],
    pairs: Sequence[tuple[str, str]],
) -> list[str]:
    openers = {op: cl for op, cl in pairs}
    closers = {cl: op for op, cl in pairs}
    stack: list[tuple[str, int]] = []
    errors: list[str] = []

    for line_no, line in enumerate(lines, start=1):
        for match in _ALL_DELIM_RE.finditer(line):
            ch = match.group(0)
            if ch in openers:
                stack.append((ch, line_no))
            elif ch in closers:
                expected = closers[ch]
                if not stack or stack[-1][0] != expected:
                    errors.append(f"Line {line_no}: unexpected '{ch}'")
                    return errors
                stack.pop()

    if stack:
        op, ln = stack[-1]
        errors.append(f"Line {ln}: unclosed '{op}' delimiter")
    return errors


def _skip_mermaid_trivia(lines: Sequence[str], start: int) -> int:
    idx = start
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("%%"):
            idx += 1
            continue
        return idx
    return idx


def _consume_mermaid_frontmatter(lines: Sequence[str]) -> tuple[int, list[str]]:
    idx = _skip_mermaid_trivia(lines, 0)
    if idx >= len(lines) or lines[idx].strip() != "---":
        return idx, []
    idx += 1
    while idx < len(lines):
        if lines[idx].strip() == "---":
            return _skip_mermaid_trivia(lines, idx + 1), []
        idx += 1
    return idx, ["Unclosed Mermaid YAML frontmatter '---' block"]


def _parse_mermaid_header(header_line: str) -> tuple[str | None, list[str]]:
    cleaned = header_line.split("%%", 1)[0].strip().rstrip(";").strip()
    parts = cleaned.split()
    result: tuple[str | None, list[str]] = (
        None,
        [f"Unsupported or invalid Mermaid diagram header: '{cleaned}'"],
    )
    if not parts:
        result = (None, ["Missing Mermaid diagram header"])
    elif parts[0] in {"flowchart", "graph"}:
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in _MERMAID_DIRECTIONS):
            result = (None, [f"Invalid Mermaid {parts[0]} direction in header: '{cleaned}'"])
        else:
            result = ("flowchart", [])
    elif parts[0] in {"classDiagram", "classDiagram-v2"} and len(parts) == 1:
        result = ("classDiagram", [])
    elif parts[0] == "sequenceDiagram" and len(parts) == 1:
        result = ("sequenceDiagram", [])
    elif parts[0] in {"stateDiagram", "stateDiagram-v2"} and len(parts) == 1:
        result = ("stateDiagram", [])
    return result


_FLOWCHART_INTEREST_RE = re.compile(r'["(\[{]')
_CLOSER_INTEREST_RE = re.compile(r'["\)\]/\\]')


def _skip_quoted_span(line: str, quote_pos: int) -> int:
    idx = quote_pos + 1
    while idx < len(line):
        next_q = line.find('"', idx)
        if next_q == -1:
            return len(line)
        if line[next_q - 1] != "\\":
            return next_q + 1
        backslashes = 0
        pos = next_q - 1
        while pos >= quote_pos + 1 and line[pos] == "\\":
            backslashes += 1
            pos -= 1
        if backslashes % 2 == 0:
            return next_q + 1
        idx = next_q + 1
    return len(line)


def _find_mermaid_closer(
    line: str,
    start: int,
    closers: tuple[str, ...],
) -> tuple[int, int] | None:
    idx = start
    while idx < len(line):
        match = _CLOSER_INTEREST_RE.search(line, idx)
        if match is None:
            return None
        pos = match.start()
        if line[pos] == '"':
            idx = _skip_quoted_span(line, pos)
            continue
        for closer in closers:
            if line.startswith(closer, pos):
                return pos, pos + len(closer)
        idx = pos + 1
    return None


def _is_wrapped_in_quotes(text: str) -> bool:
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return False
    return _skip_quoted_span(text, 0) == len(text)


def _match_mermaid_opener(line: str, idx: int) -> tuple[str, tuple[str, ...]] | None:
    for opener, closers in _MERMAID_COMPOUND_DELIMS:
        if line.startswith(opener, idx):
            return opener, closers
    return None


_MERMAID_PROP_BLOCK_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*@\{.*\}$")


def _check_mermaid_flowchart_labels(raw_line: str, line_no: int) -> list[str]:
    stripped = raw_line.strip()
    if (
        not stripped
        or stripped.lower().startswith(_MERMAID_SKIP_LABEL_PREFIXES)
        or _MERMAID_PROP_BLOCK_RE.match(stripped) is not None
    ):
        return []
    errors: list[str] = []
    idx = 0
    while idx < len(raw_line):
        match = _FLOWCHART_INTEREST_RE.search(raw_line, idx)
        if match is None:
            break
        pos = match.start()
        if raw_line[pos] == '"':
            idx = _skip_quoted_span(raw_line, pos)
            continue
        matched = _match_mermaid_opener(raw_line, pos)
        if matched is not None:
            opener, closers = matched
            span = _find_mermaid_closer(raw_line, pos + len(opener), closers)
            if span is not None:
                inner = raw_line[pos + len(opener) : span[0]].strip()
                if not _is_wrapped_in_quotes(inner) and any(bad in inner for bad in "()[]{}"):
                    errors.append(
                        f"Line {line_no}: unquoted flowchart label '{inner}' "
                        "contains delimiter characters"
                    )
                idx = span[1]
                continue
        idx = pos + 1
    return errors


def _check_mermaid_sequence_line(
    line: str,
    line_no: int,
    stack: list[str],
) -> list[str]:
    stripped = line.strip()
    if not stripped:
        return []
    errors: list[str] = []
    p_match = _MERMAID_PARTICIPANT_RE.match(stripped)
    if p_match and p_match.group(1).lower() == "end":
        errors.append(
            f"Line {line_no}: unquoted reserved keyword 'end' used as participant/actor id"
        )
    msg_match = _MERMAID_SEQ_MSG_RE.search(stripped)
    if msg_match:
        msg_body = _HTML_ENTITY_RE.sub("", msg_match.group(1))
        if ";" in msg_body:
            errors.append(f"Line {line_no}: unescaped raw ';' in sequence message")

    head = stripped.split()[0]
    if head in _MERMAID_SEQ_OPENERS:
        stack.append(head)
    elif head in _MERMAID_SEQ_BRANCH_PARENTS:
        allowed = _MERMAID_SEQ_BRANCH_PARENTS[head]
        if not stack or stack[-1] not in allowed:
            errors.append(f"Line {line_no}: '{head}' outside matching block")
    elif head == "end":
        if not stack:
            errors.append(f"Line {line_no}: unexpected 'end' with no open block")
        else:
            stack.pop()
    return errors


def _check_mermaid_flowchart_line(
    stripped: str,
    raw_line: str,
    line_no: int,
    stack: list[str],
) -> list[str]:
    errors: list[str] = []
    head = stripped.split()[0]
    if head == "subgraph":
        stack.append("subgraph")
    elif head == "end":
        if not stack:
            errors.append(f"Line {line_no}: unexpected 'end' with no open subgraph")
        else:
            stack.pop()
    errors.extend(_check_mermaid_flowchart_labels(raw_line, line_no))
    return errors


def _check_mermaid_class_line(
    stripped: str,
    raw_line: str,
    line_no: int,
    in_class_body: bool,
) -> tuple[bool, list[str]]:
    lowered = stripped.lower()
    errors: list[str] = []
    if not in_class_body and lowered.startswith(("classdef ", "style ")) and ":" in stripped:
        errors.append(
            f"Line {line_no}: '{stripped.split()[0]}' with ':' attributes "
            "is invalid in Mermaid classDiagram"
        )
    if in_class_body:
        if stripped == "}":
            return False, errors
        if ";" in raw_line:
            errors.append(f"Line {line_no}: ';' inside Mermaid classDiagram member block")
    elif stripped.endswith("{"):
        return True, errors
    return in_class_body, errors


def _validate_mermaid_body(
    kind: str,
    masked_lines: Sequence[str],
    raw_lines: Sequence[str],
    body_start: int,
) -> list[str]:
    errors: list[str] = []
    stack: list[str] = []
    in_class_body = False
    for offset, masked in enumerate(masked_lines[body_start:]):
        line_no = body_start + offset + 1
        stripped = masked.strip()
        if not stripped:
            continue
        raw_line = raw_lines[line_no - 1]
        if kind == "flowchart":
            errors.extend(_check_mermaid_flowchart_line(stripped, raw_line, line_no, stack))
        elif kind == "classDiagram":
            in_class_body, cls_errs = _check_mermaid_class_line(
                stripped, raw_line, line_no, in_class_body
            )
            errors.extend(cls_errs)
        elif kind == "sequenceDiagram":
            errors.extend(_check_mermaid_sequence_line(stripped, line_no, stack))
        elif kind == "stateDiagram" and ";" in stripped:
            errors.append(f"Line {line_no}: raw ';' is invalid in Mermaid stateDiagram")
    if stack:
        errors.append(f"Unclosed Mermaid block(s): {', '.join(stack)}")
    return errors


class MermaidSyntaxValidator:
    """Browser-free deterministic lexical and structural validator for Mermaid diagrams."""

    @property
    def target_format(self) -> str:
        return "mermaid"

    def validate(self, diagram_source: str) -> SyntaxValidationResult:
        if not diagram_source.strip():
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=("Empty mermaid diagram source",),
            )
        raw_lines = diagram_source.splitlines()
        header_idx, fm_errors = _consume_mermaid_frontmatter(raw_lines)
        if fm_errors:
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=tuple(fm_errors),
            )
        if header_idx >= len(raw_lines):
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=("Missing Mermaid diagram header",),
            )
        kind, hdr_errors = _parse_mermaid_header(raw_lines[header_idx])
        if hdr_errors or kind is None:
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=tuple(hdr_errors),
            )
        masked, scan_errors = _mask_source(diagram_source, _MaskOptions(percent_comments=True))
        delim_pairs = (
            (("[", "]"), ("{", "}"))
            if kind in {"sequenceDiagram", "stateDiagram"}
            else (
                ("(", ")"),
                ("[", "]"),
                ("{", "}"),
            )
        )
        errors = [
            *scan_errors,
            *_check_delimiter_balance(masked[header_idx + 1 :], delim_pairs),
            *_validate_mermaid_body(kind, masked, raw_lines, header_idx + 1),
        ]
        return SyntaxValidationResult(
            valid=not errors,
            target_format=self.target_format,
            errors=tuple(errors),
        )


def _check_d2_statement(stmt: str, line_no: int) -> list[str]:
    clean = stmt.strip().lstrip("}").rstrip("{").strip()
    if not clean:
        return []
    if _D2_BAD_ARROW_RE.search(clean):
        return [f"Line {line_no}: invalid D2 arrow operator in '{clean}'"]
    edge_part = clean.split(":", 1)[0].strip()
    matches = list(_D2_ARROW_RE.finditer(edge_part))
    errors: list[str] = []
    if matches:
        if matches[0].start() == 0 or matches[-1].end() == len(edge_part):
            errors.append(f"Line {line_no}: dangling D2 arrow endpoint in '{edge_part}'")
        elif any(
            not edge_part[prev.end() : curr.start()].strip()
            for prev, curr in itertools.pairwise(matches)
        ):
            errors.append(f"Line {line_no}: consecutive D2 arrows in '{edge_part}'")
    return errors


class D2SyntaxValidator:
    """Headless D2 syntax validator with optional `d2 fmt -` subprocess verification."""

    def __init__(self, *, use_cli: bool = True, cli_timeout: float = 5.0) -> None:
        self._use_cli = use_cli
        self._cli_timeout = cli_timeout

    @property
    def target_format(self) -> str:
        return "d2"

    def validate(self, diagram_source: str) -> SyntaxValidationResult:
        if not diagram_source.strip():
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=("Empty d2 diagram source",),
            )
        masked, errors = _mask_source(
            diagram_source,
            _MaskOptions(single_quotes=True, hash_comments=True),
        )
        errors.extend(_check_delimiter_balance(masked, (("{", "}"), ("[", "]"))))
        for line_no, line in enumerate(masked, start=1):
            for stmt in line.split(";"):
                errors.extend(_check_d2_statement(stmt, line_no))
        if not errors and self._use_cli:
            errors.extend(_run_cli_check("d2", ("fmt", "-"), diagram_source, self._cli_timeout))
        return SyntaxValidationResult(
            valid=not errors,
            target_format=self.target_format,
            errors=tuple(errors),
        )


def _check_puml_skinparam_line(stripped: str, line_no: int) -> list[str]:
    if _PUML_SKINPARAM_SPACE_RE.search(stripped):
        return [f"Line {line_no}: PlantUML skinparam stereotype trap (whitespace before '<<')"]
    if _PUML_STEREO_INNER_SPACE_RE.search(stripped):
        return [f"Line {line_no}: PlantUML skinparam stereotype trap (whitespace inside '<<...>>')"]
    return []


@dataclass(slots=True)
class _PumlState:
    started: bool = False
    ended: bool = False
    brace_depth: int = 0
    in_skinparam_block: bool = False
    in_note: bool = False
    in_style: bool = False
    frag_stack: list[str] | None = None

    def stack(self) -> list[str]:
        if self.frag_stack is None:
            self.frag_stack = []
        return self.frag_stack


def _handle_puml_framing(stripped: str, line_no: int, state: _PumlState) -> tuple[bool, list[str]]:
    lowered = stripped.lower()
    errors: list[str] = []
    handled = True
    if lowered.startswith("@startuml"):
        if state.started and not state.ended:
            errors.append(f"Line {line_no}: nested '@startuml' before '@enduml'")
        state.started = True
        state.ended = False
    elif lowered == "@enduml":
        if not state.started:
            errors.append(f"Line {line_no}: '@enduml' without matching '@startuml'")
        elif state.ended:
            errors.append(f"Line {line_no}: duplicate '@enduml'")
        state.ended = True
    elif not state.started:
        errors.append(f"Line {line_no}: PlantUML content before '@startuml'")
    elif state.ended:
        errors.append(f"Line {line_no}: PlantUML content after '@enduml'")
    else:
        handled = False
    return handled, errors


def _consume_puml_block_skip(lowered: str, stripped: str, state: _PumlState) -> bool:
    skip = True
    if lowered.startswith("<style>"):
        state.in_style = True
    elif lowered.startswith("</style>"):
        state.in_style = False
    elif state.in_style:
        pass
    elif lowered.startswith("note ") and ":" not in stripped:
        state.in_note = True
    elif state.in_note:
        if lowered == "end note":
            state.in_note = False
    else:
        skip = False
    return skip


def _check_puml_fork_line(lowered: str, line_no: int, state: _PumlState) -> tuple[bool, list[str]]:
    handled = True
    errors: list[str] = []
    if lowered == "fork":
        state.stack().append("fork")
    elif lowered == "fork again":
        if not state.stack() or state.stack()[-1] != "fork":
            errors.append(f"Line {line_no}: PlantUML 'fork again' outside 'fork'")
    elif lowered == "end fork":
        if not state.stack() or state.stack()[-1] != "fork":
            errors.append(f"Line {line_no}: PlantUML 'end fork' without open 'fork'")
        else:
            state.stack().pop()
    else:
        handled = False
    return handled, errors


def _check_puml_fragment_line(
    stripped: str, lowered: str, line_no: int, state: _PumlState
) -> list[str]:
    if stripped.endswith("{") or stripped.startswith("}"):
        return []
    handled_fork, fork_errs = _check_puml_fork_line(lowered, line_no, state)
    if handled_fork:
        return fork_errs
    errors: list[str] = []
    frag_match = _PUML_FRAG_OPEN_RE.match(stripped)
    if frag_match:
        state.stack().append(frag_match.group(1).lower())
    elif _PUML_ELSE_RE.match(stripped):
        if not state.stack() or state.stack()[-1] not in {"alt", "par", "critical"}:
            errors.append(f"Line {line_no}: PlantUML 'else' outside 'alt'/'par'/'critical'")
    elif lowered == "end":
        if not state.stack() or state.stack()[-1] == "fork":
            errors.append(f"Line {line_no}: unexpected 'end' with no open fragment")
        else:
            state.stack().pop()
    return errors


def _handle_puml_line(stripped: str, line_no: int, state: _PumlState) -> list[str]:
    lowered = stripped.lower()
    if _consume_puml_block_skip(lowered, stripped, state):
        return []
    errors: list[str] = []
    if lowered.startswith("skinparam ") or state.in_skinparam_block:
        errors.extend(_check_puml_skinparam_line(stripped, line_no))
    if lowered.startswith("skinparam ") and stripped.endswith("{"):
        state.in_skinparam_block = True
    state.brace_depth += stripped.count("{") - stripped.count("}")
    if state.brace_depth == 0:
        state.in_skinparam_block = False
    errors.extend(_check_puml_fragment_line(stripped, lowered, line_no, state))
    return errors


class PlantUmlSyntaxValidator:
    """Headless PlantUML syntax validator with optional `plantuml -checkonly` support."""

    def __init__(self, *, use_cli: bool = False, cli_timeout: float = 5.0) -> None:
        self._use_cli = use_cli
        self._cli_timeout = cli_timeout

    @property
    def target_format(self) -> str:
        return "plantuml"

    def validate(self, diagram_source: str) -> SyntaxValidationResult:
        if not diagram_source.strip():
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=("Empty plantuml diagram source",),
            )
        masked, errors = _mask_source(
            diagram_source,
            _MaskOptions(block_comments=True, puml_tick_comments=True),
        )
        errors.extend(_check_delimiter_balance(masked, (("{", "}"),)))
        state = _PumlState()
        for line_no, line in enumerate(masked, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            handled, frame_errs = _handle_puml_framing(stripped, line_no, state)
            errors.extend(frame_errs)
            if not handled:
                errors.extend(_handle_puml_line(stripped, line_no, state))

        if not state.started:
            errors.append("Missing '@startuml' header")
        elif not state.ended:
            errors.append("Missing '@enduml' footer")
        if state.stack():
            errors.append(f"Unclosed PlantUML fragment(s): {', '.join(state.stack())}")
        if not errors and self._use_cli:
            errors.extend(
                _run_cli_check(
                    "plantuml",
                    ("-checkonly", "-failfast2", "-pipe"),
                    diagram_source,
                    self._cli_timeout,
                )
            )
        return SyntaxValidationResult(
            valid=not errors,
            target_format=self.target_format,
            errors=tuple(errors),
        )


def _check_likec4_statement(stmt: str, line_no: int) -> list[str]:
    clean = stmt.strip().lstrip("}").rstrip("{").strip()
    if not clean:
        return []
    arrows = list(_LIKEC4_ARROW_RE.finditer(clean))
    if len(arrows) >= 2:
        return [f"Line {line_no}: chained LikeC4 arrows are forbidden in '{clean}'"]
    if len(arrows) == 1:
        before = clean[: arrows[0].start()].strip()
        after = clean[arrows[0].end() :].strip()
        if not before or not after:
            return [f"Line {line_no}: dangling LikeC4 arrow in '{clean}'"]
    return []


def _check_likec4_framing(masked_lines: Sequence[str]) -> list[str]:
    depth = 0
    seen_blocks: set[str] = set()
    errors: list[str] = []

    for line_no, line in enumerate(masked_lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if depth == 0:
            head = (
                stripped.split("{", 1)[0].strip().split()[0] if not stripped.startswith("{") else ""
            )
            if head and head not in _LIKEC4_TOP_KEYWORDS:
                errors.append(f"Line {line_no}: unexpected top-level LikeC4 statement '{head}'")
            elif head in _LIKEC4_REQUIRED_BLOCKS:
                seen_blocks.add(head)
        else:
            for stmt in stripped.split(";"):
                errors.extend(_check_likec4_statement(stmt, line_no))
        depth += stripped.count("{") - stripped.count("}")

    if not seen_blocks:
        errors.append(
            "LikeC4 diagram must contain a top-level 'model', 'views', or 'specification' block"
        )
    return errors


class LikeC4SyntaxValidator:
    """Headless LikeC4 DSL syntax validator."""

    @property
    def target_format(self) -> str:
        return "likec4"

    def validate(self, diagram_source: str) -> SyntaxValidationResult:
        if not diagram_source.strip():
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=("Empty likec4 diagram source",),
            )
        masked, errors = _mask_source(
            diagram_source,
            _MaskOptions(single_quotes=True, slash_comments=True, block_comments=True),
        )
        errors.extend(_check_delimiter_balance(masked, (("{", "}"),)))
        errors.extend(_check_likec4_framing(masked))
        return SyntaxValidationResult(
            valid=not errors,
            target_format=self.target_format,
            errors=tuple(errors),
        )


def _strip_dot_html_labels(text: str) -> str:
    current = text
    for _ in range(16):
        updated = re.sub(r"<[^<>]*>", " ", current)
        if updated == current:
            break
        current = updated
    return current


def _check_dot_structure(masked_lines: Sequence[str]) -> list[str]:
    joined = "\n".join(masked_lines)
    no_html = _strip_dot_html_labels(joined)
    hdr_match = _DOT_GRAPH_HDR_RE.search(no_html)
    if hdr_match is None:
        return ["Missing DOT 'graph' or 'digraph' header"]
    graph_kind = hdr_match.group(1).lower()

    depth = 0
    saw_block = False
    after_block_chunks: list[str] = []
    for ch in no_html:
        if ch == "{":
            depth += 1
            saw_block = True
        elif ch == "}":
            depth -= 1
        elif saw_block and depth == 0:
            after_block_chunks.append(ch)

    trailing = "".join(after_block_chunks).strip()
    if trailing and not _DOT_TRAILING_HDR_RE.match(trailing):
        return [f"Unexpected trailing tokens after DOT graph block: '{trailing[:40]}'"]
    if graph_kind == "digraph" and "--" in no_html:
        return ["Undirected edge '--' is invalid inside DOT digraph"]
    if graph_kind == "graph" and "->" in no_html:
        return ["Directed edge '->' is invalid inside undirected DOT graph"]
    return []


def _run_pydot_parse(diagram_source: str) -> list[str]:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            graphs = pydot.graph_from_dot_data(diagram_source)
    except Exception as exc:
        return [f"DOT parse error: {exc}"]
    if not graphs:
        captured = " ".join(line.strip() for line in buf.getvalue().splitlines() if line.strip())
        return [captured or "Invalid DOT syntax"]
    return []


class DotSyntaxValidator:
    """Headless Graphviz DOT validator via pydot + lexical checks + optional `dot -Tcanon`."""

    def __init__(self, *, use_cli: bool = False, cli_timeout: float = 5.0) -> None:
        self._use_cli = use_cli
        self._cli_timeout = cli_timeout

    @property
    def target_format(self) -> str:
        return "dot"

    def validate(self, diagram_source: str) -> SyntaxValidationResult:
        if not diagram_source.strip():
            return SyntaxValidationResult(
                valid=False,
                target_format=self.target_format,
                errors=("Empty dot diagram source",),
            )
        masked, errors = _mask_source(
            diagram_source,
            _MaskOptions(hash_comments=True, slash_comments=True, block_comments=True),
        )
        errors.extend(_check_delimiter_balance(masked, (("{", "}"), ("[", "]"))))
        errors.extend(_check_dot_structure(masked))
        if not errors:
            errors.extend(_run_pydot_parse(diagram_source))
        if not errors and self._use_cli:
            errors.extend(_run_cli_check("dot", ("-Tcanon",), diagram_source, self._cli_timeout))
        return SyntaxValidationResult(
            valid=not errors,
            target_format=self.target_format,
            errors=tuple(errors),
        )


def build_default_validators(*, use_cli: bool | None = None) -> dict[str, SyntaxValidatorPort]:
    """Construct the canonical Gate 5 validator registry keyed by target format."""
    return {
        "mermaid": MermaidSyntaxValidator(),
        "d2": D2SyntaxValidator() if use_cli is None else D2SyntaxValidator(use_cli=use_cli),
        "plantuml": (
            PlantUmlSyntaxValidator()
            if use_cli is None
            else PlantUmlSyntaxValidator(use_cli=use_cli)
        ),
        "likec4": LikeC4SyntaxValidator(),
        "dot": DotSyntaxValidator() if use_cli is None else DotSyntaxValidator(use_cli=use_cli),
    }


DEFAULT_VALIDATORS: dict[str, SyntaxValidatorPort] = build_default_validators()


class Gate5SyntaxHarness:
    """Dispatcher mapping emitted diagram target formats to headless Gate 5 validators."""

    slice_name: str = "lint_syntax"

    def __init__(
        self,
        validators: Mapping[str, SyntaxValidatorPort] | None = None,
    ) -> None:
        self._validators: dict[str, SyntaxValidatorPort] = (
            dict(validators) if validators is not None else build_default_validators()
        )

    @property
    def supported_formats(self) -> tuple[str, ...]:
        return tuple(sorted(self._validators))

    def get_validator(self, target_format: str) -> SyntaxValidatorPort | None:
        normalized = _FORMAT_ALIASES.get(
            target_format.strip().lower(), target_format.strip().lower()
        )
        return self._validators.get(normalized)

    def validate(self, target_format: str, diagram_source: str) -> SyntaxValidationResult:
        validator = self.get_validator(target_format)
        if validator is None:
            return SyntaxValidationResult(
                valid=False,
                target_format=target_format,
                errors=(f"Unsupported diagram target format: '{target_format}'",),
            )
        return validator.validate(diagram_source)
