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

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

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


MERMAID_GITEA_MAX_SOURCE_CHARS = 5000

MERMAID_STRUCTURAL_FRONTMATTER = (
    "---\n"
    "config:\n"
    "  layout: elk\n"
    "  elk:\n"
    "    mergeEdges: false\n"
    "    nodePlacementStrategy: BRANDES_KOEPF\n"
    "  flowchart:\n"
    "    defaultRenderer: elk\n"
    "    curve: linear\n"
    "  themeVariables:\n"
    "    primaryColor: transparent\n"
    "    secondaryColor: transparent\n"
    "    tertiaryColor: transparent\n"
    "    mainBkg: transparent\n"
    f"    nodeBorder: '{NEUTRAL_BORDER_HEX}'\n"
    "    clusterBkg: transparent\n"
    f"    clusterBorder: '{NEUTRAL_BORDER_HEX}'\n"
    f"    lineColor: '{NEUTRAL_BORDER_HEX}'\n"
    "---"
)

MERMAID_SEQUENCE_FRONTMATTER = (
    "---\n"
    "config:\n"
    "  themeVariables:\n"
    "    actorBkg: transparent\n"
    f"    actorBorder: '{NEUTRAL_BORDER_HEX}'\n"
    f"    actorLineColor: '{NEUTRAL_BORDER_HEX}'\n"
    f"    signalColor: '{CANONICAL_EDGE_STYLES[EdgeRelation.CALLS].stroke_hex}'\n"
    "    activationBkgColor: transparent\n"
    f"    activationBorderColor: '{NEUTRAL_BORDER_HEX}'\n"
    "    noteBkgColor: transparent\n"
    f"    noteBorderColor: '{NEUTRAL_BORDER_HEX}'\n"
    "---"
)

MERMAID_DEFAULT_CLASSDEF = (
    f"classDef default fill:{TRANSPARENT_FILL},"
    f"stroke:{NEUTRAL_BORDER_HEX},"
    f"stroke-width:{DEFAULT_NODE_BORDER_WIDTH_PX}"
)

MERMAID_SUBGRAPH_STYLE_ATTRS = (
    f"fill:{TRANSPARENT_FILL},stroke:{NEUTRAL_BORDER_HEX},stroke-width:1px,stroke-dasharray:4 4"
)

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
