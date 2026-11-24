"""CLI Driving Adapter entrypoint."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="archigram",
    help="Deterministic graph projection, concurrency trace compaction, and 5-gate linter.",
    no_args_is_help=True,
)


@app.command("version")
def version_cmd() -> None:
    """Print ArchiGram CLI version."""
    from archigram import __version__

    typer.echo(__version__)


def main() -> None:
    """Entrypoint registered in pyproject.toml."""
    app()
