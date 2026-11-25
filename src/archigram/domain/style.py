"""Canonical dual-mode (light/dark) diagram styling defaults, routing, and UML arrow policy.

Design rules enforced across all ArchiGram emitters (Mermaid, D2, PlantUML, LikeC4, DOT):
1. Dual-theme readiness: Element, container, subgraph, class, and participant backgrounds are
   ALWAYS ``transparent`` with neutral mid-luminance borders (``#64748b``) so diagrams render
   cleanly on both light (``#ffffff``) and dark (``#0d1117`` / ``#0f172a``) surfaces.
2. Edge-only semantic color: Visual color is reserved exclusively for relationship strokes
   using a mid-luminance WCAG-legible hex palette keyed by ``EdgeRelation`` and ``SyncKind``.
3. Orthogonal / linear routing: Structural edges default to straight or 90-degree orthogonal
   segments (ELK + ``curve: linear`` in Mermaid, ELK in D2, ``linetype ortho`` in PlantUML,
   ``splines="ortho"`` in Graphviz DOT).
4. Canonical UML arrowheads: Generalization (``<|--``), realization (``<|..``), composition
   (``*--``), aggregation (``o--``), dependency (``..>``), and async open arrows are mapped
   uniformly from ``EdgeRelation`` and ``SyncKind``.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from archigram.domain.ir import EdgeRelation, SyncKind

TRANSPARENT_FILL = "transparent"
NEUTRAL_BORDER_HEX = "#64748b"
DEFAULT_STROKE_WIDTH_PX = "1.75px"
DEFAULT_NODE_BORDER_WIDTH_PX = "1.5px"


class LinePattern(StrEnum):
    """Canonical stroke dash/weight pattern for rendered edges."""

    SOLID = "solid"
    DASHED = "dashed"
    BOLD = "bold"


class UmlArrowheadKind(StrEnum):
    """Canonical UML 2 relationship arrowhead semantics."""

    OPEN_VEE = "open_vee"
    HOLLOW_TRIANGLE = "hollow_triangle"
    FILLED_DIAMOND = "filled_diamond"
    HOLLOW_DIAMOND = "hollow_diamond"
    ASYNC_OPEN = "async_open"


class EdgeVisualStyle(BaseModel):
    """Immutable dual-mode visual specification for one semantic ``EdgeRelation``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: EdgeRelation
    stroke_hex: str
    likec4_color: str
    line_pattern: LinePattern
    uml_arrowhead: UmlArrowheadKind
    dot_arrowhead: str
    dot_arrowtail: str | None = None
    animated: bool = False
    usage_note: str


