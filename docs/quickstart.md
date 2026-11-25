# ArchiGram Python Quickstart

Goal: from a clean checkout, pin a toolchain revision, regenerate a Mermaid diagram of a Python package, and fail CI when the code, the spec, or the committed diagram drift apart. The whole workflow is local and deterministic: **zero model calls, zero hosted services, byte-identical output**. After dependencies are installed it runs with the network disabled.

The runnable example lives in [`examples/python-quickstart/`](../examples/python-quickstart/): a six-module `shop` package, one [`CurationSpecV1`](../examples/python-quickstart/diagram-views.yaml), and the committed diagram [`diagrams/request_path.mmd`](../examples/python-quickstart/diagrams/request_path.mmd).

```text
shop.api ──imports──▶ shop.service ──imports──▶ shop.repository ──imports──▶ shop.models
                           └──imports──▶ shop.audit   (leaf stub, pruned from the view)
```

All commands below run from the ArchiGram repository root unless noted. Every one of them is exercised by `tests/fuzz/fuzz_quickstart_example.py` against the committed files.

## 1. Install a pinned toolchain

Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) are the only prerequisites.

**ArchiGram checkout (reproduces this example exactly).** `uv.lock` pins every dependency:

```bash
git clone https://github.com/klcd/archigram.git && cd archigram
git checkout <rev>             # pin the revision you trust
uv sync --frozen               # installs exactly what uv.lock says (needs network once)
```

**Your own repository.** Pin ArchiGram by commit SHA, never by branch:

```bash
uv tool install "git+https://github.com/klcd/archigram@<rev>"   # puts `archigram` on PATH
# or, without installing:
uvx --from "git+https://github.com/klcd/archigram@<rev>" archigram --help
```

## 2. Pass: lint and regenerate

```bash
uv run --frozen archigram lint examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml
# ArchiGram 5-Gate Linter: PASSED (0 error(s), 0 warning(s))

# Regenerate the committed diagram (writes examples/python-quickstart/diagrams/request_path.mmd):
uv run --frozen archigram apply examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml \
  --out-dir examples/python-quickstart/diagrams

# The CI check: same command + --check. Writes nothing, exits 1 on any failure.
uv run --frozen archigram apply examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml \
  --out-dir examples/python-quickstart/diagrams --check
# ArchiGram 5-Gate Linter: PASSED (0 error(s), 0 warning(s))
# Check passed: 1 view(s) up-to-date byte-for-byte.
```

Exit codes: `0` clean, `1` lint failure or stale artifact, `2` input/usage error (for example, a missing path).

### Offline check

After `uv sync --frozen`, the check needs no network. Prove it by forbidding uv from touching it:

```bash
uv run --offline --frozen archigram apply examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml \
  --out-dir examples/python-quickstart/diagrams --check
# or, bypassing uv entirely:
.venv/bin/archigram apply examples/python-quickstart/shop \
  --spec examples/python-quickstart/diagram-views.yaml \
  --out-dir examples/python-quickstart/diagrams --check
```

`tests/fuzz/fuzz_quickstart_example.py` additionally runs the check in-process with every socket connection and DNS lookup patched to raise, and asserts the output bytes are unchanged.

## 3. CI snippet

```yaml
# .github/workflows/diagrams.yml
name: diagrams
on: [pull_request]
jobs:
  diagram-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install "git+https://github.com/klcd/archigram@<rev>"
      # Everything below runs offline and calls no model:
      - run: archigram lint src/mypkg --spec docs/diagram-views.yaml
      - run: archigram apply src/mypkg --spec docs/diagram-views.yaml --out-dir docs/diagrams --check
```

The job fails when a required path disappears from the code, when curation hides a required path, or when the committed `.mmd` is stale. The log tells you which (next section). Add `--sync-md <dir>` to keep inline `<!-- archigram:start <view_id> -->` … `<!-- archigram:end <view_id> -->` Markdown blocks in sync as well; the quickstart example deliberately does not use it.

## 4. Break it on purpose

Work on a scratch copy so the committed files stay green:

```bash
cp -r examples/python-quickstart /tmp/qs && cd /tmp/qs
# `archigram` below is the installed CLI; in an ArchiGram checkout use `uv run --project <checkout> archigram`
```

### 4a. `REACHABILITY_NOT_OBSERVED`: the code no longer has the path

