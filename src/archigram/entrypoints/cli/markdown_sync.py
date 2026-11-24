"""Deterministic inline Markdown diagram synchronization for `archigram apply --sync-md`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from archigram.domain.spec import TargetRenderer

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from archigram.entrypoints.cli.pipeline import CompiledViewArtifact

_MARKER_LINE_RE = re.compile(
    r"^[ \t]*<!--\s*archigram:(start|end)\s+([^\s>]+)\s*-->[ \t]*$",
)

_RENDERER_FENCE_LANGUAGES: dict[TargetRenderer, str] = {
    TargetRenderer.MERMAID: "mermaid",
    TargetRenderer.D2: "d2",
    TargetRenderer.PLANTUML: "plantuml",
    TargetRenderer.LIKEC4: "likec4",
    TargetRenderer.DOT: "dot",
}


@dataclass(frozen=True, slots=True)
class MarkdownSyncOutcome:
    """Outcome of synchronizing or checking inline diagram blocks across Markdown files."""

    synced_files: tuple[Path, ...] = ()
    drifted_files: tuple[Path, ...] = ()
    matched_view_ids: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Return True when zero Markdown files drifted from compiled diagram artifacts."""
        return not self.drifted_files


@dataclass(slots=True)
class _MarkdownRenderState:
    """Mutable parser state while scanning inline `archigram:start`/`archigram:end` regions."""

    source_label: str
    strict_unknown_views: bool
    out_chunks: list[str] = field(default_factory=list)
    matched_views: list[str] = field(default_factory=list)
    active_view_id: str | None = None
    active_start_line: int = 0
    active_start_line_text: str = ""
    buffered_inner: list[str] = field(default_factory=list)

    def push_regular_line(self, line: str) -> None:
        if self.active_view_id is not None:
            self.buffered_inner.append(line)
        else:
            self.out_chunks.append(line)

    def open_block(
        self,
        view_id: str,
        line_no: int,
        line: str,
        compiled_views: Mapping[str, CompiledViewArtifact],
    ) -> None:
        if self.active_view_id is not None:
            msg = (
                f"{self.source_label}:{line_no}: nested or unclosed archigram:start '{view_id}' "
                f"inside open block '{self.active_view_id}' "
                f"(started at line {self.active_start_line})"
            )
            raise ValueError(msg)
        if self.strict_unknown_views and view_id not in compiled_views:
            msg = f"{self.source_label}:{line_no}: unknown archigram view_id '{view_id}'"
            raise ValueError(msg)
        self.active_view_id = view_id
        self.active_start_line = line_no
        self.active_start_line_text = line if line.endswith("\n") else f"{line}\n"
        self.buffered_inner.clear()

    def close_block(
        self,
        view_id: str,
        line_no: int,
        line: str,
        compiled_views: Mapping[str, CompiledViewArtifact],
    ) -> None:
        if self.active_view_id is None:
            msg = (
                f"{self.source_label}:{line_no}: unexpected archigram:end '{view_id}' "
                "without matching archigram:start"
            )
            raise ValueError(msg)
        if self.active_view_id != view_id:
            msg = (
                f"{self.source_label}:{line_no}: mismatched archigram:end '{view_id}' "
                f"for open block '{self.active_view_id}' "
                f"(started at line {self.active_start_line})"
            )
            raise ValueError(msg)
        artifact = compiled_views.get(self.active_view_id)
        self.out_chunks.append(self.active_start_line_text)
        if artifact is not None:
            self.out_chunks.append(f"{format_fenced_diagram_block(artifact)}\n")
            self.matched_views.append(self.active_view_id)
        else:
            self.out_chunks.extend(self.buffered_inner)
        self.out_chunks.append(line)
        self.active_view_id = None
        self.active_start_line = 0
        self.buffered_inner.clear()


def renderer_fence_language(renderer: TargetRenderer) -> str:
    """Return the canonical Markdown code-fence language identifier for a TargetRenderer."""
    return _RENDERER_FENCE_LANGUAGES[renderer]


def format_fenced_diagram_block(artifact: CompiledViewArtifact) -> str:
    """Format a compiled diagram artifact as a deterministic Markdown code fence block."""
    lang = renderer_fence_language(artifact.view.target_renderer)
    body = artifact.diagram_source.rstrip("\n")
    return f"```{lang}\n{body}\n```"


def _discover_markdown_files(sync_target: Path) -> list[Path]:
    """Resolve a single Markdown file or recursively discover sorted `*.md` files in a directory."""
    if not sync_target.exists():
        msg = f"Markdown sync target does not exist: {sync_target}"
        raise FileNotFoundError(msg)
    if sync_target.is_file():
        return [sync_target]
    return sorted(
        (p for p in sync_target.rglob("*.md") if p.is_file()),
        key=lambda p: p.relative_to(sync_target).as_posix(),
    )


def render_synced_markdown_text(
    markdown_text: str,
    compiled_views: Mapping[str, CompiledViewArtifact],
    *,
    source_label: str = "<markdown>",
    strict_unknown_views: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Replace inline `<!-- archigram:start <id> -->` blocks matching `compiled_views`."""
    state = _MarkdownRenderState(
        source_label=source_label,
        strict_unknown_views=strict_unknown_views,
    )
    for line_no, line in enumerate(markdown_text.splitlines(keepends=True), start=1):
        match = _MARKER_LINE_RE.match(line.rstrip("\r\n"))
        if match is None:
            state.push_regular_line(line)
        elif match.group(1) == "start":
            state.open_block(match.group(2), line_no, line, compiled_views)
        else:
            state.close_block(match.group(2), line_no, line, compiled_views)

    if state.active_view_id is not None:
        msg = (
            f"{source_label}:{state.active_start_line}: unclosed archigram:start "
            f"'{state.active_view_id}' reached end of file without matching archigram:end"
        )
        raise ValueError(msg)

    return "".join(state.out_chunks), tuple(state.matched_views)


def sync_markdown_views(
    compiled_views: Mapping[str, CompiledViewArtifact],
    sync_target: Path,
    *,
    check_only: bool = False,
    strict_unknown_views: bool = False,
) -> MarkdownSyncOutcome:
    """Synchronize or verify inline diagram blocks across a Markdown file or directory."""
    md_files = _discover_markdown_files(sync_target)
    synced: list[Path] = []
    drifted: list[Path] = []
    matched_ids: list[str] = []

    for md_path in md_files:
        original_bytes = md_path.read_bytes()
        original_text = original_bytes.decode("utf-8")
        updated_text, file_matched = render_synced_markdown_text(
            original_text,
            compiled_views,
            source_label=md_path.as_posix(),
            strict_unknown_views=strict_unknown_views,
        )
        matched_ids.extend(file_matched)
        updated_bytes = updated_text.encode("utf-8")
        if updated_bytes != original_bytes:
            if check_only:
                drifted.append(md_path)
            else:
                md_path.write_bytes(updated_bytes)
                synced.append(md_path)

    return MarkdownSyncOutcome(
        synced_files=tuple(synced),
        drifted_files=tuple(drifted),
        matched_view_ids=tuple(matched_ids),
    )