CANONICAL_EDGE_STYLES: dict[EdgeRelation, EdgeVisualStyle] = {
    EdgeRelation.CALLS: EdgeVisualStyle(
        relation=EdgeRelation.CALLS,
        stroke_hex="#3b82f6",
        likec4_color="blue",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.OPEN_VEE,
        dot_arrowhead="vee",
        animated=True,
        usage_note="Synchronous function, method, or service invocation (animated flow).",
    ),
    EdgeRelation.IMPORTS: EdgeVisualStyle(
        relation=EdgeRelation.IMPORTS,
        stroke_hex="#64748b",
        likec4_color="slate",
        line_pattern=LinePattern.DASHED,
        uml_arrowhead=UmlArrowheadKind.OPEN_VEE,
        dot_arrowhead="vee",
        animated=False,
        usage_note="Static module, package, or type-import dependency (UML dashed dependency).",
    ),
    EdgeRelation.INHERITS: EdgeVisualStyle(
        relation=EdgeRelation.INHERITS,
        stroke_hex="#8b5cf6",
        likec4_color="indigo",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.HOLLOW_TRIANGLE,
        dot_arrowhead="empty",
        animated=False,
        usage_note="UML Generalization: subclass extends superclass (hollow triangle).",
    ),
    EdgeRelation.IMPLEMENTS: EdgeVisualStyle(
        relation=EdgeRelation.IMPLEMENTS,
        stroke_hex="#a855f7",
        likec4_color="indigo",
        line_pattern=LinePattern.DASHED,
        uml_arrowhead=UmlArrowheadKind.HOLLOW_TRIANGLE,
        dot_arrowhead="empty",
        animated=False,
        usage_note="UML Realization: concrete class implements Protocol/interface.",
    ),
    EdgeRelation.COMPOSES: EdgeVisualStyle(
        relation=EdgeRelation.COMPOSES,
        stroke_hex="#14b8a6",
        likec4_color="green",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.FILLED_DIAMOND,
        dot_arrowhead="diamond",
        dot_arrowtail="diamond",
        animated=False,
        usage_note="UML Composition: lifecycle-bound part ownership (filled diamond).",
    ),
    EdgeRelation.AGGREGATES: EdgeVisualStyle(
        relation=EdgeRelation.AGGREGATES,
        stroke_hex="#06b6d4",
        likec4_color="sky",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.HOLLOW_DIAMOND,
        dot_arrowhead="odiamond",
        dot_arrowtail="odiamond",
        animated=False,
        usage_note="UML Aggregation: shared reference or collection ownership (hollow diamond).",
    ),
    EdgeRelation.ASSOCIATES: EdgeVisualStyle(
        relation=EdgeRelation.ASSOCIATES,
        stroke_hex="#6366f1",
        likec4_color="indigo",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.OPEN_VEE,
        dot_arrowhead="vee",
        animated=False,
        usage_note="Structural field or parameter type association.",
    ),
    EdgeRelation.READS_DATA: EdgeVisualStyle(
        relation=EdgeRelation.READS_DATA,
        stroke_hex="#10b981",
        likec4_color="green",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.OPEN_VEE,
        dot_arrowhead="vee",
        animated=True,
        usage_note="Read-only query or state inspection from a store or artifact (animated flow).",
    ),
    EdgeRelation.WRITES_DATA: EdgeVisualStyle(
        relation=EdgeRelation.WRITES_DATA,
        stroke_hex="#f59e0b",
        likec4_color="amber",
        line_pattern=LinePattern.SOLID,
        uml_arrowhead=UmlArrowheadKind.OPEN_VEE,
        dot_arrowhead="vee",
        animated=True,
        usage_note="Mutating write, persistence, or artifact emission (animated flow).",
    ),
    EdgeRelation.ASYNC_SPAWN: EdgeVisualStyle(
        relation=EdgeRelation.ASYNC_SPAWN,
        stroke_hex="#ec4899",
        likec4_color="red",
        line_pattern=LinePattern.DASHED,
        uml_arrowhead=UmlArrowheadKind.ASYNC_OPEN,
        dot_arrowhead="normalnoneodot",
        animated=True,
        usage_note="Asynchronous fire-and-forget task or event dispatch (animated flow).",
    ),
    EdgeRelation.FORK_JOIN: EdgeVisualStyle(
        relation=EdgeRelation.FORK_JOIN,
        stroke_hex="#f97316",
        likec4_color="amber",
        line_pattern=LinePattern.BOLD,
        uml_arrowhead=UmlArrowheadKind.OPEN_VEE,
        dot_arrowhead="vee",
        animated=True,
        usage_note="Structured concurrent fork-join branch execution (animated flow).",
    ),
}


def resolve_edge_visual_style(
    relation: EdgeRelation,
    sync_kind: SyncKind = SyncKind.SYNC,
) -> EdgeVisualStyle:
    """Resolve the effective ``EdgeVisualStyle`` for an ``(EdgeRelation, SyncKind)`` pair."""
    base = CANONICAL_EDGE_STYLES[relation]
    if sync_kind is SyncKind.ASYNC_FIRE_FORGET:
        if relation is EdgeRelation.CALLS:
            return CANONICAL_EDGE_STYLES[EdgeRelation.ASYNC_SPAWN].model_copy(
                update={"relation": relation}
            )
        if base.line_pattern is not LinePattern.DASHED or not base.animated:
            return base.model_copy(update={"line_pattern": LinePattern.DASHED, "animated": True})
    elif sync_kind is SyncKind.FORK_JOIN_BRANCH and base.line_pattern is LinePattern.SOLID:
        return base.model_copy(
            update={
                "line_pattern": LinePattern.BOLD,
                "stroke_hex": CANONICAL_EDGE_STYLES[EdgeRelation.FORK_JOIN].stroke_hex,
                "animated": True,
            }
        )
    return base