Edit `shop/service.py`: stop using the repository.

```diff
-from shop import audit, repository
+from shop import audit
 ...
-    saved = repository.save_order(order)
+    saved = order
```

```bash
archigram apply shop --spec diagram-views.yaml --out-dir diagrams --check
```

```text
ArchiGram 5-Gate Linter: FAILED (2 error(s), 0 warning(s))
  [ERROR] REACHABILITY_NOT_OBSERVED (gate_3_topology_poset, view=request_path): Required reachability from 'shop.api' to 'shop.repository' restricted to relations ['imports'] has no directed path in the observed input for view 'request_path'; no witness path exists.
    Endpoints: shop.api -> shop.repository
    Hint: Fix the endpoints or relation filter in require_reachability, or move the assertion to preserve_reachability if the path is optional.
  [ERROR] REACHABILITY_NOT_OBSERVED (gate_3_topology_poset, view=request_path): Required reachability from 'shop.api.place_order' to 'shop.repository.save_order' restricted to relations ['calls'] has no directed path in the observed input for view 'request_path'; no witness path exists.
    Endpoints: shop.api.place_order -> shop.repository.save_order
    Hint: ...
  [STALE] Committed diagram file drifted from spec: diagrams/request_path.mmd
  Fix the lint error(s) above first; regeneration refuses to write while lint fails.
  Then regenerate stale artifacts with: archigram apply shop --spec diagram-views.yaml --out-dir diagrams
```

There is **no `Witness:` line**: ArchiGram never invents a path. Either the code regressed (restore the call) or the assertion is wrong. A `via_relations` mismatch fails the same way: changing `via_relations: [imports]` to `[inherits]` in the YAML produces the same code with `restricted to relations ['inherits']`.

### 4b. `REACHABILITY_SEVERED`: the path exists, but curation hides it

Restore `shop/service.py`, then add an over-aggressive rule to `diagram-views.yaml` (under `rules:`):

```yaml
      - op: bypass_vertices
        id: hide_service_layer
        node_id_globs: [shop.service]
        preserve_edge_relations: [calls]   # splices only `calls`, so the `imports` path is cut
```

```text
ArchiGram 5-Gate Linter: FAILED (1 error(s), 0 warning(s))
  [ERROR] REACHABILITY_SEVERED (gate_3_topology_poset, view=request_path, rule=hide_service_layer): Reachability from 'shop.api' to 'shop.repository' via relations ['imports'] was severed in view 'request_path'; observed witness: shop.api -> shop.service -> shop.repository; bottleneck symbol(s): shop.service. Cause: attributable rule: bypass_vertices 'hide_service_layer' (splices only relations ['calls']).
    Endpoints: shop.api -> shop.repository
    Witness: shop.api -> shop.service -> shop.repository
    Bottlenecks: shop.service
    Hint: Preserve path connectivity by adding a bypass_vertices rule across intermediate bottleneck(s) (shop.service) or widening the view k_hop_neighborhood. Narrow or remove rule(s) hide_service_layer so they no longer remove symbols on the observed witness path.
```

The line names the view, both endpoints, the observed witness, the bottleneck node, and the rule (`rule=hide_service_layer`) responsible. A scope edit fails the same way, naming the scope instead of a rule: replacing `mode: full` with `mode: k_hop_neighborhood`, `focal_nodes: [shop.api, shop.repository]`, `upstream_depth: 0`, `downstream_depth: 0` reports `Cause: excluded node(s) shop.service fall outside view scope k_hop_neighborhood(...)`.

How to fix, in order of preference:

1. **Remove or narrow the rule** (`delete-rule`, or shrink its `node_id_globs`).
2. **Splice every relation on the path**: `preserve_edge_relations: [imports, calls]`. The check passes; `--check` then reports the diagram stale, so regenerate it.
3. **Widen the scope** (more `focal_nodes` or deeper `upstream_depth`/`downstream_depth`).
4. **Declare intent with `protected_nodes`**: listing `shop.service` makes Gate 2 report `PROTECTED_NODE_DROPPED` the moment any rule removes it, before reachability is evaluated. `protected_nodes` guards nodes that must stay visible; `require_reachability` guards paths.

### 4c. Stale artifact

```bash
echo "%% hand edit" >> examples/python-quickstart/diagrams/request_path.mmd   # on the committed copy, then `git checkout` it back
archigram apply examples/python-quickstart/shop --spec examples/python-quickstart/diagram-views.yaml \
  --out-dir examples/python-quickstart/diagrams --check
```

