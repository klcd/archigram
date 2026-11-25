"""Hypothesis fuzz suite for AG-03: the committed Python quickstart example is reproducible.

Every test copies the real `examples/python-quickstart/` files into a temp directory (the
repository tree is never mutated, and asserted unchanged), then drives the same
`archigram lint` / `archigram apply --check` commands that `docs/quickstart.md` documents.
"""

from __future__ import annotations

import shlex
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from archigram.entrypoints.cli.app import EXIT_LINT_OR_DRIFT, app

if TYPE_CHECKING:
    from collections.abc import Generator

_RUNNER = CliRunner()
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "python-quickstart"
_DIAGRAM = "request_path.mmd"
_SETTINGS = settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
_IDENT = st.from_regex(r"[a-z][a-z0-9_]{2,10}", fullmatch=True)
_NOISE = st.lists(st.from_regex(r"# [a-z ]{0,20}", fullmatch=True), min_size=0, max_size=4)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


@contextmanager
def _scratch_copy() -> Generator[Path]:
    """Copy the committed example into a temp dir; assert the repo copy is never touched."""
    before = _tree_bytes(_EXAMPLE)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "qs"
        root.mkdir()
        for rel, data in before.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        yield root
    assert _tree_bytes(_EXAMPLE) == before


def _lint_args(root: Path) -> list[str]:
    return ["lint", str(root / "shop"), "--spec", str(root / "diagram-views.yaml")]