def resolve_edge_animated(
    visual: EdgeVisualStyle,
    metadata_override: str | None = None,
) -> bool:
    """Resolve whether an edge should animate, honoring per-view metadata override if set."""
    if metadata_override == "true":
        return True
    if metadata_override == "false":
        return False
    return visual.animated


def relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.1 relative luminance in ``[0.0, 1.0]`` for a ``#rrggbb`` hex color."""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        msg = f"Expected 6-digit hex color '#rrggbb', got {hex_color!r}"
        raise ValueError(msg)
    channels: list[float] = []
    for offset in (0, 2, 4):
        srgb = int(cleaned[offset : offset + 2], 16) / 255.0
        linear = srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4
        channels.append(linear)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """Compute WCAG 2.1 contrast ratio in ``[1.0, 21.0]`` between two ``#rrggbb`` colors."""
    lum_a = relative_luminance(hex_a)
    lum_b = relative_luminance(hex_b)
    lighter = max(lum_a, lum_b)
    darker = min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _check_hex_color(value: str, field_name: str) -> None:
    if _HEX_COLOR_RE.match(value) is None:
        msg = f"Field '{field_name}' must be a 6-digit '#rrggbb' hex color, got {value!r}"
        raise ValueError(msg)


class MermaidWebModePalette(BaseModel):
    """Explicit light or dark surface/text palette for centralized web Mermaid rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    background_hex: str
    surface_hex: str
    text_hex: str
    muted_text_hex: str
    border_hex: str = NEUTRAL_BORDER_HEX
    line_hex: str = NEUTRAL_BORDER_HEX
    signal_hex: str = "#3b82f6"

    @model_validator(mode="after")
    def _validate_contrast(self) -> MermaidWebModePalette:
        for fname in (
            "background_hex",
            "surface_hex",
            "text_hex",
            "muted_text_hex",
            "border_hex",
            "line_hex",
            "signal_hex",
        ):
            _check_hex_color(getattr(self, fname), fname)
        if contrast_ratio(self.text_hex, self.background_hex) < 7.0:
            msg = "Primary text_hex must achieve >= 7.0:1 WCAG AAA contrast on background_hex"
            raise ValueError(msg)
        if contrast_ratio(self.text_hex, self.surface_hex) < 7.0:
            msg = "Primary text_hex must achieve >= 7.0:1 WCAG AAA contrast on surface_hex"
            raise ValueError(msg)
        if contrast_ratio(self.muted_text_hex, self.background_hex) < 4.5:
            msg = "muted_text_hex must achieve >= 4.5:1 WCAG AA contrast on background_hex"
            raise ValueError(msg)
        return self


_DEFAULT_WEB_LIGHT = MermaidWebModePalette(
    background_hex="#ffffff",
    surface_hex="#f8fafc",
    text_hex="#0f172a",
    muted_text_hex="#475569",
    border_hex=NEUTRAL_BORDER_HEX,
    line_hex=NEUTRAL_BORDER_HEX,
    signal_hex="#3b82f6",
)

_DEFAULT_WEB_DARK = MermaidWebModePalette(
    background_hex="#0b0f19",
    surface_hex="#111827",
    text_hex="#e2e8f0",
    muted_text_hex="#94a3b8",
    border_hex=NEUTRAL_BORDER_HEX,
    line_hex="#94a3b8",
    signal_hex="#3b82f6",
)


class MermaidThemeConfig(BaseModel):
    """Central repository-level Mermaid layout and dual-mode styling policy.

    Maintained once per repository (e.g. ``docs/architecture/mermaid-theme.yaml``) so AI
    curation specs (``CurationSpecV1``) never duplicate or override colors per diagram.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    layout_engine: Literal["elk", "dagre"] = "elk"
    elk_merge_edges: bool = False
    elk_node_placement_strategy: Literal[
        "BRANDES_KOEPF",
        "NETWORK_SIMPLEX",
        "LINEAR_SEGMENTS",
        "SIMPLE",
    ] = "BRANDES_KOEPF"
    flowchart_curve: Literal["linear", "basis", "stepBefore", "stepAfter"] = "linear"
    sequence_mirror_actors: bool = False
    node_fill: Literal["transparent"] = "transparent"
    node_border_hex: str = NEUTRAL_BORDER_HEX
    node_border_width_px: str = DEFAULT_NODE_BORDER_WIDTH_PX
    subgraph_border_hex: str = NEUTRAL_BORDER_HEX
    subgraph_border_width_px: str = "1px"
    subgraph_dash_array: str = "4 4"
    edge_stroke_width_px: str = DEFAULT_STROKE_WIDTH_PX
    bold_edge_stroke_width_px: str = "2.5px"
    dashed_edge_array: str = "5 5"
    web_light: MermaidWebModePalette = _DEFAULT_WEB_LIGHT
    web_dark: MermaidWebModePalette = _DEFAULT_WEB_DARK

    @model_validator(mode="after")
    def _validate_dual_mode_bounds(self) -> MermaidThemeConfig:
        for fname in ("node_border_hex", "subgraph_border_hex"):
            val = getattr(self, fname)
            _check_hex_color(val, fname)
            lum = relative_luminance(val)
            if not (0.08 <= lum <= 0.65):
                msg = f"'{fname}' ({val}) must stay mid-luminance [0.08, 0.65] for light/dark"
                raise ValueError(msg)
        if relative_luminance(self.web_light.background_hex) < 0.5:
            msg = "web_light.background_hex must be a light surface (luminance >= 0.5)"
            raise ValueError(msg)
        if relative_luminance(self.web_dark.background_hex) > 0.15:
            msg = "web_dark.background_hex must be a dark surface (luminance <= 0.15)"
            raise ValueError(msg)
        return self

    def structural_frontmatter(self) -> str:
        """Render host-theme-safe YAML frontmatter (layout & routing only, zero khroma traps)."""
        merge_str = "true" if self.elk_merge_edges else "false"
        return (
            "---\n"
            "config:\n"
            f"  layout: {self.layout_engine}\n"
            "  elk:\n"
            f"    mergeEdges: {merge_str}\n"
            f"    nodePlacementStrategy: {self.elk_node_placement_strategy}\n"
            "  flowchart:\n"
            f"    defaultRenderer: {self.layout_engine}\n"
            f"    curve: {self.flowchart_curve}\n"
            "---"
        )

    def sequence_frontmatter(self) -> str:
        """Render host-theme-safe YAML frontmatter for sequenceDiagram."""
        mirror_str = "true" if self.sequence_mirror_actors else "false"
        return (
            "---\n"
            "config:\n"
            "  sequence:\n"
            f"    mirrorActors: {mirror_str}\n"
            "    messageAlign: center\n"
            "---"
        )

    def state_frontmatter(self) -> str:
        """Render host-theme-safe YAML frontmatter for stateDiagram-v2."""
        return f"---\nconfig:\n  layout: {self.layout_engine}\n---"

    def default_classdef(self) -> str:
        """Render dual-mode flowchart node classDef without overriding host text color."""
        return (
            f"classDef default fill:{self.node_fill},"
            f"stroke:{self.node_border_hex},"
            f"stroke-width:{self.node_border_width_px}"
        )

    def subgraph_style_attrs(self) -> str:
        """Render dual-mode subgraph container style attributes."""
        return (
            f"fill:{self.node_fill},"
            f"stroke:{self.subgraph_border_hex},"
            f"stroke-width:{self.subgraph_border_width_px},"
            f"stroke-dasharray:{self.subgraph_dash_array}"
        )

    def web_theme_variables(self, mode: Literal["light", "dark"]) -> dict[str, str | bool]:
        """Build complete Mermaid `theme: 'base'` themeVariables using real hex colors."""
        pal = self.web_dark if mode == "dark" else self.web_light
        return {
            "darkMode": mode == "dark",
            "background": pal.background_hex,
            "primaryColor": pal.surface_hex,
            "secondaryColor": pal.surface_hex,
            "tertiaryColor": pal.background_hex,
            "mainBkg": pal.surface_hex,
            "primaryTextColor": pal.text_hex,
            "secondaryTextColor": pal.text_hex,
            "tertiaryTextColor": pal.muted_text_hex,
            "textColor": pal.text_hex,
            "primaryBorderColor": pal.border_hex,
            "nodeBorder": pal.border_hex,
            "clusterBkg": "transparent",
            "clusterBorder": self.subgraph_border_hex,
            "lineColor": pal.line_hex,
            "edgeLabelBackground": pal.background_hex,
            "actorBkg": pal.surface_hex,
            "actorBorder": pal.border_hex,
            "actorTextColor": pal.text_hex,
            "actorLineColor": pal.border_hex,
            "signalColor": pal.signal_hex,
            "signalTextColor": pal.text_hex,
            "labelBoxBkgColor": pal.surface_hex,
            "labelBoxBorderColor": pal.border_hex,
            "labelTextColor": pal.text_hex,
            "loopTextColor": pal.text_hex,
            "activationBkgColor": pal.surface_hex,
            "activationBorderColor": pal.border_hex,
            "noteBkgColor": pal.surface_hex,
            "noteBorderColor": pal.border_hex,
            "noteTextColor": pal.text_hex,
            "classText": pal.text_hex,
            "titleColor": pal.text_hex,
        }