```text
ArchiGram 5-Gate Linter: PASSED (0 error(s), 0 warning(s))
  [STALE] Committed diagram file drifted from spec: examples/python-quickstart/diagrams/request_path.mmd
  Regenerate stale artifacts with: archigram apply examples/python-quickstart/shop --spec examples/python-quickstart/diagram-views.yaml --out-dir examples/python-quickstart/diagrams
```

A missing file is reported as `[STALE] Missing committed diagram file: ...`. `--check` never writes or creates anything; run the printed command (without `--check`) to regenerate, and commit the result.

## 5. What a passing check means (and does not)

**Extraction scope.** For a Python directory, `PythonAstStructuralIngestor` parses source with `ast` and records static facts: modules, classes and functions, `import` statements, statically resolvable calls, inheritance, and annotated composition. It does **not** see dynamic dispatch, `getattr`/`importlib` lookups, monkey-patching, decorators that rewire calls, dependency-injection containers, plugin entry points, or any runtime-only wiring. Complete architecture conformance (what *actually* runs, across services and processes) needs additional evidence: runtime traces (`interaction_trace` IR from OpenTelemetry or `sys.monitoring`) or a SCIP index (`--format scip`).

**A passing `apply --check` means all of:**

- every symbol the spec names exists in the statically extracted graph (Gate 1);
- each `require_reachability` path exists in that graph, restricted to the listed `via_relations` (any relation if omitted), and survives curation (Gate 3);
- protected nodes, budgets, and target syntax pass (Gates 2, 4, 5);
- the committed diagram bytes equal a fresh render.

It does **not** mean the system has no other dependencies, that the declared path executes at runtime, or that forbidden dependencies are absent (use `forbid_new_cycles` and `archigram diff` for regressions). Endpoints folded into a `collapse_partition` supernode are checked as their containing supernodes: a path between two members of the same supernode holds trivially, so assert reachability between visible, un-collapsed nodes (or list them in `protected_nodes`) when you need a meaningful check.

## 6. Adapt it to your package

1. Point `archigram` at your package directory and propose a starting spec (local, deterministic heuristic, no LLM):

   ```bash
   archigram propose src/mypkg --out docs/diagram-views.yaml
   ```

   `propose` takes `<input> [--out/-o PATH] [--format/-f FORMAT] [--max-turns N]` and runs a bounded propose → lint → repair loop with `DeterministicHeuristicProposer`. It writes a single `structural_overview` starter view (no reachability assertions, `mermaid` target) once it passes all five gates; without `--out` it prints the YAML. There is no `--goal` option. The heuristic proposer ignores `--format`; input format is auto-detected.
2. Rename the view, add `prune_leaves`/`bypass_vertices` rules, and add `require_reachability` for the two or three paths your architecture must keep. Author rules without hand-editing YAML via `archigram curate init|prune-leaves|bypass|collapse|scope-khop|budget|delete-rule`.
3. `archigram apply src/mypkg --spec docs/diagram-views.yaml --out-dir docs/diagrams`, commit the spec and `.mmd`, and add the CI job from section 3.
4. To compare two snapshots in a PR: `archigram diff --base base.ir.json --head head.ir.json --spec docs/diagram-views.yaml`.

## 7. Optional: local MCP server (stdio)

For an editor or agent on the same machine, ArchiGram ships a Model Context Protocol server over **stdio**: no hosted service, no network listener, no credentials. It exposes three deterministic tools:

| Tool | Input | Returns |
| --- | --- | --- |
| `get_ir_digest` | `ir_json`, `max_tokens` | Compact Markdown digest of a Canonical IR |
| `validate_curation_spec` | `ir_json`, `spec_yaml` | JSON `LintReport` (same diagnostics as the CLI) |
| `preview_curated_views` | `ir_json`, `spec_yaml` | JSON per-view counts, lint report, and rendered diagram text |

There is no `archigram-mcp` console script yet; start the server through its entrypoint function and register that command in your MCP client:

```bash
uv run --frozen python -c "from archigram.entrypoints.mcp import run_mcp_stdio_server; run_mcp_stdio_server()"
```

The tools take Canonical IR as JSON text (not a source path). The CLI is the CI interface; MCP is the interactive one, and both run the same five gates.
