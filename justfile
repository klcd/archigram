# ArchiGram Developer & Diagram Workflow Recipes
# Run `just` (or `just --list`) to inspect all available commands.

set shell := ["bash", "-euo", "pipefail", "-c"]

nix_exec := if env("IN_NIX_SHELL", "") != "" { "" } else { "direnv exec . " }

# List all available recipes grouped by category
default:
    @just --list

# Run all 8 mandatory ArchiGram quality & zero-drift gates
[group('quality')]
verify:
    {{nix_exec}}./scripts/verify.sh

# Run static format, complexity, security, strict typing, architecture & 5-Gate lints
[group('quality')]
lint:
    {{nix_exec}}uv run ruff format --check src tests scripts
    {{nix_exec}}uv run ruff check src tests scripts
    {{nix_exec}}uv run pyright
    {{nix_exec}}uv run lint-imports
    {{nix_exec}}uv run python scripts/check-test-discipline.py
    {{nix_exec}}uv run archigram lint src/archigram --spec docs/architecture/diagram-views.yaml
    {{nix_exec}}uv run archigram lint docs/architecture/traces/pipeline-and-repair.ir.json --spec docs/architecture/trace-views.yaml
    {{nix_exec}}uv run archigram lint src/archigram --spec docs/architecture/showcase-structural-views.yaml
    {{nix_exec}}uv run archigram lint docs/architecture/traces/pipeline-and-repair.ir.json --spec docs/architecture/showcase-trace-views.yaml

# Auto-format and fix lintable issues across src/ and tests/
[group('quality')]
fmt:
    {{nix_exec}}uv run ruff format src tests
    {{nix_exec}}uv run ruff check --fix src tests

# Run Hypothesis property-based fuzzing suite (pass extra pytest flags via `just fuzz -k style`)
[group('quality')]
fuzz *args:
    {{nix_exec}}uv run pytest tests/fuzz {{args}}

# Run pytest-benchmark throughput & latency suite
[group('quality')]
perf *args:
    {{nix_exec}}uv run pytest tests/perf --benchmark-only --benchmark-min-rounds=3 --benchmark-warmup=off {{args}}

# Capture live self-hosted sys.monitoring execution trace into Canonical IR JSON
[group('diagrams')]
trace:
    {{nix_exec}}uv run python -m archigram.entrypoints.cli.self_trace --out docs/architecture/traces/pipeline-and-repair.ir.json

# Compile all L0-L3 diagrams, UML 2.5 showcase, 3-stage triage SVGs & sync Markdown docs
[group('diagrams')]
docs: trace
    {{nix_exec}}uv run archigram apply src/archigram \
      --spec docs/architecture/diagram-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture
    {{nix_exec}}uv run archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
      --spec docs/architecture/trace-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture
    {{nix_exec}}uv run archigram apply src/archigram \
      --spec docs/architecture/showcase-structural-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture
    {{nix_exec}}uv run archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
      --spec docs/architecture/showcase-trace-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture
    {{nix_exec}}uv run archigram triage src/archigram \
      --spec docs/architecture/showcase-structural-views.yaml \
      --view showcase_cli_triage \
      --out-dir docs/architecture/diagrams
    {{nix_exec}}uv run archigram triage docs/architecture/traces/pipeline-and-repair.ir.json \
      --spec docs/architecture/showcase-trace-views.yaml \
      --view showcase_trace_triage \
      --out-dir docs/architecture/diagrams

# Check zero byte-drift and companion SVG SHA-256 parity across all L0-L3 & showcase docs
[group('diagrams')]
docs-check:
    {{nix_exec}}uv run archigram apply src/archigram \
      --spec docs/architecture/diagram-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture \
      --check
    {{nix_exec}}uv run archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
      --spec docs/architecture/trace-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture \
      --check
    {{nix_exec}}uv run archigram apply src/archigram \
      --spec docs/architecture/showcase-structural-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture \
      --check
    {{nix_exec}}uv run archigram apply docs/architecture/traces/pipeline-and-repair.ir.json \
      --spec docs/architecture/showcase-trace-views.yaml \
      --out-dir docs/architecture/diagrams \
      --sync-md docs/architecture \
      --check
    {{nix_exec}}uv run archigram triage src/archigram \
      --spec docs/architecture/showcase-structural-views.yaml \
      --view showcase_cli_triage \
      --out-dir docs/architecture/diagrams \
      --check
    {{nix_exec}}uv run archigram triage docs/architecture/traces/pipeline-and-repair.ir.json \
      --spec docs/architecture/showcase-trace-views.yaml \
      --view showcase_trace_triage \
      --out-dir docs/architecture/diagrams \
      --check

# Compile a single .d2 file to <file>.svg (auto-enabling --animate-interval when `steps:` is present)
[group('diagrams')]
d2 file pad="24" interval="1200":
    #!/usr/bin/env bash
    set -euo pipefail
    if grep -qE '^[[:space:]]*steps:[[:space:]]*\{' "{{file}}"; then
      {{nix_exec}}d2 --animate-interval "{{interval}}" --pad "{{pad}}" "{{file}}" "{{file}}.svg"
    else
      {{nix_exec}}d2 --pad "{{pad}}" "{{file}}" "{{file}}.svg"
    fi

# Live-preview and hot-reload a .d2 diagram in the browser
[group('diagrams')]
watch-d2 file="docs/architecture/diagrams/l0_hexagonal_rings_d2.d2":
    {{nix_exec}}d2 --watch --pad 24 "{{file}}" "{{file}}.preview.svg"

# Propose a new 5-Gate-verified CurationSpecV1 via the bounded 3-turn self-healing loop
[group('diagrams')]
propose target="src/archigram" goal="High-level hexagonal overview" out="curated.yaml":
    {{nix_exec}}uv run archigram propose "{{target}}" --goal "{{goal}}" --out "{{out}}"