def parse_mermaid_theme_yaml(raw_yaml: str) -> MermaidThemeConfig:
    """Parse and strictly validate a ``MermaidThemeConfig`` from YAML text."""
    loaded: object = yaml.safe_load(raw_yaml)
    if loaded is None:
        msg = "Mermaid theme YAML must be a mapping, got empty document"
        raise ValueError(msg)
    return MermaidThemeConfig.model_validate(loaded)


def dump_mermaid_theme_yaml(theme: MermaidThemeConfig) -> str:
    """Serialize a ``MermaidThemeConfig`` into deterministic canonical YAML."""
    payload = theme.model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=False)


def render_mermaid_web_theme_js(theme: MermaidThemeConfig | None = None) -> str:
    """Emit deterministic shared browser JS helper (`docs/mermaid-theme.js`) from central config."""
    resolved = theme if theme is not None else DEFAULT_MERMAID_THEME
    themes_json = json.dumps(
        {
            "light": resolved.web_theme_variables("light"),
            "dark": resolved.web_theme_variables("dark"),
        },
        indent=2,
    )
    flowchart_json = json.dumps(
        {
            "defaultRenderer": resolved.layout_engine,
            "curve": resolved.flowchart_curve,
        }
    )
    return (
        "// Generated from docs/architecture/mermaid-theme.yaml — do not edit per diagram.\n"
        "(function (root) {\n"
        f"  const ARCHIGRAM_MERMAID_THEMES = {themes_json};\n"
        "  function resolveMode(override) {\n"
        "    if (override === 'light' || override === 'dark') return override;\n"
        "    const attr = document.documentElement.getAttribute('data-theme');\n"
        "    if (attr === 'light' || attr === 'dark') return attr;\n"
        "    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';\n"
        "  }\n"
        "  function buildMermaidConfig(mode) {\n"
        "    const active = resolveMode(mode);\n"
        "    return {\n"
        "      startOnLoad: false,\n"
        "      securityLevel: 'strict',\n"
        "      theme: 'base',\n"
        f"      layout: {json.dumps(resolved.layout_engine)},\n"
        f"      flowchart: {flowchart_json},\n"
        "      themeVariables: ARCHIGRAM_MERMAID_THEMES[active],\n"
        "    };\n"
        "  }\n"
        "  async function renderAllMermaid(mermaid, selector, mode) {\n"
        "    const sel = selector || '[data-mermaid-src]';\n"
        "    const nodes = Array.from(document.querySelectorAll(sel));\n"
        "    if (!nodes.length || !mermaid) return;\n"
        "    mermaid.initialize(buildMermaidConfig(mode));\n"
        "    for (let i = 0; i < nodes.length; i += 1) {\n"
        "      const el = nodes[i];\n"
        "      const raw = el.getAttribute('data-mermaid-src');\n"
        "      const src = raw || el.textContent || '';\n"
        "      if (!raw) el.setAttribute('data-mermaid-src', src);\n"
        "      const out = await mermaid.render('ag-mmd-' + i + '-' + Date.now(), src);\n"
        "      el.innerHTML = out.svg;\n"
        "    }\n"
        "  }\n"
        "  root.ArchiGramMermaidTheme = {\n"
        "    themes: ARCHIGRAM_MERMAID_THEMES,\n"
        "    resolveMode: resolveMode,\n"
        "    buildConfig: buildMermaidConfig,\n"
        "    renderAll: renderAllMermaid,\n"
        "  };\n"
        "})(typeof globalThis !== 'undefined' ? globalThis : window);\n"
    )