def _apply_args(root: Path, *, check: bool = True) -> list[str]:
    args = [
        "apply",
        str(root / "shop"),
        "--spec",
        str(root / "diagram-views.yaml"),
        "--out-dir",
        str(root / "diagrams"),
    ]
    return [*args, "--check"] if check else args


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name} no longer contains {old!r}; update the quickstart mutation"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _error_blocks(output: str) -> dict[str, list[str]]:
    """Group CI log lines under the `[ERROR] CODE (...)` header that introduced them."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        if line.startswith("  [ERROR] "):
            code = line.removeprefix("  [ERROR] ").split(" ", 1)[0]
            current = blocks.setdefault(code, [])
            current.append(line)
        elif current is not None and line.startswith("    "):
            current.append(line)
        else:
            current = None
    return blocks


def _blocked_network(*_args: object, **_kwargs: object) -> NoReturn:
    msg = "network access is disabled in the quickstart offline check"
    raise OSError(msg)


@contextmanager
def _network_disabled() -> Generator[None]:
    with (
        mock.patch.object(socket.socket, "connect", _blocked_network),
        mock.patch.object(socket.socket, "connect_ex", _blocked_network),
        mock.patch.object(socket, "getaddrinfo", _blocked_network),
        mock.patch.object(socket, "create_connection", _blocked_network),
    ):
        yield


def _noisy_sources(root: Path, noise: list[str]) -> None:
    """Behavior-neutral edits: comments change spans, never the architecture."""
    for module in sorted((root / "shop").glob("*.py")):
        module.write_text(
            "\n".join(noise) + "\n" + module.read_text(encoding="utf-8") + "\n" + "\n".join(noise),
            encoding="utf-8",
        )


@_SETTINGS
@given(noise=_NOISE, repeats=st.integers(min_value=1, max_value=3), apply_noise=st.booleans())
def test_clean_example_passes_offline_with_byte_identical_output(
    noise: list[str], repeats: int, apply_noise: bool
) -> None:
    """Pass baseline: lint + `apply --check` pass offline, bytes match, repeats are identical."""
    with _scratch_copy() as root:
        committed = (root / "diagrams" / _DIAGRAM).read_bytes()
        if apply_noise:
            _noisy_sources(root, noise)
        before = _tree_bytes(root)

        with _network_disabled():
            lint = _RUNNER.invoke(app, _lint_args(root))
            assert lint.exit_code == 0, lint.output
            assert "PASSED" in lint.output
            for _ in range(repeats):
                check = _RUNNER.invoke(app, _apply_args(root))
                assert check.exit_code == 0, check.output
                assert "Check passed: 1 view(s) up-to-date byte-for-byte." in check.output
                assert "[STALE]" not in check.output
            fresh = root / "fresh"
            write = _RUNNER.invoke(
                app,
                [*_apply_args(root, check=False)[:-1], str(fresh)],
            )
            assert write.exit_code == 0, write.output

        assert (fresh / _DIAGRAM).read_bytes() == committed
        assert _tree_bytes(root) == {**before, f"fresh/{_DIAGRAM}": committed}


@_SETTINGS
@given(
    mutation=st.sampled_from(("drop_dependency", "via_relations_mismatch")),
    wrong_relation=st.sampled_from(("inherits", "implements", "composes")),
    noise=_NOISE,
)
def test_missing_observed_path_reports_no_witness(
    mutation: str, wrong_relation: str, noise: list[str]
) -> None:
    """Source-level path loss or a `via_relations` mismatch => REACHABILITY_NOT_OBSERVED."""
    with _scratch_copy() as root:
        _noisy_sources(root, noise)
        if mutation == "drop_dependency":
            service = root / "shop" / "service.py"
            _edit(service, "from shop import audit, repository", "from shop import audit")
            _edit(service, "    saved = repository.save_order(order)\n", "    saved = order\n")
        else:
            _edit(
                root / "diagram-views.yaml",
                "via_relations: [imports]",
                f"via_relations: [{wrong_relation}]",
            )
        before = _tree_bytes(root)

        result = _RUNNER.invoke(app, _apply_args(root))

        assert _tree_bytes(root) == before  # --check never writes
        assert result.exit_code == EXIT_LINT_OR_DRIFT
        blocks = _error_blocks(result.output)
        assert "REACHABILITY_SEVERED" not in blocks
        block = blocks["REACHABILITY_NOT_OBSERVED"]
        assert "view=request_path" in block[0]
        assert "no witness path exists" in block[0]
        assert "    Endpoints: shop.api -> shop.repository" in block
        assert not any("Witness:" in line for line in block)  # never fabricated
        relation = "imports" if mutation == "drop_dependency" else wrong_relation
        assert f"restricted to relations ['{relation}']" in block[0]
        assert any(line.startswith("    Hint: ") for line in block)


@_SETTINGS
@given(
    mutation=st.sampled_from(("bypass_relation_mismatch", "scope_excludes_middle")),
    rule_id=_IDENT,
    other_relation=st.sampled_from(("calls", "inherits", "composes")),
)
def test_over_aggressive_curation_reports_severed_path_with_attribution(
    mutation: str, rule_id: str, other_relation: str
) -> None:
    """Curation that hides the only route => REACHABILITY_SEVERED with witness + attribution."""
    with _scratch_copy() as root:
        spec = root / "diagram-views.yaml"
        if mutation == "bypass_relation_mismatch":
            _edit(
                spec,
                "    invariants:",
                f"      - op: bypass_vertices\n        id: {rule_id}\n"
                f"        node_id_globs: [shop.service]\n"
                f"        preserve_edge_relations: [{other_relation}]\n    invariants:",
            )
        else:
            _edit(
                spec,
                "      mode: full",
                "      mode: k_hop_neighborhood\n      focal_nodes: [shop.api, shop.repository]\n"
                "      upstream_depth: 0\n      downstream_depth: 0",
            )
        before = _tree_bytes(root)

        result = _RUNNER.invoke(app, _apply_args(root))

        assert _tree_bytes(root) == before
        assert result.exit_code == EXIT_LINT_OR_DRIFT
        blocks = _error_blocks(result.output)
        assert "REACHABILITY_NOT_OBSERVED" not in blocks
        severed = blocks["REACHABILITY_SEVERED"]
        assert "view=request_path" in severed[0]
        assert "    Endpoints: shop.api -> shop.repository" in severed
        assert "    Witness: shop.api -> shop.service -> shop.repository" in severed
        assert "    Bottlenecks: shop.service" in severed
        if mutation == "bypass_relation_mismatch":
            assert f"rule={rule_id}" in severed[0]
            assert f"bypass_vertices '{rule_id}'" in severed[0]
            # Documented fix: splice every relation on the path => lint passes again.
            _edit(
                spec,
                f"preserve_edge_relations: [{other_relation}]",
                "preserve_edge_relations: [imports, calls]",
            )
            fixed = _RUNNER.invoke(app, _lint_args(root))
            assert fixed.exit_code == 0, fixed.output
        else:
            assert "k_hop_neighborhood" in severed[0]
            assert "excluded node(s) shop.service" in severed[0]


@_SETTINGS
@given(
    scenario=st.sampled_from(("missing", "drifted")),
    suffix=st.from_regex(r"%% [a-z ]{1,20}\n", fullmatch=True),
)
def test_stale_diagram_prints_regeneration_command_that_fixes_it(
    scenario: str, suffix: str
) -> None:
    """`[STALE]` + printed regeneration command; check writes nothing; the command heals."""
    with _scratch_copy() as root:
        diagram = root / "diagrams" / _DIAGRAM
        committed = diagram.read_bytes()
        if scenario == "missing":
            diagram.unlink()
        else:
            diagram.write_bytes(committed + suffix.encode())
        before = _tree_bytes(root)

        result = _RUNNER.invoke(app, _apply_args(root))

        assert _tree_bytes(root) == before
        assert result.exit_code == EXIT_LINT_OR_DRIFT
        expected = (
            "Missing committed diagram file" if scenario == "missing" else "drifted from spec"
        )
        assert "[STALE] " in result.output
        assert expected in result.output
        marker = "Regenerate stale artifacts with: "
        command = next(
            line.split(marker, 1)[1] for line in result.output.splitlines() if marker in line
        )
        argv = shlex.split(command)
        assert argv[:2] == ["archigram", "apply"]
        assert "--check" not in argv

        healed = _RUNNER.invoke(app, argv[1:])
        assert healed.exit_code == 0, healed.output
        assert diagram.read_bytes() == committed
        again = _RUNNER.invoke(app, _apply_args(root))
        assert again.exit_code == 0, again.output
