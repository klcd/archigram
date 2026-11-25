---
name: dual-mode-diagrams
description: Design, compile, theme, and audit architecture, UML, sequence, and SVG diagrams that render with WCAG-compliant contrast on both light (#ffffff) and dark (#0b0f19 / #0d1117) backgrounds across GitHub, Gitea, docs sites, and standalone browsers. Use when creating or fixing Mermaid, D2, Graphviz DOT, PlantUML, LikeC4, or SVG diagrams; fixing unreadable dark-mode text, glaring white badges, or black 3D facets; or setting up centralized repository-wide Mermaid theming instead of per-diagram AI color blocks.
---

# Dual-Mode Diagrams & Centralized Mermaid Theming

Architecture diagrams routinely break in dark mode because renderers hardcode `#000000` text, `#ffffff` fills, or compute inverted text luminance from `"transparent"`. Follow these deterministic rules so every diagram renders cleanly on **both** light (`#ffffff` / `#f8fafc`) and dark (`#0b0f19` / `#0d1117` / `#0f172a`) surfaces—while keeping theme colors governed centrally in one human-owned config instead of scattered across AI-generated diagrams.

---

## 1. Core Optical Invariants (All Renderers)

1. **Transparent Canvas & Container Fills (`fill: transparent`):**
   - Root canvas, packages, subgraphs, swimlanes, and standard boxes use `transparent` backgrounds so the host surface (`#ffffff` or `#0b0f19`) shines through seamlessly without white rectangular "stickers" in dark mode.
   - **Never** pair a `transparent` fill with a hardcoded dark (`#000000`) or light (`#ffffff`) text color. If a shape requires an opaque fill (e.g. a UML class header band or web tooltip), both its light and dark surface/text pairs must be explicitly themed.
2. **Mid-Luminance Neutral Structural Borders (`L ∈ [0.12, 0.35]`):**
   - Default node, actor, lifeline, and container borders use `#64748b` (Slate 500, WCAG relative luminance `L ≈ 0.175`), which achieves ~4.8:1 contrast on `#ffffff` and ~4.0:1 contrast on `#0b0f19`.
   - Subgraph/package boundaries use `1px` dashed `#64748b` (`stroke-dasharray: 4 4`) so hierarchy never competes with primary edges.
3. **Edge-Only Semantic Color (`L ∈ [0.10, 0.48]`):**
   - Reserve saturated color exclusively for relationship strokes and status badges. Every semantic stroke must stay mid-luminance so it pops on white and slate-950 alike:

   | Semantic Role | Hex | Luminance `L` | Light Contrast (`#fff`) | Dark Contrast (`#0b0f19`) |
   | :--- | :--- | :---: | :---: | :---: |
   | Synchronous Call / Signal | `#3b82f6` | `0.255` | `3.4:1` | `5.3:1` |
   | Static Import / Dependency | `#64748b` | `0.175` | `4.7:1` | `3.9:1` |
   | Generalization (`<\|--`) | `#8b5cf6` | `0.223` | `3.8:1` | `4.7:1` |
   | Realization (`<\|..`) | `#a855f7` | `0.253` | `3.5:1` | `5.2:1` |
   | Composition (`*--`) | `#14b8a6` | `0.386` | `2.4:1` | `7.5:1` |
   | Read / Keep (`+`) | `#10b981` | `0.407` | `2.3:1` | `7.9:1` |
   | Write / Bypass (`~`) | `#f59e0b` | `0.480` | `2.0:1` | `9.2:1` |
   | Async / Error / Prune (`-`) | `#ef4444` | `0.226` | `3.8:1` | `4.8:1` |

4. **Adaptive Primary Text (`>= 7.0:1` WCAG AAA):**
   - Light mode primary text: `#0f172a` or `#1e293b` on `#ffffff` (`16–18:1`), muted text `#475569` (`7.6:1`).
   - Dark mode primary text: `#e2e8f0` or `#f1f5f9` on `#0b0f19` (`15–17:1`), muted text `#94a3b8` (`7.8:1`).

---

## 2. Centralized Mermaid Theming (Zero Per-Diagram AI Styling)

### Why AI Must Never Theme Individual Mermaid Diagrams
1. **Visual Drift:** Letting an LLM emit `%%{init: {'themeVariables': ...}}%%` or custom `classDef` palettes per diagram produces mismatched colors across docs and wastes character budget (Gitea caps Mermaid blocks at `5,000` chars).
2. **The `khroma` `primaryColor: transparent` Trap:** Mermaid's `base` and `dark` themes compute derived text colors (`nodeTextColor`, `actorTextColor`, `labelTextColor`) by passing `primaryColor` into `khroma.isDark(color)`. Passing `primaryColor: transparent` (rgba `0,0,0,0`) evaluates as **light** or `NaN`, causing Mermaid to emit **near-black text on dark backgrounds**!

### Architecture: One Central Config, Two Deterministic Targets

Maintain **one** human-owned config file in the repository (e.g. `docs/architecture/mermaid-theme.yaml`) validated by schema (`extra="forbid"` on AI curation specs so agents cannot override theme colors per view):

```yaml
# docs/architecture/mermaid-theme.yaml
schema_version: "1.0"
layout_engine: elk
elk_merge_edges: false
elk_node_placement_strategy: BRANDES_KOEPF
flowchart_curve: linear
sequence_mirror_actors: false
node_fill: transparent
node_border_hex: "#64748b"
node_border_width_px: "1.5px"
subgraph_border_hex: "#64748b"

web_light:
  background_hex: "#ffffff"
  surface_hex: "#f8fafc"   # Real #rrggbb hex — NEVER "transparent"!
  text_hex: "#0f172a"
  muted_text_hex: "#475569"
  border_hex: "#64748b"
  line_hex: "#64748b"
  signal_hex: "#3b82f6"

web_dark:
  background_hex: "#0b0f19"
  surface_hex: "#111827"   # Real #rrggbb hex — NEVER "transparent"!
  text_hex: "#e2e8f0"
  muted_text_hex: "#94a3b8"
  border_hex: "#64748b"
  line_hex: "#94a3b8"
  signal_hex: "#3b82f6"
```

From that single YAML file, your compiler emits:

1. **GitHub / Gitea Markdown `.mmd` Files (Layout-Only Frontmatter):**
   - Emit **only** layout & routing keys in `.mmd` YAML frontmatter—zero `theme:` or `themeVariables:`! GitHub and Gitea automatically switch their own light/dark host Mermaid theme for text contrast.
   - In `flowchart` diagrams, emit `classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px` **without** `color:`, preserving host-adaptive text while keeping node boxes transparent.
   - **Mermaid v10 `classDiagram` Parser Rule:** Do **not** emit `classDef default` inside `classDiagram` (Gitea's Mermaid v10 parser rejects it).

   ```mermaid
   ---
   config:
     layout: elk
     elk:
       mergeEdges: false
       nodePlacementStrategy: BRANDES_KOEPF
     flowchart:
       defaultRenderer: elk
       curve: linear
   ---
   flowchart TD
     classDef default fill:transparent,stroke:#64748b,stroke-width:1.5px
     A["Ingress Port"] --> B["Domain Core"]
     linkStyle 0 stroke:#3b82f6,stroke-width:1.75px
   ```

2. **Web Docs Sites (`mermaid-theme.js` Shared Initializer):**
   - Generate a single shared JS module (`docs/mermaid-theme.js`) that calls `mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'base', themeVariables: THEMES[mode] })` using the real `#rrggbb` `surface_hex` values from `web_light` / `web_dark`, and re-renders `[data-mermaid-src]` blocks whenever `prefers-color-scheme` or `html[data-theme]` changes.

---

## 3. Renderer-Specific Dark-Mode Landmines & Exact Fixes

### A. D2 (`d2` CLI SVG Compiler)
Always start `.d2` files with dual-theme configuration and a transparent root canvas:

```d2
vars: {
  d2-config: {
    layout-engine: elk
    theme-id: 0
    dark-theme-id: 200
  }
}
style.fill: transparent
```

Watch out for **three** D2 dark-mode landmines:

1. **`shape: class` Text Inversion Bug (Critical):**
   - Setting `style.fill: transparent` and `style.stroke: "#64748b"` on a `shape: class` node causes D2 to bind the class header `<rect>` fill to `stroke` (`#64748b`) **and** the class title `<text>` fill to `#64748b` (0:1 contrast — invisible title!), while binding member `<text>` fills to the node `fill` (`transparent` — completely invisible methods/fields!).
   - **Fix:** Omit `style.fill` and `style.stroke` on `shape: class` nodes (`if not is_class:`). D2's built-in `theme-id: 0` + `dark-theme-id: 200` automatically emits `.fill-N1` / `.fill-N7` (`#0A0F25` on `#FFFFFF` in light mode; `#CDD6F4` on `#1E1E2E` in dark mode — >11:1 contrast in both modes).
2. **`tooltip:` Glaring White `(i)` Badge (`.appendix-icon`):**
   - Any D2 node with `tooltip: ...` emits `<g class="appendix-icon"><path fill="white" .../><path stroke="#2E3346" .../></g>`, leaving glaring white circles on dark backgrounds even when `dark-theme-id: 200` is active.
   - **Fix:** Inject a `<style>` override immediately before `</svg>` after running `d2`:
     ```css
     .appendix-icon path[fill="white"]{fill:#f8fafc;stroke:#94a3b8;}
     @media screen and (prefers-color-scheme:dark){
       .appendix-icon path[fill="white"]{fill:#1e293b !important;stroke:#64748b !important;}
       .appendix-icon path[stroke="#2E3346"]{stroke:#cbd5e1 !important;}
     }
     ```
3. **`style.3d: true` Hardcoded Black Facets:**
   - D2 renders 3D box side/top facets with hardcoded `#000000` / dark gray fills that vanish into `#0b0f19` backgrounds.
   - **Fix:** Replace `style.3d: true` on UML deployment nodes with `style.double-border: true` and `stroke: "#64748b"`.

### B. Graphviz DOT (`dot -Tsvg`)
Graphviz `dot -Tsvg` emits static SVG presentation attributes (`<text fill="#1e293b" ...>`) and has no native `prefers-color-scheme` support:
- **Fix:** Emit `bgcolor="transparent"` on `digraph`, neutral `#64748b` node borders with `fillcolor="transparent"`, and post-process the compiled `.dot.svg` to inject CSS before `</svg>` (CSS author rules always override SVG presentation attributes):
  ```xml
  <style type="text/css"><![CDATA[
  text{fill:#1e293b;}
  @media (prefers-color-scheme:dark){text{fill:#e2e8f0;}}
  ]]></style>
  ```

### C. PlantUML & LikeC4
- **PlantUML:** Set `skinparam backgroundColor transparent`, `skinparam shadowing false`, `skinparam linetype ortho`, and `transparent` background + `#64748b` border on every `Package`, `Component`, `Class`, `ClassHeader`, `SequenceParticipant`, `State`, and `Activity` skinparam.
- **LikeC4:** Use semantic mid-luminance relationship palette tokens (`blue`, `slate`, `indigo`, `green`, `sky`, `amber`, `red`) rather than light-only custom hex fills.

---

## 4. Web Embedding & Manual Light/Dark Theme Switchers

External SVGs loaded via `<img src="diagram.svg">` run in an isolated document context:
- In **Auto (OS)** mode, `<img src="diagram.svg">` automatically honors internal `@media (prefers-color-scheme: dark)` rules inside D2 and DOT SVGs.
- When a webpage offers a manual **Light / Dark** toggle that overrides the OS setting (`<html data-theme="dark">` on a light-OS machine), browsers still evaluate `@media (prefers-color-scheme: dark)` inside `<img src="diagram.svg">` against the **OS** preference unless handled!
- **Fix for Manual Web Theme Switchers:**
  - Either inline trusted SVGs directly into the DOM, **or** keep `<img data-dual-svg src="diagram.svg">` for fast lazy-loading in `auto` mode and swap to a cached `Blob` URL (`image/svg+xml`) when the user forces `light` or `dark` by rewriting `(prefers-color-scheme:\s*dark)` inside the SVG text to `all` (forced dark) or `not all` (forced light).

---

## 5. Verification Checklist Before Shipping Diagrams

Run both automated structural checks and a visual dark/light inspection:

1. **Automated SVG & Mermaid Lint:**
   - Zero `.mmd` files contain `primaryColor: transparent` or `actorBkg: transparent`.
   - Zero `.mmd` files contain per-diagram `themeVariables` when a central `mermaid-theme.yaml` exists.
   - Zero `<text>` elements in `.svg` have `fill="transparent"`, `fill="#000000"`, or `fill="black"` without an overriding `@media (prefers-color-scheme: dark)` `<style>` rule.
   - Zero `.d2` files combine `shape: class` with `style.fill: transparent` or use `style.3d: true`.
2. **Visual Dual-Surface Check:**
   - Preview every diagram on `#ffffff` (Light) and `#0b0f19` (Dark): verify class titles, method signatures, edge labels, sequence lifelines, subgraph titles, and tooltip badges are all effortlessly legible.