DEFAULT_MERMAID_THEME = MermaidThemeConfig()
MERMAID_GITEA_MAX_SOURCE_CHARS = 5000
MERMAID_STRUCTURAL_FRONTMATTER = DEFAULT_MERMAID_THEME.structural_frontmatter()
MERMAID_SEQUENCE_FRONTMATTER = DEFAULT_MERMAID_THEME.sequence_frontmatter()
MERMAID_STATE_FRONTMATTER = DEFAULT_MERMAID_THEME.state_frontmatter()
MERMAID_DEFAULT_CLASSDEF = DEFAULT_MERMAID_THEME.default_classdef()
MERMAID_SUBGRAPH_STYLE_ATTRS = DEFAULT_MERMAID_THEME.subgraph_style_attrs()

D2_CANONICAL_HEADER = (
    "vars: {\n"
    "  d2-config: {\n"
    "    layout-engine: elk\n"
    "    theme-id: 0\n"
    "    dark-theme-id: 200\n"
    "  }\n"
    "}\n"
    f"style.fill: {TRANSPARENT_FILL}"
)

PLANTUML_ORTHO_SKINPARAMS: tuple[str, ...] = (
    f"skinparam backgroundColor {TRANSPARENT_FILL}",
    "skinparam linetype ortho",
    "skinparam shadowing false",
    "skinparam componentStyle uml2",
    f"skinparam ArrowColor {NEUTRAL_BORDER_HEX}",
    f"skinparam PackageBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam PackageBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ComponentBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam ComponentBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ClassBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam ClassBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ClassHeaderBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam RectangleBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam RectangleBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam DatabaseBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam DatabaseBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam QueueBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam QueueBorderColor {NEUTRAL_BORDER_HEX}",
)

