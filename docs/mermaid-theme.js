// Generated from docs/architecture/mermaid-theme.yaml — do not edit per diagram.
(function (root) {
  const ARCHIGRAM_MERMAID_THEMES = {
  "light": {
    "darkMode": false,
    "background": "#ffffff",
    "primaryColor": "#f8fafc",
    "secondaryColor": "#f8fafc",
    "tertiaryColor": "#ffffff",
    "mainBkg": "#f8fafc",
    "primaryTextColor": "#0f172a",
    "secondaryTextColor": "#0f172a",
    "tertiaryTextColor": "#475569",
    "textColor": "#0f172a",
    "primaryBorderColor": "#64748b",
    "nodeBorder": "#64748b",
    "clusterBkg": "transparent",
    "clusterBorder": "#64748b",
    "lineColor": "#64748b",
    "edgeLabelBackground": "#ffffff",
    "actorBkg": "#f8fafc",
    "actorBorder": "#64748b",
    "actorTextColor": "#0f172a",
    "actorLineColor": "#64748b",
    "signalColor": "#3b82f6",
    "signalTextColor": "#0f172a",
    "labelBoxBkgColor": "#f8fafc",
    "labelBoxBorderColor": "#64748b",
    "labelTextColor": "#0f172a",
    "loopTextColor": "#0f172a",
    "activationBkgColor": "#f8fafc",
    "activationBorderColor": "#64748b",
    "noteBkgColor": "#f8fafc",
    "noteBorderColor": "#64748b",
    "noteTextColor": "#0f172a",
    "classText": "#0f172a",
    "titleColor": "#0f172a"
  },
  "dark": {
    "darkMode": true,
    "background": "#0b0f19",
    "primaryColor": "#111827",
    "secondaryColor": "#111827",
    "tertiaryColor": "#0b0f19",
    "mainBkg": "#111827",
    "primaryTextColor": "#e2e8f0",
    "secondaryTextColor": "#e2e8f0",
    "tertiaryTextColor": "#94a3b8",
    "textColor": "#e2e8f0",
    "primaryBorderColor": "#64748b",
    "nodeBorder": "#64748b",
    "clusterBkg": "transparent",
    "clusterBorder": "#64748b",
    "lineColor": "#94a3b8",
    "edgeLabelBackground": "#0b0f19",
    "actorBkg": "#111827",
    "actorBorder": "#64748b",
    "actorTextColor": "#e2e8f0",
    "actorLineColor": "#64748b",
    "signalColor": "#3b82f6",
    "signalTextColor": "#e2e8f0",
    "labelBoxBkgColor": "#111827",
    "labelBoxBorderColor": "#64748b",
    "labelTextColor": "#e2e8f0",
    "loopTextColor": "#e2e8f0",
    "activationBkgColor": "#111827",
    "activationBorderColor": "#64748b",
    "noteBkgColor": "#111827",
    "noteBorderColor": "#64748b",
    "noteTextColor": "#e2e8f0",
    "classText": "#e2e8f0",
    "titleColor": "#e2e8f0"
  }
};
  function resolveMode(override) {
    if (override === 'light' || override === 'dark') return override;
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'light' || attr === 'dark') return attr;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function buildMermaidConfig(mode) {
    const active = resolveMode(mode);
    return {
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      layout: "elk",
      flowchart: {"defaultRenderer": "elk", "curve": "linear"},
      themeVariables: ARCHIGRAM_MERMAID_THEMES[active],
    };
  }
  async function renderAllMermaid(mermaid, selector, mode) {
    const sel = selector || '[data-mermaid-src]';
    const nodes = Array.from(document.querySelectorAll(sel));
    if (!nodes.length || !mermaid) return;
    mermaid.initialize(buildMermaidConfig(mode));
    for (let i = 0; i < nodes.length; i += 1) {
      const el = nodes[i];
      const raw = el.getAttribute('data-mermaid-src');
      const src = raw || el.textContent || '';
      if (!raw) el.setAttribute('data-mermaid-src', src);
      const out = await mermaid.render('ag-mmd-' + i + '-' + Date.now(), src);
      el.innerHTML = out.svg;
    }
  }
  root.ArchiGramMermaidTheme = {
    themes: ARCHIGRAM_MERMAID_THEMES,
    resolveMode: resolveMode,
    buildConfig: buildMermaidConfig,
    renderAll: renderAllMermaid,
  };
})(typeof globalThis !== 'undefined' ? globalThis : window);
