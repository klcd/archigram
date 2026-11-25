# ArchiGram Python quickstart example

A six-module `shop` package (`api -> service -> repository`, plus a `models` value object and an `audit` stub leaf), one curated Mermaid view, and the committed diagram it compiles to.

```text
examples/python-quickstart/
├── shop/                      # the "your code" part: 6 tiny modules
├── diagram-views.yaml         # CurationSpecV1: 1 view, 1 prune rule, 2 require_reachability assertions
└── diagrams/request_path.mmd  # committed output of `archigram apply` (checked byte-for-byte)
```

Run from the repository root (after `uv sync --frozen`):

```bash
uv run --frozen archigram lint examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml
uv run --frozen archigram apply examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml \
  --out-dir examples/python-quickstart/diagrams --check
```

Full walkthrough (pinned install, offline check, CI snippet, both reachability failures, extraction limits, `propose`, MCP): **[docs/quickstart.md](../../docs/quickstart.md)**.

To adapt it: copy `diagram-views.yaml`, replace the `shop.*` symbol ids with your package's dotted module ids, and point `apply` at your package directory.