PLANTUML_SEQUENCE_SKINPARAMS: tuple[str, ...] = (
    f"skinparam backgroundColor {TRANSPARENT_FILL}",
    "skinparam shadowing false",
    f"skinparam SequenceArrowColor {CANONICAL_EDGE_STYLES[EdgeRelation.CALLS].stroke_hex}",
    f"skinparam SequenceParticipantBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam SequenceParticipantBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam SequenceLifeLineBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam SequenceGroupBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam SequenceGroupBodyBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam SequenceGroupBorderColor {NEUTRAL_BORDER_HEX}",
)

PLANTUML_BEHAVIORAL_SKINPARAMS: tuple[str, ...] = (
    f"skinparam backgroundColor {TRANSPARENT_FILL}",
    "skinparam shadowing false",
    f"skinparam ArrowColor {CANONICAL_EDGE_STYLES[EdgeRelation.CALLS].stroke_hex}",
    f"skinparam StateBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam StateBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ActivityBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam ActivityBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ActivityDiamondBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam ActivityDiamondBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam UsecaseBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam UsecaseBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ActorBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam ActorBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam RectangleBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam RectangleBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam NodeBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam NodeBorderColor {NEUTRAL_BORDER_HEX}",
    f"skinparam ArtifactBackgroundColor {TRANSPARENT_FILL}",
    f"skinparam ArtifactBorderColor {NEUTRAL_BORDER_HEX}",
)

TRIAGE_KEEP_HEX = "#10b981"
TRIAGE_REMOVE_HEX = "#ef4444"
TRIAGE_BYPASS_HEX = "#f59e0b"
TRIAGE_COLLAPSE_HEX = "#8b5cf6"
